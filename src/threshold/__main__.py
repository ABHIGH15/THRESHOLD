"""
Threshold CLI Entry Point (python3 -m src.threshold).

Provides administrative and diagnostic subcommands:
- ring-check: Polls devices, status, and recent history from Ring Partner API with token redaction.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import sys

from scripts.probe_ring import load_env
from src.threshold.config import find_config_file, load_config_file, seed_routines_from_config, ThresholdConfig
from src.threshold.daemon import DaemonConfig, ThresholdDaemon
from src.threshold.notifier import ConsoleNotifier, TelegramNotifier
from src.threshold.ring_client import RingAPIError, RingClient, TokenExpired
from src.threshold.store import init_db, ms_to_iso


def run_ring_check() -> int:
    """Execute ring-check diagnostic subcommand against Ring Partner API."""
    load_env(".env")

    token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    if not token or not token.strip():
        print("ERROR: RING_ACCESS_TOKEN is not set in environment or .env file.", file=sys.stderr)
        print("Please configure RING_ACCESS_TOKEN in .env or run with a valid Playground token.", file=sys.stderr)
        return 1

    token = token.strip()
    client = RingClient(access_token=token)

    print("=" * 80)
    print("THRESHOLD RING PARTNER API DIAGNOSTIC (ring-check)")
    print("=" * 80)

    try:
        devices = client.list_devices()
    except TokenExpired:
        print("ERROR: Ring access token has expired (HTTP 401). Please generate a fresh token.", file=sys.stderr)
        return 1
    except RingAPIError as exc:
        print(f"ERROR: Failed to list devices: {exc}", file=sys.stderr)
        return 1

    if not devices:
        print("No devices discovered for this Ring account.")
        return 0

    print(f"Discovered {len(devices)} device(s):\n")

    for idx, dev in enumerate(devices, start=1):
        dev_id = dev.get("id", "N/A")
        attrs = dev.get("attributes", {})
        dev_name = attrs.get("name", "Unnamed Device")
        print(f"[{idx}] Device: {dev_name}")
        print(f"    ID:   {dev_id}")

        # Device status
        try:
            status_obj = client.get_device_status(dev_id)
            s_attrs = status_obj.get("attributes", {})
            online = s_attrs.get("online", False)
            reported_at = s_attrs.get("reported_at", "N/A")
            state_str = "ONLINE (Healthy)" if online else "OFFLINE"
            print(f"    Status:      {state_str}")
            print(f"    Reported At: {reported_at}")
        except Exception as e:
            print(f"    Status:      Failed to fetch ({e})")

        # Recent history
        try:
            history = client.get_event_history(dev_id)
            print(f"    Recent Events ({len(history)} recorded):")
            if not history:
                print("      (No events recorded)")
            else:
                for ev in history[:5]:  # show up to 5 recent events
                    ev_id = ev.get("id", "")
                    short_id = ev_id[-16:] if len(ev_id) > 16 else ev_id
                    ev_attrs = ev.get("attributes", {})
                    etype = ev_attrs.get("event_type", "unknown")
                    start_ms = ev_attrs.get("start")
                    start_str = ms_to_iso(start_ms) if start_ms else "N/A"
                    print(f"      - {start_str} | type: {etype} (id: ...{short_id})")
        except Exception as e:
            print(f"    Events:      Failed to fetch ({e})")

        print("-" * 80)

    return 0


def run_seed_command(args: argparse.Namespace) -> int:
    """Seed routines from declarative configuration file into SQLite database."""
    load_env(".env")

    config_path = find_config_file(args.config)
    if not config_path:
        print(f"ERROR: Configuration file '{args.config or 'threshold.json'}' not found.", file=sys.stderr)
        print("Tip: Copy threshold.example.json to threshold.json and customize it with your device IDs.", file=sys.stderr)
        return 1

    try:
        cfg = load_config_file(config_path)
    except Exception as exc:
        print(f"ERROR: Failed to load configuration '{config_path}': {exc}", file=sys.stderr)
        return 1

    ring_client = None
    if args.verify_devices:
        token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
        if token and token.strip():
            ring_client = RingClient(access_token=token.strip())
        else:
            print("[WARN] --verify-devices requested but no RING_ACCESS_TOKEN found in .env.", file=sys.stderr)

    conn = init_db(args.db)
    res = seed_routines_from_config(conn, cfg, client=ring_client)

    print("=" * 80)
    print("THRESHOLD ROUTINE SEEDER")
    print("=" * 80)
    print(f"Config:    {config_path} (Household: {cfg.household}, TZ: {cfg.timezone})")
    print(f"Database:  {args.db}")
    print(f"\nSeeded {res['seeded_count']} routine(s):\n")

    for idx, r in enumerate(cfg.routines, start=1):
        cam_info = f"Snapshot: {r.snapshot_device_id}" if r.snapshot_device_id else "Snapshot: (Primary sensor)"
        print(f"[{idx}] {r.name} (id: {r.id})")
        print(f"    Window:    {r.window_start} – {r.window_end} (+{r.escalation_delay_minutes}m delay)")
        print(f"    Device:    {r.device_id}")
        print(f"    Camera:    {cam_info}")
        print(f"    Days:      {', '.join(r.days)}")
        print("-" * 80)

    if res.get("warnings"):
        print("\nWarnings:")
        for w in res["warnings"]:
            print(f"  [WARN] {w}")

    print(f"\nSuccessfully seeded {res['seeded_count']} routine(s) into {args.db}!")
    conn.close()
    return 0


def run_daemon_command(args: argparse.Namespace) -> int:
    """Execute daemon background service."""
    load_env(".env")

    # Check for config file to auto-populate defaults
    cfg: Optional[ThresholdConfig] = None
    config_path = find_config_file(args.config)
    if config_path:
        try:
            cfg = load_config_file(config_path)
            print(f"[CONFIG] Loaded household configuration from '{config_path}'")
        except Exception as exc:
            print(f"[WARN] Failed to load config file '{config_path}': {exc}", file=sys.stderr)

    # CLI args override config file if explicitly provided, else use config file, else defaults
    household = args.household if args.household is not None else (cfg.household if cfg else "Threshold Household")
    timezone_name = args.timezone if args.timezone is not None else (cfg.timezone if cfg else "America/Chicago")
    poll_interval = args.poll_interval if args.poll_interval is not None else (cfg.poll_interval_seconds if cfg else 300.0)
    tick_interval = args.tick_interval if args.tick_interval is not None else (cfg.tick_interval_seconds if cfg else 60.0)
    cb_interval = cfg.callback_interval_seconds if cfg else 2.0

    conn = init_db(args.db)

    # Auto-seed routines from config if config was loaded
    if cfg and cfg.routines:
        seed_routines_from_config(conn, cfg)

    # Ring Client setup
    token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    ring_client = None
    if token and token.strip() and not args.no_poll:
        ring_client = RingClient(access_token=token.strip())

    # Notifier setup
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id and tg_token.strip() and tg_chat_id.strip():
        notifier = TelegramNotifier(bot_token=tg_token.strip(), chat_id=tg_chat_id.strip())
    else:
        notifier = ConsoleNotifier()

    config = DaemonConfig(
        db_path=args.db,
        poll_interval_seconds=float(poll_interval),
        tick_interval_seconds=float(tick_interval),
        callback_interval_seconds=float(cb_interval),
        household_name=household,
        timezone_name=timezone_name,
        enable_polling=not args.no_poll,
    )

    daemon = ThresholdDaemon(
        config=config,
        ring_client=ring_client,
        notifier=notifier,
        conn=conn,
    )

    max_ticks = 1 if args.once else None
    return daemon.run(max_ticks=max_ticks)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m src.threshold",
        description="Threshold Command-Line Tools & Diagnostics",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # ring-check
    subparsers.add_parser(
        "ring-check",
        help="Check Ring Partner API devices, status, and recent event history",
    )

    # seed
    seed_parser = subparsers.add_parser(
        "seed",
        help="Seed routines from declarative JSON configuration into SQLite database",
    )
    seed_parser.add_argument("--config", "-c", default=None, help="Path to JSON config file (default: threshold.json)")
    seed_parser.add_argument("--db", default="threshold.db", help="Path to SQLite database (default: threshold.db)")
    seed_parser.add_argument("--verify-devices", action="store_true", help="Verify device IDs against Ring Partner API")

    # run (daemon)
    run_parser = subparsers.add_parser(
        "run",
        help="Run the Threshold background service daemon",
    )
    run_parser.add_argument("--config", "-c", default=None, help="Path to JSON config file (default: threshold.json if exists)")
    run_parser.add_argument("--db", default="threshold.db", help="Path to SQLite database (default: threshold.db)")
    run_parser.add_argument("--poll-interval", type=float, default=None, help="Ring poll interval in seconds (default: 300)")
    run_parser.add_argument("--tick-interval", type=float, default=None, help="Scheduler tick interval in seconds (default: 60)")
    run_parser.add_argument("--household", default=None, help="Household name (overrides config)")
    run_parser.add_argument("--timezone", default=None, help="Local timezone name (overrides config)")
    run_parser.add_argument("--no-poll", action="store_true", help="Disable Ring API polling (scheduler only)")
    run_parser.add_argument("--once", action="store_true", help="Execute single cycle and exit")

    args = parser.parse_args()

    if args.subcommand == "ring-check":
        return run_ring_check()
    elif args.subcommand == "seed":
        return run_seed_command(args)
    elif args.subcommand == "run":
        return run_daemon_command(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
