"""
Threshold Background Daemon (src/threshold/daemon.py).

Provides a single-threaded, resilient orchestration loop combining:
1. Periodic Ring Partner API poller (poll_once) with hot-reloading from .env on token refresh.
2. Deterministic absence scheduler evaluation (run_scheduler_tick).
3. Fast-response Telegram callback polling (dismissing caretaker loading spinners within 2s).
4. Signal handling (SIGINT, SIGTERM) for graceful shutdown.
5. Error boundaries around all network and evaluation ticks.
6. Daemon health state persistence in SQLite (app_state table).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Union

from src.threshold.models import Routine
from src.threshold.notifier import ConsoleNotifier, Notifier, TelegramNotifier
from src.threshold.ring_client import RingAPIError, RingClient, TokenExpired, poll_once
from src.threshold.scheduler import Clock, RealClock, run_scheduler_tick
from src.threshold.store import get_app_state, init_db, list_routines, set_app_state

logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Configuration options for ThresholdDaemon."""

    db_path: str = "threshold.db"
    poll_interval_seconds: float = 300.0  # 5 minutes
    tick_interval_seconds: float = 60.0  # 1 minute
    callback_interval_seconds: float = 2.0  # 2 seconds for responsive caretaker replies
    household_name: str = "Threshold Household"
    timezone_name: str = "UTC"
    staleness_threshold_minutes: int = 120
    reminder_delay_minutes: int = 15
    enable_polling: bool = True
    dashboard_enabled: bool = True
    dashboard_port: int = 8080
    dashboard_host: str = "0.0.0.0"
    snapshot_dir: str = "snapshots"


def read_env_value(key: str, filepath: str = ".env") -> Optional[str]:
    """Read a specific key directly from a .env file, bypassing cached os.environ."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k == key:
                        return v
    except Exception:
        pass
    return None


def format_local_time(dt: datetime, tz_name: str) -> str:
    """Format datetime in local timezone string."""
    try:
        from zoneinfo import ZoneInfo

        local_dt = dt.astimezone(ZoneInfo(tz_name))
        return local_dt.strftime("%H:%M:%S %Z")
    except Exception:
        return dt.strftime("%H:%M:%S UTC")


class ThresholdDaemon:
    """
    Unified background daemon for Threshold.
    Runs single-threaded loop with signal handling, error boundaries,
    and fast caretaker callback processing.
    """

    def __init__(
        self,
        config: Optional[DaemonConfig] = None,
        ring_client: Optional[RingClient] = None,
        notifier: Optional[Notifier] = None,
        conn: Optional[sqlite3.Connection] = None,
        clock: Optional[Clock] = None,
    ):
        self.config = config or DaemonConfig()
        self.clock = clock or RealClock()
        self._owns_conn = False

        if conn is not None:
            self.conn = conn
        else:
            self.conn = init_db(self.config.db_path)
            self._owns_conn = True

        self.ring_client = ring_client
        self.notifier = notifier or ConsoleNotifier(ring_client=ring_client)
        if self.ring_client and hasattr(self.notifier, "ring_client") and not getattr(self.notifier, "ring_client", None):
            self.notifier.ring_client = self.ring_client

        self.shutdown_requested = False
        self.polling_paused = False
        self.tick_count = 0
        self.poll_count = 0
        self.dashboard_server = None

    def stop(self) -> None:
        """Signal the daemon to stop gracefully."""
        self.shutdown_requested = True
        if self.dashboard_server:
            try:
                self.dashboard_server.shutdown()
                self.dashboard_server.server_close()
            except Exception:
                pass
            self.dashboard_server = None

    def build_startup_banner(self) -> str:
        """Generate formatted startup banner with routine breakdown."""
        routines = list_routines(self.conn)
        routine_count = len(routines)

        # Count devices
        cur = self.conn.execute("SELECT COUNT(*), SUM(CASE WHEN online = 1 THEN 1 ELSE 0 END) FROM device_health")
        row = cur.fetchone()
        dev_total = row[0] if row else 0
        dev_online = row[1] if row and row[1] is not None else 0

        dev_summary = f"{dev_total} tracked ({dev_online} online)" if dev_total > 0 else "0 tracked (awaiting poll)"
        routines_summary = f"{routine_count} loaded"

        notifier_type = type(self.notifier).__name__
        poll_str = f"every {int(self.config.poll_interval_seconds)}s" if self.config.enable_polling and self.ring_client else "disabled"
        tick_str = f"every {int(self.config.tick_interval_seconds)}s"

        banner_lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║  THRESHOLD v1.0 — Pattern Absence Detection for Elderly Care ║",
            "╠══════════════════════════════════════════════════════════════╣",
            f"║  Household:  {self.config.household_name:<47}║",
            f"║  Timezone:   {self.config.timezone_name:<47}║",
            f"║  Devices:    {dev_summary:<47}║",
            f"║  Routines:   {routines_summary:<47}║",
        ]

        if routines:
            for r in routines[:4]:
                r_name = r.get("name", "Unnamed")
                w_start = r.get("window_start", "?")
                w_end = r.get("window_end", "?")
                line_content = f"• {r_name} ({w_start}–{w_end})"
                if len(line_content) > 46:
                    line_content = line_content[:43] + "..."
                banner_lines.append(f"║              {line_content:<47}║")
            if len(routines) > 4:
                more_msg = f"(+{len(routines) - 4} more routines)"
                banner_lines.append(f"║              {more_msg:<47}║")

        banner_lines.extend([
            f"║  Polling:    {poll_str:<15} | Ticking: {tick_str:<18}║",
            f"║  Notifier:   {notifier_type:<47}║",
        ])

        if self.config.dashboard_enabled:
            disp_host = "localhost" if self.config.dashboard_host in ("0.0.0.0", "127.0.0.1") else self.config.dashboard_host
            dash_url = f"http://{disp_host}:{self.config.dashboard_port}"
            banner_lines.append(f"║  Dashboard:  {dash_url:<47}║")

        banner_lines.append("╚══════════════════════════════════════════════════════════════╝")
        return "\n".join(banner_lines)

    def tick_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Execute a single scheduler evaluation tick."""
        if now is None:
            now = self.clock.now()

        res = run_scheduler_tick(
            conn=self.conn,
            notifier=self.notifier,
            now=now,
            staleness_threshold_minutes=self.config.staleness_threshold_minutes,
            reminder_delay_minutes=self.config.reminder_delay_minutes,
        )
        self.tick_count += 1

        # Record health state in app_state table
        try:
            set_app_state(self.conn, "daemon_last_tick_at", now.isoformat())
            set_app_state(self.conn, "daemon_tick_count", str(self.tick_count))
        except Exception:
            pass

        return res

    def poll_once(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """
        Execute a single Ring API poll if client is available.
        Hot-reloads fresh token from .env if polling was previously paused.
        """
        if not self.config.enable_polling or not self.ring_client:
            return None

        # Hot-reload token if paused
        if self.polling_paused:
            fresh_token = read_env_value("RING_ACCESS_TOKEN") or read_env_value("RING_TOKEN")
            current_token = getattr(self.ring_client, "access_token", "")
            if fresh_token and fresh_token.strip() and fresh_token.strip() != current_token:
                self.ring_client.access_token = fresh_token.strip()
                self.polling_paused = False
                logger.info("Detected updated Ring access token in .env. Resuming poll mode.")
                print("\n[DAEMON] 🔄 Detected updated Ring access token in .env. Resuming poll mode!")
            else:
                return None

        if now is None:
            now = self.clock.now()

        try:
            res = poll_once(self.conn, self.ring_client, now)
            self.poll_count += 1
            try:
                set_app_state(self.conn, "daemon_last_poll_at", now.isoformat())
                set_app_state(self.conn, "daemon_poll_count", str(self.poll_count))
            except Exception:
                pass
            return res
        except TokenExpired:
            self.polling_paused = True
            logger.warning("[WARN] Ring access token expired (HTTP 401). Polling paused until refreshed in .env.")
            print("\n[WARN] Ring access token expired (HTTP 401). Polling paused. (Update .env to auto-resume)", file=sys.stderr)
            return None
        except RingAPIError as e:
            logger.warning("[WARN] Ring API error during poll: %s", e)
            print(f"\n[WARN] Ring API poll error (continuing): {e}", file=sys.stderr)
            return None
        except Exception as e:
            logger.warning("[WARN] Unexpected poll exception: %s", e)
            print(f"\n[WARN] Unexpected poll error (continuing): {e}", file=sys.stderr)
            return None

    def poll_callbacks_fast(self) -> List[Dict[str, Any]]:
        """Poll caretaker callbacks from notifier and log actions promptly."""
        if hasattr(self.notifier, "poll_and_process_callbacks") and callable(getattr(self.notifier, "poll_and_process_callbacks")):
            try:
                processed = self.notifier.poll_and_process_callbacks(self.conn, timeout=0)
                for cb in processed:
                    action = cb.get("action", "acknowledged")
                    routine_id = cb.get("routine_id", "unknown")
                    local_date = cb.get("local_date", "")
                    action_label = "False Alarm Recorded" if action == "false_alarm" else "Acknowledged"
                    print(
                        f"\n[CARETAKER] 📱 Caretaker action received: '{action_label}' "
                        f"for routine '{routine_id}' ({local_date})"
                    )
                return processed
            except Exception as e:
                logger.debug("Callback poll error: %s", e)
        return []

    def format_heartbeat(self, tick_result: Dict[str, Any], now: datetime) -> str:
        """Format heartbeat status line for terminal output."""
        time_str = format_local_time(now, self.config.timezone_name)
        statuses = tick_result.get("statuses", {})
        new_esc = tick_result.get("new_escalations", 0)

        status_parts = []
        for r_id, st in statuses.items():
            short_id = r_id[-12:] if len(r_id) > 12 else r_id
            badge = "✓" if st == "observed" else ("⚠" if st == "missed" else "")
            status_parts.append(f"{short_id}: {st} {badge}".strip())

        status_str = " | ".join(status_parts) if status_parts else "no routines"
        esc_str = f"{new_esc} new escalation(s)" if new_esc > 0 else "0 escalations"
        return f"[{time_str}] tick #{self.tick_count:<3} | {status_str} | {esc_str}"

    def print_alert_box(self, tick_result: Dict[str, Any], now: datetime) -> None:
        """Print prominent alert box when escalations or reminders fire."""
        new_esc = tick_result.get("new_escalations", 0)
        delivered = tick_result.get("delivered_notifications", 0)
        reminded = tick_result.get("reminded_notifications", 0)
        time_str = format_local_time(now, self.config.timezone_name)

        if new_esc > 0:
            print(
                f"\n┌─────────────────────────────────────────────────────────────┐\n"
                f"│ 🚨 PATTERN ABSENCE DETECTED — ESCALATION FIRED              │\n"
                f"│ Time:    {time_str:<49}│\n"
                f"│ Details: {new_esc} routine(s) missed expected activity window!     │\n"
                f"└─────────────────────────────────────────────────────────────┘"
            )
        if delivered > 0:
            notifier_name = type(self.notifier).__name__
            print(f"[ALERT] 🔔 Delivered {delivered} escalation alert(s) via {notifier_name}")
        if reminded > 0:
            print(f"[REMINDER] ⏰ Sent {reminded} unacknowledged escalation reminder(s)")

    def run(self, max_ticks: Optional[int] = None) -> int:
        """
        Run the main daemon orchestration loop.
        Blocks until stop() is called, SIGINT/SIGTERM is received, or max_ticks is reached.
        """
        try:
            def _sig_handler(signum, frame):
                print(f"\n[SIGNAL] Received signal {signum}. Stopping Threshold daemon...")
                self.stop()

            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except (ValueError, AttributeError):
            pass

        # Record daemon status in app_state
        try:
            set_app_state(self.conn, "daemon_status", "running")
            set_app_state(self.conn, "daemon_started_at", self.clock.now().isoformat())
        except Exception:
            pass

        # Start local web dashboard if enabled
        if self.config.dashboard_enabled and not self.dashboard_server:
            try:
                from src.threshold.dashboard import start_dashboard_server

                self.dashboard_server = start_dashboard_server(
                    db_path=self.config.db_path,
                    port=self.config.dashboard_port,
                    host=self.config.dashboard_host,
                    household_name=self.config.household_name,
                    timezone_name=self.config.timezone_name,
                    snapshot_dir=self.config.snapshot_dir,
                )
            except Exception as exc:
                logger.warning("Failed starting dashboard server: %s", exc)

        print(self.build_startup_banner())
        print(f"\nThreshold daemon started. Press Ctrl+C to stop.\n")

        # Initial poll
        if self.config.enable_polling and self.ring_client:
            print("[DAEMON] Performing initial device and event history poll...")
            poll_res = self.poll_once()
            if poll_res:
                print(f"[DAEMON] Initial poll complete: {poll_res.get('devices_polled', 0)} device(s) polled, {poll_res.get('events_ingested', 0)} event(s) ingested.\n")

        last_poll_time = time.monotonic()
        last_tick_time = 0.0
        last_cb_poll = 0.0

        try:
            while not self.shutdown_requested:
                now_mono = time.monotonic()
                now_dt = self.clock.now()

                # 1. Scheduler Tick
                if (now_mono - last_tick_time) >= self.config.tick_interval_seconds or last_tick_time == 0.0:
                    try:
                        tick_res = self.tick_once(now_dt)
                        heartbeat = self.format_heartbeat(tick_res, now_dt)
                        print(heartbeat)
                        self.print_alert_box(tick_res, now_dt)
                    except Exception as e:
                        logger.error("[ERROR] Exception in scheduler tick: %s", e, exc_info=True)
                        print(f"[ERROR] Exception in scheduler tick (continuing): {e}", file=sys.stderr)

                    last_tick_time = now_mono

                    if max_ticks is not None and self.tick_count >= max_ticks:
                        break

                # 2. Ring API Poll
                if self.config.enable_polling and self.ring_client:
                    if (now_mono - last_poll_time) >= self.config.poll_interval_seconds:
                        poll_res = self.poll_once(now_dt)
                        if poll_res:
                            p_time = format_local_time(now_dt, self.config.timezone_name)
                            print(f"[{p_time}] poll     | {poll_res.get('events_ingested', 0)} new event(s) | {poll_res.get('devices_polled', 0)} device(s) checked")
                        last_poll_time = now_mono

                # 3. Responsive sleep with fast caretaker callback check
                sleep_accum = 0.0
                step = min(0.5, self.config.callback_interval_seconds)
                target_sleep = min(1.0, self.config.tick_interval_seconds)

                while sleep_accum < target_sleep and not self.shutdown_requested:
                    mono_inner = time.monotonic()
                    if (mono_inner - last_cb_poll) >= self.config.callback_interval_seconds:
                        self.poll_callbacks_fast()
                        last_cb_poll = mono_inner

                    time.sleep(step)
                    sleep_accum += step

        finally:
            print("[SHUTDOWN] Threshold daemon stopped cleanly.")
            if self.dashboard_server:
                try:
                    self.dashboard_server.shutdown()
                    self.dashboard_server.server_close()
                except Exception:
                    pass
                self.dashboard_server = None

            try:
                set_app_state(self.conn, "daemon_status", "stopped")
                set_app_state(self.conn, "daemon_stopped_at", self.clock.now().isoformat())
                self.conn.commit()
            except Exception:
                pass

            if self._owns_conn:
                try:
                    self.conn.close()
                except Exception:
                    pass

        return 0
