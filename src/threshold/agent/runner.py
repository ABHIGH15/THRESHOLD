"""
Agent execution runner integrated into the Threshold daemon.
Scans new events via cursor, filters ambiguous events, executes investigations, and gates escalations.
"""

from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

from src.threshold.agent.investigate import run_investigation_and_gate
from src.threshold.agent.provider import MockProvider
from src.threshold.agent.schemas import Investigation
from src.threshold.agent.triggers import is_ambiguous_event
from src.threshold.notifier import format_snapshot_caption_prefix, truncate_caption
from src.threshold.store import (
    get_app_state,
    list_routines,
    row_to_routine,
    save_investigation,
    set_app_state,
    to_iso_timestamp,
)

logger = logging.getLogger("threshold.agent.runner")


def format_agent_notification_text(
    event: Dict[str, Any],
    investigation: Investigation,
    timezone_name: str = "America/Chicago",
) -> str:
    """
    Format agent notification message according to specification:
    - Header: CHECK NEEDED
    - Event time in local timezone
    - Evidence list: ✓/✗ <tool text>
    - Recommendation prefixed with mode label
    """
    tz = ZoneInfo(timezone_name)
    occ_str = event.get("occurred_at", "")
    if occ_str:
        clean_str = occ_str.replace("Z", "+00:00")
        occ_dt = datetime.fromisoformat(clean_str)
        if occ_dt.tzinfo is None:
            occ_dt = occ_dt.replace(tzinfo=timezone.utc)
        local_occ = occ_dt.astimezone(tz)
        time_formatted = local_occ.strftime("%Y-%m-%d %H:%M %Z")
    else:
        time_formatted = "Unknown"

    evidence_lines = []
    for item in investigation.evidence:
        mark = "✓" if item.ok else "✗"
        evidence_lines.append(f"{mark} {item.text}")
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "None recorded."

    mode_label = f"({investigation.mode})"
    rec_text = investigation.recommendation
    if not rec_text.startswith(mode_label) and not rec_text.startswith("(replay"):
        rec_text = f"{mode_label} {rec_text}"

    text = (
        f"<b>CHECK NEEDED</b>\n\n"
        f"<b>Event Time:</b> {time_formatted}\n"
        f"<b>Device:</b> {event.get('device_id', 'Unknown')}\n"
        f"<b>Event Type:</b> {event.get('event_type', 'Unknown')}\n\n"
        f"<b>Evidence:</b>\n{evidence_block}\n\n"
        f"<b>Recommendation:</b>\n{rec_text}"
    )
    return text


def deliver_agent_notification(
    notifier: Any,
    event: Dict[str, Any],
    investigation: Investigation,
    now: datetime,
    timezone_name: str = "America/Chicago",
) -> bool:
    """Deliver notification through existing notifier path without modifying absence alert logic."""
    if notifier is None:
        return False

    message_text = format_agent_notification_text(event, investigation, timezone_name)

    # Check if a snapshot was retrieved during investigation
    snapshot_path = None
    captured_at_iso = None
    for item in investigation.evidence:
        if item.tool == "pull_snapshot" and item.ok:
            for step in investigation.trace:
                if step.get("type") == "result" and step.get("tool") == "pull_snapshot" and step.get("ok"):
                    res_data = step.get("data") or {}
                    if isinstance(res_data, dict) and res_data.get("path"):
                        snapshot_path = res_data.get("path")
                        captured_at_iso = res_data.get("captured_at")
                        break

    photo_bytes = None
    if snapshot_path and os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, "rb") as f:
                photo_bytes = f.read()
        except Exception as e:
            logger.warning("Could not read snapshot file %s: %s", snapshot_path, e)

    delivered = False
    if photo_bytes and hasattr(notifier, "send_photo") and callable(getattr(notifier, "send_photo")):
        captured_ms = None
        if captured_at_iso:
            try:
                c_clean = captured_at_iso.replace("Z", "+00:00")
                c_dt = datetime.fromisoformat(c_clean)
                captured_ms = int(c_dt.timestamp() * 1000)
            except Exception:
                pass
        caption_prefix = format_snapshot_caption_prefix(captured_ms, timezone_name, now=now)
        full_caption = truncate_caption(f"{caption_prefix}\n\n{message_text}", max_chars=1024)

        try:
            notifier.send_photo(photo_bytes=photo_bytes, caption=full_caption)
            delivered = True
        except Exception as e:
            logger.warning("send_photo failed for agent alert; falling back to sendMessage: %s", e)

    if not delivered:
        if hasattr(notifier, "_make_request") and callable(getattr(notifier, "_make_request")):
            payload = {
                "chat_id": getattr(notifier, "chat_id", ""),
                "text": message_text,
                "parse_mode": "HTML",
            }
            notifier._make_request("sendMessage", payload)
            delivered = True
        elif hasattr(notifier, "send_message") and callable(getattr(notifier, "send_message")):
            notifier.send_message(message_text)
            delivered = True
        elif hasattr(notifier, "sent") and isinstance(getattr(notifier, "sent"), list):
            # For RecordingNotifier or test doubles
            notifier.sent.append({
                "type": "agent_check_needed",
                "text": message_text,
                "photo_bytes": photo_bytes,
                "event": event,
            })
            delivered = True

    return delivered


def process_new_events(
    conn: sqlite3.Connection,
    provider_factory: Optional[Union[Callable[[], Any], Any]],
    now: datetime,
    cfg: Optional[Union[Dict[str, Any], Any]] = None,
    notifier: Optional[Any] = None,
    ring_client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Process newly ingested events for ambiguity and autonomous investigation:
    1. Read 'agent_event_cursor' from app_state (default 0).
    2. Query events where rowid > cursor limit cfg.max_per_tick (default 3).
    3. For each event:
       - evaluate is_ambiguous_event()
       - if ambiguous: run investigation, gate decision, save record
       - if decision == 'escalate' and notify_enabled: send notification
       - advance cursor after EACH event
    4. Guard with per-tick timeout and catch exceptions per event.
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # Config extraction
    if cfg is None:
        cfg_dict: Dict[str, Any] = {}
    elif isinstance(cfg, dict):
        cfg_dict = cfg
    else:
        cfg_dict = getattr(cfg, "__dict__", {})

    enabled = cfg_dict.get("enabled", False)
    if not enabled:
        return []

    max_per_tick = int(cfg_dict.get("max_per_tick", 3))
    notify_enabled = bool(cfg_dict.get("notify_enabled", False))
    max_seconds = float(cfg_dict.get("max_seconds_per_tick", 25.0))
    trace_file = cfg_dict.get("trace_file", "trace_escalate_two_tools.json")

    start_mono = time.monotonic()
    deadline_mono = start_mono + max_seconds

    cursor_str = get_app_state(conn, "agent_event_cursor")
    if cursor_str is None:
        # First run guard: initialize cursor to current max rowid in events table to prevent walking past history
        cur_max = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
        max_rowid = cur_max[0] if cur_max and cur_max[0] is not None else 0
        set_app_state(conn, "agent_event_cursor", str(max_rowid))
        logger.info("Initialized agent_event_cursor on first run to rowid %d", max_rowid)
        return []

    try:
        cursor = int(cursor_str)
    except ValueError:
        cursor = 0

    max_event_age = int(cfg_dict.get("max_event_age_minutes", 120))

    cur = conn.execute(
        """
        SELECT rowid, source, external_id, device_id, event_type, sub_type, occurred_at, received_at
        FROM events
        WHERE rowid > ?
        ORDER BY rowid ASC
        LIMIT ?
        """,
        (cursor, max_per_tick),
    )
    rows = cur.fetchall()
    if not rows:
        return []

    processed = []

    for row in rows:
        if time.monotonic() >= deadline_mono:
            logger.warning("Agent tick deadline reached; deferring remaining events.")
            break

        rowid = row["rowid"]
        event = dict(row)
        event_key = event.get("external_id") or f"evt_{rowid}"
        event["event_key"] = event_key

        # Guard against stale events older than max_event_age_minutes
        occ_str = event.get("occurred_at")
        if occ_str:
            clean_occ = occ_str.replace("Z", "+00:00")
            occ_dt = datetime.fromisoformat(clean_occ)
            if occ_dt.tzinfo is None:
                occ_dt = occ_dt.replace(tzinfo=timezone.utc)
            age_seconds = (now_utc - occ_dt).total_seconds()
            if age_seconds > max_event_age * 60:
                logger.info(
                    "Event %s exceeds max_event_age_minutes (%dm > %dm); skipping.",
                    event_key,
                    int(age_seconds / 60),
                    max_event_age,
                )
                set_app_state(conn, "agent_event_cursor", str(rowid))
                processed.append({
                    "rowid": rowid,
                    "event_key": event_key,
                    "ambiguous": False,
                    "skipped_reason": "stale_event",
                })
                continue

        try:
            reason = is_ambiguous_event(conn, event, now_utc, cfg=cfg_dict)
            if reason is not None:
                logger.info("Ambiguous event detected (%s) for device %s", reason, event.get("device_id"))

                # Resolve provider
                if callable(provider_factory):
                    provider = provider_factory()
                elif provider_factory is not None:
                    provider = provider_factory
                else:
                    provider = MockProvider(trace_file)

                # Find timezone for device from routine if available
                dev_routines = list_routines(conn)
                tz_name = "America/Chicago"
                for r_row in dev_routines:
                    if r_row["device_id"] == event.get("device_id"):
                        tz_name = r_row["timezone"]
                        break
                event["timezone"] = tz_name

                tools_dict = {"ring_client": ring_client}
                snap_dir = cfg_dict.get("snapshots_dir") or (getattr(cfg, "snapshots_dir", None) if cfg else None)
                if snap_dir:
                    tools_dict["snapshots_dir"] = snap_dir

                inv, gate_decision = run_investigation_and_gate(
                    conn=conn,
                    event=event,
                    provider=provider,
                    tools=tools_dict,
                    now=now_utc,
                    save_to_db=True,
                )

                notified = False
                if gate_decision.decision == "escalate" and notify_enabled and notifier is not None:
                    delivered = deliver_agent_notification(
                        notifier=notifier,
                        event=event,
                        investigation=inv,
                        now=now_utc,
                        timezone_name=tz_name,
                    )
                    if delivered:
                        notified = True
                        now_iso = to_iso_timestamp(now_utc)
                        conn.execute(
                            "UPDATE investigations SET notified_at = ? WHERE event_key = ?",
                            (now_iso, inv.event_key),
                        )
                        conn.commit()

                processed.append({
                    "rowid": rowid,
                    "event_key": event_key,
                    "ambiguous": True,
                    "tier": inv.tier,
                    "decision": gate_decision.decision,
                    "reason_code": gate_decision.reason_code,
                    "notified": notified,
                })
            else:
                processed.append({
                    "rowid": rowid,
                    "event_key": event_key,
                    "ambiguous": False,
                })

            # Advance cursor after EACH event
            set_app_state(conn, "agent_event_cursor", str(rowid))

        except Exception as exc:
            logger.error("Exception during agent investigation of event %s: %s", event_key, exc, exc_info=True)
            # Save fallback investigation record
            try:
                save_investigation(
                    conn=conn,
                    event_key=event_key,
                    device_id=event.get("device_id", "unknown"),
                    created_at=now_utc,
                    mode="fallback",
                    provider_name="error",
                    tier="check_needed",
                    decision="check_needed",
                    reason_code="error",
                    recommendation="could not determine",
                )
            except Exception:
                pass
            # Always advance cursor so failing event does not block queue
            set_app_state(conn, "agent_event_cursor", str(rowid))
            processed.append({
                "rowid": rowid,
                "event_key": event_key,
                "error": str(exc),
            })

    return processed
