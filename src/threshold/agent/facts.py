"""
Fact builder for Gate evaluation.
Queries local SQLite database and reuses scheduler DeviceHealthRecord.
"""

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Optional

from src.threshold.agent.schemas import GateFacts
from src.threshold.scheduler import build_device_health_record
from src.threshold.store import get_device_health


def build_gate_facts(
    conn: sqlite3.Connection,
    device_id: str,
    now: datetime,
) -> GateFacts:
    """
    Build ground-truth factual context for Gate evaluation:
    - device_online_fresh: reuses build_device_health_record and is_stale, requires online
    - routine_snoozed: checks if any routine for device is snoozed as of now
    - minutes_since_last_notification: latest from escalations and investigations
    - agent_escalations_last_6h: count of investigations with decision 'escalate' in last 6h
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # 1. Device online and fresh
    health = build_device_health_record(conn=conn, device_id=device_id, now=now_utc)
    health_row = get_device_health(conn, device_id)
    is_online = bool(health_row["online"]) if health_row else False
    device_online_fresh = bool(is_online and not health.is_stale(now_utc))

    # 2. Routine snoozed
    cur = conn.execute(
        "SELECT snoozed_until FROM routines WHERE device_id = ? AND snoozed_until IS NOT NULL",
        (device_id,),
    )
    routine_snoozed = False
    for row in cur.fetchall():
        snooze_str = row[0]
        if snooze_str:
            clean_str = snooze_str.replace("Z", "+00:00")
            snooze_dt = datetime.fromisoformat(clean_str)
            if snooze_dt.tzinfo is None:
                snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
            if snooze_dt > now_utc:
                routine_snoozed = True
                break

    # 3. Minutes since last notification (across escalations and investigations)
    cur_esc = conn.execute("SELECT MAX(notified_at) FROM escalations WHERE notified_at IS NOT NULL")
    latest_esc_str = cur_esc.fetchone()[0]

    cur_inv = conn.execute("SELECT MAX(notified_at) FROM investigations WHERE notified_at IS NOT NULL")
    latest_inv_str = cur_inv.fetchone()[0]

    latest_notified_dt: Optional[datetime] = None
    for n_str in (latest_esc_str, latest_inv_str):
        if n_str:
            clean_str = n_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if latest_notified_dt is None or dt > latest_notified_dt:
                latest_notified_dt = dt

    minutes_since: Optional[float] = None
    if latest_notified_dt is not None:
        diff_seconds = (now_utc - latest_notified_dt).total_seconds()
        minutes_since = max(0.0, diff_seconds / 60.0)

    # 4. Agent escalations in last 6 hours
    cutoff_6h = (now_utc - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur_6h = conn.execute(
        "SELECT COUNT(*) FROM investigations WHERE decision = 'escalate' AND created_at >= ?",
        (cutoff_6h,),
    )
    agent_escalations_last_6h = cur_6h.fetchone()[0] or 0

    return GateFacts(
        device_online_fresh=device_online_fresh,
        routine_snoozed=routine_snoozed,
        minutes_since_last_notification=minutes_since,
        agent_escalations_last_6h=agent_escalations_last_6h,
    )
