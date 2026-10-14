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
from src.threshold.models import Routine
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


def run_init_command(args: argparse.Namespace) -> int:
    """Initialize a threshold.json configuration file from threshold.example.json template."""
    import shutil
    template_path = "threshold.example.json"
    target_path = args.target

    if not os.path.exists(template_path):
        print(f"ERROR: Template file '{template_path}' not found in current directory.", file=sys.stderr)
        return 1

    if os.path.exists(target_path) and not args.force:
        print(f"ERROR: Configuration file '{target_path}' already exists.", file=sys.stderr)
        print("Use --force to overwrite it with the default template.", file=sys.stderr)
        return 1

    try:
        shutil.copyfile(template_path, target_path)
        print(f"[OK] Created configuration file '{target_path}' from template.")
        print("\nNext steps:")
        print(f"  1. Edit '{target_path}' with your household details and Ring device IDs.")
        print("  2. Run 'python3 -m src.threshold seed' to populate the database.")
        print("  3. Run 'python3 -m src.threshold run' to start the monitoring service.")
        return 0
    except Exception as exc:
        print(f"ERROR: Failed to create '{target_path}': {exc}", file=sys.stderr)
        return 1


def run_seed_command(args: argparse.Namespace) -> int:
    """Seed routines from declarative configuration file into SQLite database."""
    load_env(".env")

    config_path = find_config_file(args.config)
    if not config_path:
        print(f"ERROR: Configuration file '{args.config or 'threshold.json'}' not found.", file=sys.stderr)
        print("Tip: Run 'python3 -m src.threshold init' to create a starter configuration file.", file=sys.stderr)
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
    res = seed_routines_from_config(conn, cfg, client=ring_client, purge_unlisted=getattr(args, "purge", False))

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

    if res.get("purged_count", 0) > 0:
        print(f"\nPurged {res['purged_count']} unlisted routine(s): {', '.join(res['purged_routines'])}")

    if res.get("verified_devices"):
        print("\nVerified Ring Devices:")
        for dev_id, info in res["verified_devices"].items():
            st = "ONLINE" if info["online"] else ("OFFLINE" if info["online"] is False else "UNKNOWN")
            symbol = "✓" if info["online"] else "!"
            print(f"  [{symbol} {st}] {info['name']} ({dev_id})")

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
        notifier = TelegramNotifier(
            bot_token=tg_token.strip(),
            chat_id=tg_chat_id.strip(),
            ring_client=ring_client,
        )
    else:
        notifier = ConsoleNotifier(ring_client=ring_client)

    dashboard_enabled = not getattr(args, "no_dashboard", False)
    dashboard_port = getattr(args, "dashboard_port", 8080)
    dashboard_host = getattr(args, "dashboard_host", "0.0.0.0")

    config = DaemonConfig(
        db_path=args.db,
        poll_interval_seconds=float(poll_interval),
        tick_interval_seconds=float(tick_interval),
        callback_interval_seconds=float(cb_interval),
        household_name=household,
        timezone_name=timezone_name,
        enable_polling=not args.no_poll,
        dashboard_enabled=dashboard_enabled,
        dashboard_port=dashboard_port,
        dashboard_host=dashboard_host,
    )

    daemon = ThresholdDaemon(
        config=config,
        ring_client=ring_client,
        notifier=notifier,
        conn=conn,
    )

    max_ticks = 1 if args.once else None
    return daemon.run(max_ticks=max_ticks)


def run_test_alert_command(args: argparse.Namespace) -> int:
    """Send an immediate test escalation alert with snapshot enrichment to Telegram/Console."""
    load_env(".env")

    # Load config if available to get routine definitions
    config_path = find_config_file(args.config)
    cfg: Optional[ThresholdConfig] = None
    if config_path:
        try:
            cfg = load_config_file(config_path)
        except Exception as exc:
            print(f"[WARN] Failed to load config file: {exc}", file=sys.stderr)

    # Pick a routine
    routine_obj = None
    if cfg and cfg.routines:
        if args.routine:
            matching = [r for r in cfg.routines if r.id == args.routine]
            if matching:
                r_cfg = matching[0]
                routine_obj = Routine(
                    property_id=cfg.household,
                    name=r_cfg.name,
                    device_id=r_cfg.device_id,
                    expected_event_type="motion_detected",
                    window_start=r_cfg.window_start,
                    window_end=r_cfg.window_end,
                    timezone=cfg.timezone,
                    days=r_cfg.days,
                    escalation_delay_minutes=r_cfg.escalation_delay_minutes,
                    snapshot_device_id=r_cfg.snapshot_device_id,
                )
                routine_obj.routine_id = r_cfg.id
        if not routine_obj:
            r_cfg = cfg.routines[0]
            routine_obj = Routine(
                property_id=cfg.household,
                name=r_cfg.name,
                device_id=r_cfg.device_id,
                expected_event_type="motion_detected",
                window_start=r_cfg.window_start,
                window_end=r_cfg.window_end,
                timezone=cfg.timezone,
                days=r_cfg.days,
                escalation_delay_minutes=r_cfg.escalation_delay_minutes,
                snapshot_device_id=r_cfg.snapshot_device_id,
            )
            routine_obj.routine_id = r_cfg.id

    if not routine_obj:
        routine_obj = Routine(
            property_id="Household",
            name="Morning Medicine",
            device_id="sensor_cabinet",
            expected_event_type="motion_detected",
            window_start="07:00",
            window_end="09:00",
            timezone="America/Chicago",
            days=["mon", "tue", "wed", "thu", "fri"],
            escalation_delay_minutes=30,
            snapshot_device_id=args.camera if hasattr(args, "camera") else None,
        )
        routine_obj.routine_id = "rt_medicine"

    # Set up Ring client
    ring_client = None
    token = os.environ.get("RING_ACCESS_TOKEN") or os.environ.get("RING_TOKEN")
    if token and token.strip() and not args.no_photo:
        ring_client = RingClient(access_token=token.strip())

    # Set up Notifier
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id and tg_token.strip() and tg_chat_id.strip():
        notifier = TelegramNotifier(
            bot_token=tg_token.strip(),
            chat_id=tg_chat_id.strip(),
            ring_client=ring_client,
        )
        target_name = f"Telegram (chat_id: {tg_chat_id})"
    else:
        notifier = ConsoleNotifier(ring_client=ring_client)
        target_name = "Console (stdout)"

    print("=" * 80)
    print("THRESHOLD TEST ALERT DISPATCH")
    print("=" * 80)
    print(f"Target:        {target_name}")
    print(f"Routine:       {routine_obj.name} (id: {routine_obj.routine_id})")
    print(f"Sensor Device: {routine_obj.device_id}")
    print(f"Camera Device: {routine_obj.snapshot_device_id or '(none)'}")
    print("-" * 80)

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    local_date = now_utc.strftime("%Y-%m-%d")

    try:
        res = notifier.send_escalation(
            routine=routine_obj,
            local_date=local_date,
            fired_at=now_iso,
        )
        print("\n[OK] Test alert successfully dispatched!")
        if isinstance(res, dict):
            print(f"Channel:       {res.get('channel')}")
            print(f"Alert Type:    {res.get('type')}")
            if res.get("snapshot_path"):
                print(f"Saved Image:   {res.get('snapshot_path')}")
        return 0
    except Exception as exc:
        print(f"\n[FAIL] Test alert delivery failed: {exc}", file=sys.stderr)
        return 1


def run_dashboard_cli(args: argparse.Namespace) -> int:
    """Run standalone Threshold local web dashboard server."""
    from src.threshold.dashboard import run_dashboard_command

    load_env(".env")
    config_path = find_config_file(args.config)
    household = args.household or "Mom's House — Dallas"
    tz_name = args.timezone or "America/Chicago"

    if config_path:
        try:
            cfg = load_config_file(config_path)
            household = args.household or cfg.household
            tz_name = args.timezone or cfg.timezone
        except Exception as exc:
            print(f"[WARN] Failed loading config file: {exc}", file=sys.stderr)

    return run_dashboard_command(
        db_path=args.db,
        port=args.port,
        host=args.host,
        household_name=household,
        timezone_name=tz_name,
        snapshot_dir=args.snapshot_dir,
        open_browser=getattr(args, "open", False),
    )


def run_demo_cli(args: argparse.Namespace) -> int:
    """Execute the end-to-end 3-beat hackathon demonstration."""
    import time
    from src.threshold.demo import run_demo

    load_env(".env")
    speed = 0.0 if args.fast else args.speed
    try:
        res = run_demo(
            db_path=args.db,
            dashboard_port=args.port,
            dashboard_host=args.host,
            enable_dashboard=not args.no_dashboard,
            open_browser=getattr(args, "open", False),
            interactive=args.interactive,
            speed=speed,
            live_telegram=getattr(args, "telegram", False),
            cleanup=False,
        )
        if not args.no_dashboard:
            if args.interactive or getattr(args, "keep_running", False):
                try:
                    while True:
                        time.sleep(1.0)
                except KeyboardInterrupt:
                    print("\nDemo stopped cleanly.")
            else:
                if speed > 0:
                    time.sleep(1.5)
        return 0 if res["success"] else 1
    except KeyboardInterrupt:
        print("\nDemo aborted by user.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m src.threshold",
        description="Threshold Command-Line Tools & Diagnostics",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # init
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new threshold.json configuration file from template",
    )
    init_parser.add_argument("--target", "-t", default="threshold.json", help="Destination config file path (default: threshold.json)")
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing configuration file")

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
    seed_parser.add_argument("--purge", action="store_true", help="Delete existing routines in DB that are not present in config")

    # test-alert
    alert_parser = subparsers.add_parser(
        "test-alert",
        help="Dispatch a test escalation alert (with live snapshot if available) to Telegram or Console",
    )
    alert_parser.add_argument("--config", "-c", default=None, help="Path to JSON config file (default: threshold.json)")
    alert_parser.add_argument("--routine", "-r", default=None, help="Specific routine ID to test (default: first routine)")
    alert_parser.add_argument("--camera", default=None, help="Explicit snapshot camera device ID override")
    alert_parser.add_argument("--no-photo", action="store_true", help="Force text-only alert (disable snapshot)")

    # dashboard
    dash_parser = subparsers.add_parser(
        "dashboard",
        help="Launch the Threshold local web status dashboard",
    )
    dash_parser.add_argument("--config", "-c", default=None, help="Path to JSON config file (default: threshold.json)")
    dash_parser.add_argument("--db", default="threshold.db", help="Path to SQLite database (default: threshold.db)")
    dash_parser.add_argument("--port", "-p", type=int, default=8080, help="HTTP port to bind (default: 8080)")
    dash_parser.add_argument("--host", default="0.0.0.0", help="HTTP host to bind (default: 0.0.0.0)")
    dash_parser.add_argument("--household", default=None, help="Household display name")
    dash_parser.add_argument("--timezone", default=None, help="Household timezone name")
    dash_parser.add_argument("--snapshot-dir", default="snapshots", help="Directory where snapshots are saved (default: snapshots)")
    dash_parser.add_argument("--open", "-o", action="store_true", help="Automatically open dashboard in default browser")

    # demo
    demo_parser = subparsers.add_parser(
        "demo",
        help="Run the end-to-end 3-beat interactive or automated hackathon demo",
    )
    demo_parser.add_argument("--interactive", "-i", action="store_true", help="Prompt user between narrative beats")
    demo_parser.add_argument("--fast", "-f", action="store_true", help="Run without artificial pacing delays")
    demo_parser.add_argument("--speed", type=float, default=1.0, help="Pacing speed multiplier (default: 1.0)")
    demo_parser.add_argument("--port", "-p", type=int, default=8080, help="Dashboard HTTP port (default: 8080)")
    demo_parser.add_argument("--host", default="0.0.0.0", help="Dashboard HTTP host (default: 0.0.0.0)")
    demo_parser.add_argument("--open", "-o", action="store_true", help="Open web dashboard in default browser")
    demo_parser.add_argument("--no-dashboard", action="store_true", help="Disable embedded web status dashboard")
    demo_parser.add_argument("--telegram", action="store_true", help="Send live Telegram photo alert if configured in .env")
    demo_parser.add_argument("--db", default=None, help="Path to demo SQLite database (default: demo_threshold.db)")
    demo_parser.add_argument("--keep-running", action="store_true", help="Keep dashboard running after demo concludes")

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
    run_parser.add_argument("--no-dashboard", action="store_true", help="Disable embedded dashboard web server")
    run_parser.add_argument("--dashboard-port", type=int, default=8080, help="Web dashboard port (default: 8080)")
    run_parser.add_argument("--dashboard-host", default="0.0.0.0", help="Web dashboard host (default: 0.0.0.0)")
    run_parser.add_argument("--once", action="store_true", help="Execute single cycle and exit")

    args = parser.parse_args()

    if args.subcommand == "init":
        return run_init_command(args)
    elif args.subcommand == "ring-check":
        return run_ring_check()
    elif args.subcommand == "seed":
        return run_seed_command(args)
    elif args.subcommand == "test-alert":
        return run_test_alert_command(args)
    elif args.subcommand == "dashboard":
        return run_dashboard_cli(args)
    elif args.subcommand == "demo":
        return run_demo_cli(args)
    elif args.subcommand == "run":
        return run_daemon_command(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
