"""
Threshold Declarative Configuration Module (src/threshold/config.py).

Provides declarative JSON configuration loading, strict validation,
routine seeding into SQLite, Ring device ID verification, and template initialization.
Zero external dependencies (pure Python standard library).
"""

from dataclasses import dataclass, field
import json
import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.threshold.ring_client import RingClient
from src.threshold.store import list_routines, save_routine

logger = logging.getLogger(__name__)

VALID_DAYS: Set[str] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def expand_env_vars(val: Any) -> Any:
    """Recursively expand environment variables (e.g. ${RING_DEV_ID}) in nested structures."""
    if isinstance(val, str):
        return os.path.expandvars(val)
    if isinstance(val, list):
        return [expand_env_vars(item) for item in val]
    if isinstance(val, dict):
        return {k: expand_env_vars(v) for k, v in val.items()}
    return val


@dataclass
class RoutineConfig:
    """Declarative routine configuration item."""

    id: str
    name: str
    device_id: str
    window_start: str
    window_end: str
    property_id: str = "prop_home"
    expected_event_type: str = "motion"
    expected_sub_type: Optional[str] = None
    days: List[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    escalation_delay_minutes: int = 30
    snapshot_device_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "property_id": self.property_id,
            "device_id": self.device_id,
            "expected_event_type": self.expected_event_type,
            "expected_sub_type": self.expected_sub_type,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "days": self.days,
            "escalation_delay_minutes": self.escalation_delay_minutes,
            "snapshot_device_id": self.snapshot_device_id,
        }


@dataclass
class ThresholdConfig:
    """Top-level configuration for a Threshold household instance."""

    household: str = "Threshold Household"
    timezone: str = "America/Chicago"
    poll_interval_seconds: float = 300.0
    tick_interval_seconds: float = 60.0
    callback_interval_seconds: float = 2.0
    dashboard_port: int = 8080
    snapshot_lookback_hours: int = 24
    snapshot_deadline_seconds: float = 5.0
    routines: List[RoutineConfig] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dictionary."""
        return {
            "household": self.household,
            "timezone": self.timezone,
            "poll_interval_seconds": self.poll_interval_seconds,
            "tick_interval_seconds": self.tick_interval_seconds,
            "callback_interval_seconds": self.callback_interval_seconds,
            "dashboard_port": self.dashboard_port,
            "snapshot_lookback_hours": self.snapshot_lookback_hours,
            "snapshot_deadline_seconds": self.snapshot_deadline_seconds,
            "routines": [r.to_dict() for r in self.routines],
        }


def validate_time_format(val: str, field_name: str) -> None:
    """Validate that val matches strict 24-hour HH:MM format (00:00 - 23:59)."""
    if not isinstance(val, str) or not TIME_RE.match(val):
        raise ValueError(f"Invalid {field_name} format '{val}': expected 24-hour HH:MM (e.g., '07:30', '21:00')")


def validate_config(config: ThresholdConfig) -> List[str]:
    """
    Validate ThresholdConfig structure and constraints.
    Raises ValueError on fatal schema errors.
    Returns list of non-fatal warnings.
    """
    warnings: List[str] = []

    if not config.household or not config.household.strip():
        raise ValueError("Config error: 'household' name cannot be empty")

    # Timezone validation
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, Exception):
        raise ValueError(f"Config error: Unknown or invalid timezone '{config.timezone}'")

    if config.poll_interval_seconds <= 0:
        raise ValueError("Config error: 'poll_interval_seconds' must be positive")
    if config.tick_interval_seconds <= 0:
        raise ValueError("Config error: 'tick_interval_seconds' must be positive")

    seen_routine_ids: Set[str] = set()

    for idx, r in enumerate(config.routines, start=1):
        if not r.id or not r.id.strip():
            raise ValueError(f"Routine #{idx}: 'id' cannot be empty")
        if r.id in seen_routine_ids:
            raise ValueError(f"Routine #{idx}: Duplicate routine id '{r.id}' detected")
        seen_routine_ids.add(r.id)

        if not r.name or not r.name.strip():
            raise ValueError(f"Routine '{r.id}': 'name' cannot be empty")
        if not r.device_id or not r.device_id.strip():
            raise ValueError(f"Routine '{r.id}': 'device_id' cannot be empty")

        validate_time_format(r.window_start, f"routine '{r.id}' window_start")
        validate_time_format(r.window_end, f"routine '{r.id}' window_end")

        # Zero-duration window check
        if r.window_start == r.window_end:
            raise ValueError(
                f"Routine '{r.id}': window_start and window_end cannot be identical ('{r.window_start}') — zero-duration window"
            )

        if r.escalation_delay_minutes < 0:
            raise ValueError(f"Routine '{r.id}': 'escalation_delay_minutes' cannot be negative")

        if not r.days:
            raise ValueError(f"Routine '{r.id}': 'days' list cannot be empty")
        for d in r.days:
            d_lower = d.lower().strip()
            if d_lower not in VALID_DAYS:
                raise ValueError(f"Routine '{r.id}': Invalid day '{d}'. Must be one of: {sorted(list(VALID_DAYS))}")

    return warnings


def load_config_from_dict(raw_data: Dict[str, Any]) -> ThresholdConfig:
    """Construct and validate ThresholdConfig from a dictionary with env var expansion."""
    data = expand_env_vars(raw_data)
    routines_raw = data.get("routines", [])
    routines: List[RoutineConfig] = []

    for r_dict in routines_raw:
        if not isinstance(r_dict, dict):
            continue
        days = r_dict.get("days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        if isinstance(days, str):
            days = [d.strip().lower() for d in days.split(",") if d.strip()]
        else:
            days = [d.strip().lower() for d in days]

        r_obj = RoutineConfig(
            id=str(r_dict.get("id", "")).strip(),
            name=str(r_dict.get("name", "")).strip(),
            property_id=str(r_dict.get("property_id", "prop_home")).strip(),
            device_id=str(r_dict.get("device_id", "")).strip(),
            expected_event_type=str(r_dict.get("expected_event_type", "motion")).strip(),
            expected_sub_type=r_dict.get("expected_sub_type"),
            window_start=str(r_dict.get("window_start", "")).strip(),
            window_end=str(r_dict.get("window_end", "")).strip(),
            days=days,
            escalation_delay_minutes=int(r_dict.get("escalation_delay_minutes", 30)),
            snapshot_device_id=r_dict.get("snapshot_device_id"),
        )
        routines.append(r_obj)

    raw_lookback = data.get("snapshot_lookback_hours", 24)
    try:
        snapshot_lookback = max(1, min(24, int(raw_lookback)))
    except (ValueError, TypeError):
        snapshot_lookback = 24

    raw_deadline = data.get("snapshot_deadline_seconds", 5.0)
    try:
        snapshot_deadline = max(0.5, float(raw_deadline))
    except (ValueError, TypeError):
        snapshot_deadline = 5.0

    config = ThresholdConfig(
        household=str(data.get("household", "Threshold Household")).strip(),
        timezone=str(data.get("timezone", "America/Chicago")).strip(),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 300.0)),
        tick_interval_seconds=float(data.get("tick_interval_seconds", 60.0)),
        callback_interval_seconds=float(data.get("callback_interval_seconds", 2.0)),
        dashboard_port=int(data.get("dashboard_port", 8080)),
        snapshot_lookback_hours=snapshot_lookback,
        snapshot_deadline_seconds=snapshot_deadline,
        routines=routines,
    )

    validate_config(config)
    return config


def load_config_file(filepath: str) -> ThresholdConfig:
    """Load and parse JSON configuration file."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Configuration file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in configuration file '{filepath}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Root of configuration file '{filepath}' must be a JSON object")

    return load_config_from_dict(data)


def find_config_file(explicit_path: Optional[str] = None) -> Optional[str]:
    """
    Resolve configuration file path:
    1. explicit_path if provided (must exist).
    2. 'threshold.json' in current working directory.
    3. None if not found.
    """
    if explicit_path:
        if not os.path.exists(explicit_path):
            raise FileNotFoundError(f"Specified configuration file does not exist: {explicit_path}")
        return explicit_path

    if os.path.exists("threshold.json"):
        return "threshold.json"

    return None


def seed_routines_from_config(
    conn: sqlite3.Connection,
    config: ThresholdConfig,
    client: Optional[RingClient] = None,
    purge_unlisted: bool = False,
) -> Dict[str, Any]:
    """
    Seed or update all routines in SQLite database from ThresholdConfig:
    - Preserves last_satisfied_at and snoozed_until facts if routine already exists.
    - If purge_unlisted=True, deletes routines from SQLite that are not in the config.
    - Optionally queries RingClient to verify device existence and online status.
    """
    warnings: List[str] = []
    verified_devices: Dict[str, Dict[str, Any]] = {}

    if client:
        try:
            devices = client.list_devices()
            for dev in devices:
                dev_id = dev.get("id")
                if not dev_id:
                    continue
                attrs = dev.get("attributes", {})
                dev_name = attrs.get("name", "Ring Device")
                verified_devices[dev_id] = {
                    "id": dev_id,
                    "name": dev_name,
                    "online": None,
                }
                # Check status
                try:
                    status_obj = client.get_device_status(dev_id)
                    s_attrs = status_obj.get("attributes", {})
                    verified_devices[dev_id]["online"] = bool(s_attrs.get("online", False))
                except Exception:
                    pass
        except Exception as e:
            warnings.append(f"Could not verify devices against Ring API: {e}")

    seeded_routines: List[str] = []

    for r in config.routines:
        if verified_devices:
            if r.device_id not in verified_devices:
                warnings.append(f"Routine '{r.id}': device_id '{r.device_id}' was not found in discovered Ring devices")
            if r.snapshot_device_id and r.snapshot_device_id not in verified_devices:
                warnings.append(f"Routine '{r.id}': snapshot_device_id '{r.snapshot_device_id}' was not found in discovered Ring devices")

        save_routine(
            conn=conn,
            routine_id=r.id,
            property_id=r.property_id,
            name=r.name,
            device_id=r.device_id,
            expected_event_type=r.expected_event_type,
            expected_sub_type=r.expected_sub_type,
            window_start=r.window_start,
            window_end=r.window_end,
            timezone_name=config.timezone,
            days=r.days,
            escalation_delay_minutes=r.escalation_delay_minutes,
            snapshot_device_id=r.snapshot_device_id,
        )
        seeded_routines.append(r.id)

    # Purge unlisted routines if requested
    purged_routines: List[str] = []
    if purge_unlisted:
        existing = [row["routine_id"] for row in list_routines(conn)]
        target_ids = set(seeded_routines)
        for old_id in existing:
            if old_id not in target_ids:
                conn.execute("DELETE FROM routines WHERE routine_id = ?", (old_id,))
                purged_routines.append(old_id)

    conn.commit()

    return {
        "seeded_count": len(seeded_routines),
        "purged_count": len(purged_routines),
        "purged_routines": purged_routines,
        "routines": seeded_routines,
        "warnings": warnings,
        "verified_devices": verified_devices,
    }
