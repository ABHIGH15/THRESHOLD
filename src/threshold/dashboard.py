"""
Threshold Local Web Dashboard (src/threshold/dashboard.py).

Provides a local, zero-cloud web command center for caretakers and family members:
1. Pure Python standard library only (http.server.ThreadingHTTPServer). Zero third-party dependencies.
2. Server-side rendered HTML/CSS status page with auto-refresh (<meta http-equiv="refresh" content="10">).
3. Routine status cards with live visual badges (🟢 OBSERVED, 🟡 WATCHING, 🔴 MISSED, ⚪ SNOOZED, ⚠️ UNKNOWN).
4. Direct snapshot thumbnail previews from archived JPEG captures (/snapshots/<filename>) + fullscreen modal lightbox.
5. Device health metrics, today's event timeline, and escalation audit log with caretaker feedback tracking.
6. Read-only JSON endpoint (GET /api/status, GET /api/routines) with query filtering (?status=, ?routine_id=).
7. Path-traversal protected static file serving for local camera snapshots with security headers.
8. Inline SVG favicon (/favicon.ico, /favicon.svg) and address-reuse socket handling.
"""

from datetime import datetime, timezone
import html
import http.server
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
from zoneinfo import ZoneInfo

from src.threshold.notifier import detect_image_mime
from src.threshold.scheduler import build_device_health_record, row_to_routine
from src.threshold.status import (
    STATUS_MISSED,
    STATUS_OBSERVED,
    STATUS_SNOOZED,
    STATUS_UNKNOWN,
    STATUS_WATCHING,
    derive_status,
)
from src.threshold.store import (
    get_app_state,
    get_connection,
    get_escalation,
    list_corrections,
    list_device_health,
    list_investigations,
    list_recent_escalations,
    list_recent_events,
    list_routines,
)

logger = logging.getLogger(__name__)

FAVICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#231f1b"/>
  <path d="M18 48 L18 20 Q32 10 46 20 L46 48 Z" fill="none" stroke="#d4a04a" stroke-width="4" stroke-linejoin="round"/>
  <line x1="12" y1="48" x2="52" y2="48" stroke="#d4695a" stroke-width="4" stroke-linecap="round"/>
  <circle cx="32" cy="32" r="5" fill="#6db88f"/>
</svg>"""

STATUS_CONFIG = {
    STATUS_OBSERVED: {
        "label": "OBSERVED",
        "description": "Routine Satisfied",
        "color": "#6db88f",
        "bg": "rgba(109, 184, 143, 0.15)",
        "border": "#3a5c48",
        "badge_icon": "✓",
    },
    STATUS_WATCHING: {
        "label": "WATCHING",
        "description": "Window Open / Waiting",
        "color": "#d4a04a",
        "bg": "rgba(212, 160, 74, 0.15)",
        "border": "#5c4a28",
        "badge_icon": "●",
    },
    STATUS_MISSED: {
        "label": "MISSED",
        "description": "Window Missed — Alert Fired",
        "color": "#d4695a",
        "bg": "rgba(212, 105, 90, 0.18)",
        "border": "#5c302a",
        "badge_icon": "🚨",
    },
    STATUS_SNOOZED: {
        "label": "SNOOZED",
        "description": "Temporarily Snoozed",
        "color": "#5a8fd4",
        "bg": "rgba(90, 143, 212, 0.15)",
        "border": "#2a4263",
        "badge_icon": "⏸",
    },
    STATUS_UNKNOWN: {
        "label": "UNKNOWN",
        "description": "Unknown — device offline, not escalated",
        "color": "#e69542",
        "bg": "rgba(230, 149, 66, 0.15)",
        "border": "#633c17",
        "badge_icon": "⚠️",
    },
}


def format_relative_time(iso_str: Optional[str], now_dt: Optional[datetime] = None) -> str:
    """Format an ISO timestamp string into a friendly relative description."""
    if not iso_str:
        return "Never"
    try:
        clean = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if not now_dt:
            now_dt = datetime.now(timezone.utc)
        diff = now_dt - dt
        secs = int(diff.total_seconds())
        if secs < 0:
            return "Just now"
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h ago"
        days = hrs // 24
        return f"{days}d ago"
    except Exception:
        return iso_str


def get_dashboard_data(
    conn: sqlite3.Connection,
    household_name: str = "Mom's House — Dallas",
    timezone_name: str = "America/Chicago",
    snapshot_dir: str = "snapshots",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Collect comprehensive household monitoring state from SQLite.
    Computes real-time routine statuses without mutating database state.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
        timezone_name = "UTC"

    local_now = now.astimezone(tz)
    local_time_str = local_now.strftime("%I:%M %p %Z")
    local_date_str = local_now.strftime("%A, %b %d, %Y")
    today_date_iso = local_now.strftime("%Y-%m-%d")

    # App state & daemon metrics
    daemon_started = get_app_state(conn, "daemon_started_at")
    daemon_status = get_app_state(conn, "daemon_status", default="Active")
    daemon_last_tick = get_app_state(conn, "daemon_last_tick_at")
    daemon_ticks = int(get_app_state(conn, "daemon_tick_count", default="0") or 0)
    last_poll = get_app_state(conn, "last_poll_at")

    # Query routines and calculate current derived statuses
    routine_rows = list_routines(conn)
    routines_data = []
    observed_count = 0
    watching_count = 0
    missed_count = 0
    snoozed_count = 0
    unknown_count = 0

    for r_row in routine_rows:
        r_id = r_row["routine_id"]
        routine_obj = row_to_routine(r_row)
        health_rec = build_device_health_record(conn, routine_obj.device_id, now=now)
        status = derive_status(routine_obj, health_rec, now)

        if status == STATUS_OBSERVED:
            observed_count += 1
        elif status == STATUS_WATCHING:
            watching_count += 1
        elif status == STATUS_MISSED:
            missed_count += 1
        elif status == STATUS_SNOOZED:
            snoozed_count += 1
        elif status == STATUS_UNKNOWN:
            unknown_count += 1

        cfg = STATUS_CONFIG.get(status, STATUS_CONFIG[STATUS_UNKNOWN])

        # Check for latest local snapshot
        latest_snap_rel = None
        latest_snap_file = f"{r_id}_latest.jpg"
        dated_snap_file = f"{r_id}_{today_date_iso}.jpg"
        if os.path.exists(os.path.join(snapshot_dir, latest_snap_file)):
            latest_snap_rel = f"/snapshots/{latest_snap_file}"
        elif os.path.exists(os.path.join(snapshot_dir, dated_snap_file)):
            latest_snap_rel = f"/snapshots/{dated_snap_file}"

        last_sat_iso = r_row.get("last_satisfied_at")
        last_sat_friendly = format_relative_time(last_sat_iso, now)

        days_val = r_row.get("days", "[]")
        if isinstance(days_val, str):
            try:
                days_list = json.loads(days_val)
            except Exception:
                days_list = [d.strip() for d in days_val.split(",") if d.strip()]
        else:
            days_list = list(days_val)

        # Caregiver acknowledgment status for missed routines
        caretaker_ack = None
        if status == STATUS_MISSED:
            esc_row = get_escalation(conn, r_id, today_date_iso)
            if esc_row:
                if esc_row.get("acked_at"):
                    caretaker_ack = "Acknowledged"
                elif esc_row.get("reminded_at"):
                    caretaker_ack = "Reminder Sent (Pending)"
                elif esc_row.get("notified_at"):
                    caretaker_ack = "Alert Sent (Pending)"
                else:
                    caretaker_ack = "Firing"

        routines_data.append({
            "routine_id": r_id,
            "name": r_row["name"],
            "device_id": r_row["device_id"],
            "snapshot_device_id": r_row.get("snapshot_device_id"),
            "window_start": r_row["window_start"],
            "window_end": r_row["window_end"],
            "escalation_delay_minutes": r_row.get("escalation_delay_minutes", 30),
            "days": days_list,
            "status": status,
            "status_label": cfg["label"],
            "status_desc": cfg["description"],
            "status_color": cfg["color"],
            "status_bg": cfg["bg"],
            "status_border": cfg["border"],
            "status_icon": cfg["badge_icon"],
            "last_satisfied_at": last_sat_iso,
            "last_satisfied_friendly": last_sat_friendly,
            "snapshot_url": latest_snap_rel,
            "device_online": not health_rec.is_stale(now),
            "caretaker_ack": caretaker_ack,
        })

    # Device health records
    devices = list_device_health(conn)
    online_devices_count = sum(1 for d in devices if d.get("online"))

    # Recent events
    events = list_recent_events(conn, limit=20)
    for ev in events:
        ev["relative_time"] = format_relative_time(ev.get("occurred_at"), now)

    # Recent escalations
    escalations = list_recent_escalations(conn, limit=15)
    corrections = {c["id"]: c for c in list_corrections(conn)}
    for esc in escalations:
        esc["relative_time"] = format_relative_time(esc.get("fired_at"), now)
        corr_id = f"corr_{esc['routine_id']}_{esc['local_date']}"
        corr = corrections.get(corr_id)
        if corr:
            esc["caretaker_action"] = f"False alarm ({corr.get('label', 'corrected')})"
        elif esc.get("acked_at"):
            esc["caretaker_action"] = "Acknowledged (Got it)"
        elif esc.get("reminded_at"):
            esc["caretaker_action"] = "Reminder Sent (Pending)"
        elif esc.get("notified_at"):
            esc["caretaker_action"] = "Notified (Pending)"
        else:
            esc["caretaker_action"] = "Firing..."

        snap_p = esc.get("snapshot_path")
        if snap_p and os.path.exists(snap_p):
            esc["snapshot_url"] = f"/snapshots/{os.path.basename(snap_p)}"
        else:
            esc["snapshot_url"] = None

    # Investigations (autonomous agent check-needed & gate decisions, latest 5)
    raw_invs = list_investigations(conn, limit=5)
    parsed_invs = []
    for inv in raw_invs:
        inv_item = dict(inv)
        ev_json = inv_item.pop("evidence_json", None)
        inv_item.pop("trace_json", None)
        ev_list = []
        if ev_json:
            try:
                ev_list = json.loads(ev_json) if isinstance(ev_json, str) else ev_json
            except Exception:
                ev_list = []
        inv_item["evidence"] = ev_list
        parsed_invs.append(inv_item)

    total_routines = len(routines_data)
    observed_pct = int((observed_count / total_routines * 100)) if total_routines > 0 else 100

    return {
        "household": household_name,
        "timezone": timezone_name,
        "current_time_str": local_time_str,
        "current_date_str": local_date_str,
        "now_iso": now.isoformat(),
        "snapshot_dir": snapshot_dir,
        "daemon": {
            "status": daemon_status,
            "started_at": daemon_started,
            "last_tick_at": daemon_last_tick,
            "ticks": daemon_ticks,
            "last_poll_at": last_poll,
            "last_poll_friendly": format_relative_time(last_poll, now),
        },
        "summary": {
            "total_routines": total_routines,
            "observed_count": observed_count,
            "watching_count": watching_count,
            "missed_count": missed_count,
            "snoozed_count": snoozed_count,
            "unknown_count": unknown_count,
            "observed_pct": observed_pct,
            "online_devices": online_devices_count,
            "total_devices": len(devices),
        },
        "routines": routines_data,
        "devices": devices,
        "recent_events": events,
        "recent_escalations": escalations,
        "investigations": parsed_invs,
    }


def render_dashboard_html(data: Dict[str, Any]) -> str:
    """
    Generate responsive, self-contained HTML dashboard with embedded CSS.
    Zero external dependencies, zero CDNs, zero JavaScript frameworks.
    """
    h_name = html.escape(data.get("household", "Threshold Household"))
    tz_name = html.escape(data.get("timezone", "UTC"))
    c_time = html.escape(data.get("current_time_str", ""))
    c_date = html.escape(data.get("current_date_str", ""))
    summary = data.get("summary", {})
    daemon = data.get("daemon", {})

    observed_color = "#6db88f" if summary.get("observed_pct", 100) >= 80 else ("#d4a04a" if summary.get("observed_pct", 100) >= 50 else "#d4695a")

    # Emergency Alert Banner if any routine is missed
    emergency_banner_html = ""
    missed_count = summary.get("missed_count", 0)
    if missed_count > 0:
        emergency_banner_html = f"""
        <div class="emergency-alert-banner">
          <div class="container emergency-content">
            <span class="emergency-icon">🚨</span>
            <div class="emergency-text">
              <strong>MISSED ROUTINE:</strong> {missed_count} routine(s) missed expected activity window! Telegram notification dispatched to caregivers.
            </div>
          </div>
        </div>
        """

    # Render routine cards
    cards_html = []
    for r in data.get("routines", []):
        r_name = html.escape(r["name"])
        r_window = f"{html.escape(r['window_start'])} – {html.escape(r['window_end'])}"
        r_delay = r.get("escalation_delay_minutes", 30)
        r_dev = html.escape(r["device_id"])
        r_cam = html.escape(r.get("snapshot_device_id") or "None")
        r_sat = html.escape(r.get("last_satisfied_friendly", "Never"))
        days_str = ", ".join(d.upper() for d in r.get("days", []))

        ack_pill = ""
        if r.get("caretaker_ack"):
            ack_text = html.escape(r["caretaker_ack"])
            ack_cls = "badge-green" if "Acknowledged" in ack_text else "badge-red"
            ack_pill = f"""
            <div class="card-detail ack-state">
              <span>Caregiver Status:</span> <b class="{ack_cls}">{ack_text}</b>
            </div>
            """

        thumb_html = ""
        if r.get("snapshot_url"):
            thumb_html = f"""
            <div class="card-thumb-container">
              <a href="{r['snapshot_url']}" onclick="openSnapshotModal('{r['snapshot_url']}', '{r_name}'); return false;" title="Click for Fullscreen Preview">
                <img src="{r['snapshot_url']}" class="card-thumb" alt="Room Snapshot" />
                <span class="thumb-badge">📷 Click to Expand</span>
              </a>
            </div>
            """

        online_dot = "<span class='online-dot online' title='Device Online'></span>" if r.get("device_online") else "<span class='online-dot offline' title='Device Offline / Stale'></span>"

        cards_html.append(f"""
        <div class="routine-card" style="border-top: 3px solid {r['status_color']};">
          <div class="card-header">
            <span class="status-badge" style="background: {r['status_bg']}; color: {r['status_color']}; border: 1px solid {r['status_border']};">
              {r['status_icon']} {r['status_label']}
            </span>
            <span class="card-meta-days">{days_str}</span>
          </div>
          <h3 class="card-title">{r_name}</h3>
          <div class="card-status-desc" style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 8px;">{html.escape(r.get('status_desc', ''))}</div>
          <div class="card-window">
            <span class="icon">⏰</span> Window: <b>{r_window}</b> <span class="delay-tag">+{r_delay}m buffer</span>
          </div>
          <div class="card-detail">
            <span>Sensor:</span> <code>{online_dot} {r_dev}</code>
          </div>
          <div class="card-detail">
            <span>Camera:</span> <code>{r_cam}</code>
          </div>
          <div class="card-detail last-sat">
            <span>Last Activity:</span> <b>{r_sat}</b>
          </div>
          {ack_pill}
          {thumb_html}
        </div>
        """)

    routines_grid = "\n".join(cards_html) if cards_html else "<div class='empty-state'>No routines configured. Run <code>python3 -m src.threshold seed</code> to load household routines.</div>"

    # Map reason codes to plain words
    reason_code_labels = {
        "quiet_hours_unexpected_activity": "Unexpected activity during quiet hours",
        "insufficient_tool_results": "Insufficient tool verification (< 2 distinct tools)",
        "silent_device": "Device offline or stale",
        "repeated_false_alarms": "Household memory shows repeated past false alarms",
        "rate_limit_exceeded": "Agent escalation rate limit exceeded",
        "routine_snoozed": "Associated routine currently snoozed",
        "device_not_healthy": "Device health check not clean",
        "error": "Investigation encountered an execution error",
    }

    # Render Check Needed panel (newest first)
    check_needed_cards = []
    tz_name_str = data.get("timezone", "America/Chicago")
    try:
        tz_info = ZoneInfo(tz_name_str)
    except Exception:
        tz_info = timezone.utc
    dash_snapshot_dir = data.get("snapshot_dir", "snapshots")

    for inv in data.get("investigations", []):
        inv_dev = html.escape(str(inv.get("device_id", "unknown")))

        # Local time formatting
        created_at_str = inv.get("created_at", "")
        local_time_display = created_at_str
        if created_at_str:
            try:
                clean_ts = created_at_str.replace("Z", "+00:00")
                dt_utc = datetime.fromisoformat(clean_ts)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_loc = dt_utc.astimezone(tz_info)
                local_time_display = dt_loc.strftime("%H:%M %Z (%Y-%m-%d)")
            except Exception:
                local_time_display = created_at_str
        local_time_display = html.escape(local_time_display)

        # Tier & Gate Decision badges (using strict labels: "Routine", "Check needed", "Escalation", never "Urgent")
        tier_val = str(inv.get("tier", "check_needed"))
        decision_val = str(inv.get("decision", "check_needed"))

        if decision_val == "escalate":
            decision_label = "Escalation"
            decision_cls = "badge-red"
        elif decision_val == "check_needed":
            decision_label = "Check needed"
            decision_cls = "badge-amber"
        else:
            decision_label = "Routine"
            decision_cls = "badge-green"

        tier_label_text = f"Tier: {tier_val}"
        tier_cls = "badge-amber" if tier_val == "check_needed" else ("badge-red" if tier_val == "escalate" else "badge-green")

        # Reason code in plain words
        raw_reason = str(inv.get("reason_code", ""))
        reason_plain = reason_code_labels.get(raw_reason, raw_reason.replace("_", " ").capitalize())
        reason_display = html.escape(reason_plain)

        # Recommendation with mode label
        rec_display = html.escape(str(inv.get("recommendation", "No recommendation provided.")))

        # Evidence lines
        ev_items = inv.get("evidence", [])
        evidence_lines_html = []
        cited_pull_snapshot_ok = False
        snapshot_filename = None

        for ev in ev_items:
            ev_tool = html.escape(str(ev.get("tool", "tool")))
            ev_ok = bool(ev.get("ok", False))
            ev_text = html.escape(str(ev.get("text", "")))

            if ev.get("tool") == "pull_snapshot" and ev_ok:
                cited_pull_snapshot_ok = True
                raw_text = str(ev.get("text", ""))
                m = re.search(r"saved to\s+(.+?)(?:\s*\(captured at|\.\s*$|$)", raw_text)
                if m:
                    full_path = m.group(1).rstrip(".")
                    snapshot_filename = os.path.basename(full_path)
                    raw_text = raw_text.replace(full_path, f"snapshots/{snapshot_filename}")
                ev_text = html.escape(raw_text)

            if ev_ok:
                evidence_lines_html.append(f"""
                <li class="evidence-line ev-ok">
                  <span class="ev-mark">✓</span>
                  <span class="ev-tool">{ev_tool}:</span>
                  <span class="ev-text">{ev_text}</span>
                </li>
                """)
            else:
                evidence_lines_html.append(f"""
                <li class="evidence-line ev-fail">
                  <span class="ev-mark">✗</span>
                  <span class="ev-tool">{ev_tool}:</span>
                  <span class="ev-text">{ev_text}</span>
                </li>
                """)

        if evidence_lines_html:
            evidence_html = f"<ul class='evidence-list'>{''.join(evidence_lines_html)}</ul>"
        else:
            evidence_html = "<div class='evidence-empty'>None recorded.</div>"

        # Snapshot thumbnail ONLY when snapshot file exists AND investigation cited a successful pull_snapshot
        snapshot_html = ""
        has_snapshot = False
        if cited_pull_snapshot_ok and snapshot_filename:
            candidate_path = os.path.join(dash_snapshot_dir, snapshot_filename)
            if os.path.isfile(candidate_path):
                has_snapshot = True
                safe_snap_url = f"/snapshots/{html.escape(snapshot_filename)}"
                is_simulated = os.path.isfile(os.path.join(dash_snapshot_dir, ".simulated"))
                sim_label_html = (
                    '<div class="simulated-placeholder-label">Simulated placeholder — not a camera image</div>'
                    if is_simulated else ""
                )
                snapshot_html = f"""
                <div class="inv-snapshot-box">
                  <a href="{safe_snap_url}" onclick="openSnapshotModal('{safe_snap_url}', 'Camera Snapshot — {inv_dev}'); return false;" title="Click for Fullscreen Preview">
                    <img src="{safe_snap_url}" class="inv-thumb" alt="Camera Snapshot" />
                    <span class="thumb-badge">📷 Click to Expand</span>
                  </a>
                  {sim_label_html}
                </div>
                """

        if not has_snapshot:
            snapshot_html = '<div class="inv-no-snapshot">No camera image available</div>'

        check_needed_cards.append(f"""
        <div class="check-needed-card">
          <div class="cn-header">
            <div class="cn-title-group">
              <span class="cn-time">{local_time_display}</span>
              <span class="cn-device">Device: <code>{inv_dev}</code></span>
            </div>
            <div class="cn-badges">
              <span class="badge {tier_cls}">{html.escape(tier_label_text)}</span>
              <span class="badge {decision_cls}">{html.escape(decision_label)}</span>
            </div>
          </div>
          <div class="cn-reason">
            <span class="cn-label" style="color: var(--text-muted); font-size: 0.85rem;">Reason:</span> <b>{reason_display}</b>
          </div>
          <div class="cn-rec">
            {rec_display}
          </div>
          <div class="cn-body">
            <div class="cn-evidence">
              <div class="cn-evidence-title">Gathered Evidence:</div>
              {evidence_html}
            </div>
            <div class="cn-media">
              {snapshot_html}
            </div>
          </div>
        </div>
        """)

    if check_needed_cards:
        check_needed_panel_html = f"""
        <div class="check-needed-section">
          <div class="section-header">
            <div class="section-title">
              <span>Check Needed Investigations</span>
            </div>
            <div class="section-meta">Autonomous Agent • Verification Gate Audit Trail</div>
          </div>
          <div class="check-needed-grid">
            {''.join(check_needed_cards)}
          </div>
        </div>
        """
    else:
        check_needed_panel_html = """
        <div class="check-needed-section">
          <div class="section-header">
            <div class="section-title">
              <span>Check Needed Investigations</span>
            </div>
            <div class="section-meta">Autonomous Agent • Verification Gate Audit Trail</div>
          </div>
          <div class="empty-state" style="margin-top: 12px;">
            No check-needed investigations recorded. System operating normally.
          </div>
        </div>
        """

    # Render event timeline rows
    event_rows = []
    for ev in data.get("recent_events", [])[:12]:
        ev_type = html.escape(ev.get("event_type", "event"))
        sub_t = f" ({html.escape(ev['sub_type'])})" if ev.get("sub_type") else ""
        dev = html.escape(ev.get("device_id", "unknown"))
        rel = html.escape(ev.get("relative_time", ""))
        src = html.escape(ev.get("source", "ring"))
        event_rows.append(f"""
        <tr class="event-row">
          <td class="event-time">{rel}</td>
          <td class="event-name"><span class="event-pill">{ev_type}{sub_t}</span></td>
          <td class="event-dev"><code>{dev}</code></td>
          <td class="event-src"><span class="src-pill">{src}</span></td>
        </tr>
        """)
    events_table = "\n".join(event_rows) if event_rows else "<tr><td colspan='4' class='empty-cell'>No activity recorded yet today.</td></tr>"

    # Render escalations rows
    esc_rows = []
    for esc in data.get("recent_escalations", [])[:8]:
        r_id = html.escape(esc.get("routine_id", ""))
        fired_rel = html.escape(esc.get("relative_time", ""))
        action = html.escape(esc.get("caretaker_action", "Pending"))
        snap_link = ""
        if esc.get("snapshot_url"):
            snap_link = f"<a href='{esc['snapshot_url']}' onclick=\"openSnapshotModal('{esc['snapshot_url']}', '{r_id} Snapshot'); return false;\" class='btn-view-photo'>📷 Photo</a>"
        else:
            snap_link = "<span class='text-muted'>—</span>"

        status_class = "action-ack" if "Acknowledged" in action or "False alarm" in action else "action-alert"

        esc_rows.append(f"""
        <tr class="esc-row">
          <td class="esc-time">{fired_rel}</td>
          <td class="esc-routine"><b>{r_id}</b></td>
          <td class="esc-action"><span class="action-tag {status_class}">{action}</span></td>
          <td class="esc-photo">{snap_link}</td>
        </tr>
        """)
    esc_table = "\n".join(esc_rows) if esc_rows else "<tr><td colspan='4' class='empty-cell'>Zero escalations recorded today. All routines normal.</td></tr>"

    # Render device health table
    dev_rows = []
    for d in data.get("devices", []):
        d_id = html.escape(d.get("device_id", "unknown"))
        d_online = d.get("online", False)
        status_badge = "<span class='badge-green'>Online</span>" if d_online else "<span class='badge-red'>Offline / Stale</span>"
        fetched_rel = format_relative_time(d.get("fetched_at"), None)
        dev_rows.append(f"""
        <tr>
          <td><code>{d_id}</code></td>
          <td>{status_badge}</td>
          <td>{fetched_rel}</td>
          <td><span class='src-pill'>{html.escape(d.get('source', 'api'))}</span></td>
        </tr>
        """)
    devices_table = "\n".join(dev_rows) if dev_rows else "<tr><td colspan='4' class='empty-cell'>No devices discovered yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="alternate icon" href="/favicon.ico">
  <title>Threshold — {h_name}</title>
  <style>
    :root {{
      --bg: #1a1714;
      --surface: #231f1b;
      --card: #2a2520;
      --card-hover: #332d27;
      --border: #3d3630;
      --border-subtle: #2f2924;
      --text: #e8e0d8;
      --text-muted: #8c8278;
      --text-dim: #5c544d;
      --green: #6db88f;
      --amber: #d4a04a;
      --coral: #d4695a;
      --blue: #5a8fd4;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 60px;
    }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.85em;
      background: rgba(255, 255, 255, 0.06);
      padding: 2px 6px;
      border-radius: 4px;
      color: #dfd8cf;
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 20px;
    }}

    /* Emergency Alert Banner */
    .emergency-alert-banner {{
      background: linear-gradient(90deg, #992b1d, #d4695a);
      color: #ffffff;
      padding: 12px 0;
      box-shadow: 0 4px 16px rgba(212, 105, 90, 0.4);
      border-bottom: 2px solid #5c1e16;
      animation: alert-glow 3s infinite alternate;
    }}
    @keyframes alert-glow {{
      0% {{ opacity: 0.95; }}
      100% {{ opacity: 1; filter: brightness(1.1); }}
    }}
    .emergency-content {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 0.95rem;
    }}
    .emergency-icon {{
      font-size: 1.4rem;
    }}

    /* Top Header */
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 16px 0;
      margin-bottom: 24px;
    }}
    .header-content {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
    }}
    .brand-group {{
      display: flex;
      align-items: center;
      gap: 14px;
    }}
    .logo-badge {{
      background: linear-gradient(135deg, #d4a04a, #d4695a);
      color: #1a1714;
      font-weight: 800;
      padding: 6px 12px;
      border-radius: 8px;
      letter-spacing: 1px;
      font-size: 0.9rem;
    }}
    .household-title {{
      font-size: 1.4rem;
      font-weight: 700;
      color: var(--text);
    }}
    .pulse-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(109, 184, 143, 0.15);
      color: var(--green);
      border: 1px solid rgba(109, 184, 143, 0.3);
      padding: 4px 10px;
      border-radius: 100px;
      font-size: 0.78rem;
      font-weight: 600;
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      background-color: var(--green);
      border-radius: 50%;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(1.3); }}
      100% {{ opacity: 1; transform: scale(1); }}
    }}
    .clock-group {{
      text-align: right;
    }}
    .clock-time {{
      font-size: 1.3rem;
      font-weight: 700;
      letter-spacing: 0.5px;
    }}
    .clock-date {{
      font-size: 0.85rem;
      color: var(--text-muted);
    }}

    /* Metrics Summary Grid */
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }}
    .metric-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 18px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .metric-label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .metric-value {{
      font-size: 2rem;
      font-weight: 800;
      line-height: 1.1;
    }}
    .metric-sub {{
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 6px;
    }}

    /* Section Headers */
    .section-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      margin-top: 10px;
    }}
    .section-title {{
      font-size: 1.15rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .section-meta {{
      font-size: 0.82rem;
      color: var(--text-muted);
    }}

    /* Routines Grid */
    .routines-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 20px;
      margin-bottom: 36px;
    }}
    .routine-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      transition: transform 0.15s ease, background 0.15s ease;
    }}
    .routine-card:hover {{
      background: var(--card-hover);
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 4px;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 0.75rem;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 6px;
      letter-spacing: 0.5px;
    }}
    .card-meta-days {{
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .card-title {{
      font-size: 1.15rem;
      font-weight: 700;
      color: #fff;
    }}
    .card-window {{
      font-size: 0.9rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .delay-tag {{
      font-size: 0.72rem;
      background: rgba(255, 255, 255, 0.08);
      padding: 1px 6px;
      border-radius: 4px;
      color: var(--text-muted);
    }}
    .card-detail {{
      font-size: 0.85rem;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .card-detail.last-sat {{
      padding-top: 8px;
      border-top: 1px dashed var(--border);
      color: var(--text);
    }}
    .card-detail.ack-state {{
      padding-top: 4px;
      color: var(--text);
    }}
    .online-dot {{
      display: inline-block;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      margin-right: 4px;
    }}
    .online-dot.online {{ background: var(--green); }}
    .online-dot.offline {{ background: var(--coral); }}

    /* Snapshot Thumbnail */
    .card-thumb-container {{
      position: relative;
      margin-top: 8px;
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid var(--border);
      max-height: 180px;
      background: #000;
    }}
    .card-thumb {{
      width: 100%;
      height: 180px;
      object-fit: cover;
      display: block;
      transition: opacity 0.2s;
      cursor: zoom-in;
    }}
    .card-thumb:hover {{
      opacity: 0.9;
    }}
    .thumb-badge {{
      position: absolute;
      bottom: 8px;
      right: 8px;
      background: rgba(0, 0, 0, 0.75);
      color: #fff;
      font-size: 0.7rem;
      padding: 3px 8px;
      border-radius: 6px;
      backdrop-filter: blur(4px);
    }}

    /* Lightbox Modal Dialog */
    .snap-modal {{
      margin: auto;
      border: none;
      background: transparent;
      padding: 0;
      max-width: 90vw;
      max-height: 90vh;
      outline: none;
    }}
    .snap-modal::backdrop {{
      background: rgba(0, 0, 0, 0.85);
      backdrop-filter: blur(5px);
    }}
    .modal-box {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
    }}
    .modal-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
      font-weight: 700;
      color: var(--text);
    }}
    .modal-close {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 1.2rem;
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 6px;
    }}
    .modal-close:hover {{
      color: #fff;
      background: rgba(255, 255, 255, 0.1);
    }}
    #snap-modal-img {{
      max-width: 85vw;
      max-height: 75vh;
      object-fit: contain;
      display: block;
      background: #000;
    }}

    /* Check Needed Panel Styles */
    .check-needed-section {{
      margin-top: 32px;
      margin-bottom: 32px;
    }}
    .check-needed-grid {{
      display: flex;
      flex-direction: column;
      gap: 16px;
      margin-top: 14px;
    }}
    .check-needed-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 22px;
      transition: border-color 0.2s;
    }}
    .check-needed-card:hover {{
      border-color: #5c4a28;
    }}
    .cn-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .cn-title-group {{
      display: flex;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .cn-time {{
      font-weight: 700;
      font-size: 1.05rem;
      color: var(--text);
    }}
    .cn-device {{
      color: var(--text-muted);
      font-size: 0.88rem;
    }}
    .cn-badges {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .badge-amber {{
      background: rgba(212, 160, 74, 0.15);
      color: var(--amber);
      border: 1px solid #5c4a28;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .badge-red {{
      background: rgba(212, 105, 90, 0.18);
      color: var(--coral);
      border: 1px solid #5c302a;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .badge-green {{
      background: rgba(109, 184, 143, 0.15);
      color: var(--green);
      border: 1px solid #3a5c48;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .cn-reason {{
      margin-bottom: 8px;
      font-size: 0.95rem;
    }}
    .cn-rec {{
      margin-bottom: 14px;
      font-size: 0.92rem;
      line-height: 1.45;
      background: rgba(255, 255, 255, 0.03);
      padding: 10px 14px;
      border-radius: 8px;
      border-left: 3px solid var(--amber);
      color: var(--text);
    }}
    .cn-body {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 20px;
      align-items: start;
    }}
    .cn-evidence {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
    }}
    .cn-evidence-title {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .evidence-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .evidence-line {{
      font-size: 0.88rem;
      display: flex;
      gap: 8px;
      align-items: baseline;
      word-break: break-word;
      overflow-wrap: anywhere;
    }}
    .ev-ok .ev-mark {{
      color: var(--green);
      font-weight: bold;
    }}
    .ev-fail .ev-mark {{
      color: var(--coral);
      font-weight: bold;
    }}
    .ev-tool {{
      color: var(--text-muted);
      font-size: 0.82rem;
      font-weight: 600;
    }}
    .ev-text {{
      color: var(--text);
    }}
    .evidence-empty {{
      font-size: 0.85rem;
      color: var(--text-muted);
      font-style: italic;
    }}
    .cn-media {{
      min-width: 150px;
    }}
    .inv-snapshot-box {{
      position: relative;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
    }}
    .inv-thumb {{
      width: 160px;
      height: 110px;
      object-fit: cover;
      display: block;
      cursor: zoom-in;
      transition: opacity 0.2s;
    }}
    .inv-thumb:hover {{
      opacity: 0.9;
    }}
    .simulated-placeholder-label {{
      font-size: 0.68rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--surface-raised, rgba(0, 0, 0, 0.4));
      border-top: 1px solid var(--border);
      padding: 4px 6px;
      text-align: center;
      line-height: 1.2;
      max-width: 160px;
    }}
    .inv-no-snapshot {{
      font-size: 0.82rem;
      color: var(--text-muted);
      font-style: italic;
      padding: 16px 14px;
      border: 1px dashed var(--border);
      border-radius: 8px;
      text-align: center;
      max-width: 160px;
    }}

    /* Split Sections (Activity & Escalations) */
    .split-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 24px;
      margin-bottom: 36px;
    }}
    @media (max-width: 900px) {{
      .split-grid {{ grid-template-columns: 1fr; }}
      .cn-body {{ grid-template-columns: 1fr; }}
      .inv-thumb {{ width: 100%; height: 180px; }}
      .inv-no-snapshot {{ max-width: 100%; }}
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
    }}
    .table-container {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}
    th {{
      text-align: left;
      padding: 10px 12px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.6px;
    }}
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--border-subtle);
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    .event-pill {{
      font-weight: 600;
      color: #fff;
    }}
    .src-pill {{
      font-size: 0.7rem;
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 4px;
      color: var(--text-muted);
    }}
    .action-tag {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .action-ack {{
      background: rgba(109, 184, 143, 0.15);
      color: var(--green);
      border: 1px solid rgba(109, 184, 143, 0.3);
    }}
    .action-alert {{
      background: rgba(212, 105, 90, 0.18);
      color: var(--coral);
      border: 1px solid rgba(212, 105, 90, 0.3);
    }}
    .badge-green {{
      color: var(--green);
      font-weight: 600;
    }}
    .badge-red {{
      color: var(--coral);
      font-weight: 600;
    }}
    .btn-view-photo {{
      background: rgba(90, 143, 212, 0.2);
      color: var(--blue);
      border: 1px solid rgba(90, 143, 212, 0.4);
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .empty-cell {{
      text-align: center;
      padding: 24px;
      color: var(--text-muted);
      font-style: italic;
    }}
    .empty-state {{
      grid-column: 1 / -1;
      padding: 36px;
      text-align: center;
      background: var(--surface);
      border: 1px dashed var(--border);
      border-radius: 12px;
      color: var(--text-muted);
    }}

    /* Footer */
    footer {{
      margin-top: 40px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      text-align: center;
      font-size: 0.8rem;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .footer-pill {{
      background: rgba(255, 255, 255, 0.04);
      padding: 4px 10px;
      border-radius: 100px;
      font-size: 0.75rem;
    }}
  </style>
  <script>
    function openSnapshotModal(imgSrc, title) {{
      var modal = document.getElementById('snap-modal');
      var img = document.getElementById('snap-modal-img');
      var caption = document.getElementById('snap-modal-title');
      if (modal && img) {{
        img.src = imgSrc;
        if (caption) caption.innerText = title;
        modal.showModal();
      }}
    }}
  </script>
</head>
<body>

  {emergency_banner_html}

  <header>
    <div class="container header-content">
      <div class="brand-group">
        <div class="logo-badge">THRESHOLD</div>
        <div>
          <div class="household-title">{h_name}</div>
          <div style="font-size: 0.8rem; color: var(--text-muted);">Pattern Absence Detector for Elderly Care</div>
        </div>
        <div class="pulse-pill">
          <span class="pulse-dot"></span>
          <span>{daemon.get('status', 'Active')}</span>
        </div>
      </div>
      <div class="clock-group">
        <div class="clock-time">{c_time}</div>
        <div class="clock-date">{c_date} ({tz_name})</div>
      </div>
    </div>
  </header>

  <div class="container">

    <!-- KPI Summary Grid -->
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-label">Routines observed today</div>
        <div class="metric-value" style="color: {observed_color};">{summary.get('observed_pct', 100)}%</div>
        <div class="metric-sub">{summary.get('observed_count', 0)} of {summary.get('total_routines', 0)} completed</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Active Watching</div>
        <div class="metric-value" style="color: var(--amber);">{summary.get('watching_count', 0)}</div>
        <div class="metric-sub">Within window or pending</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Missed / Escalation</div>
        <div class="metric-value" style="color: var(--coral);">{summary.get('missed_count', 0)}</div>
        <div class="metric-sub">{summary.get('missed_count', 0)} unaddressed misses</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Tracked Devices</div>
        <div class="metric-value" style="color: var(--text);">{summary.get('online_devices', 0)}<span style="font-size: 1.1rem; color: var(--text-muted);">/{summary.get('total_devices', 0)}</span></div>
        <div class="metric-sub">Ring sensors online</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Daemon Heartbeat</div>
        <div class="metric-value" style="color: var(--blue); font-size: 1.4rem; padding-top: 6px;">Tick #{daemon.get('ticks', 0)}</div>
        <div class="metric-sub">Last poll: {daemon.get('last_poll_friendly', 'Just now')}</div>
      </div>
    </div>

    <!-- Active Routines Grid -->
    <div class="section-header">
      <div class="section-title">
        <span>Routine Status Cards</span>
      </div>
      <div class="section-meta">Auto-refreshes every 10s • Local SQLite facts</div>
    </div>

    <div class="routines-grid">
      {routines_grid}
    </div>

    <!-- Check Needed Investigations Panel -->
    {check_needed_panel_html}

    <!-- Split Panel: Event Timeline & Escalations -->
    <div class="split-grid">

      <!-- Today's Activity Timeline -->
      <div class="panel">
        <div class="section-header" style="margin-top: 0;">
          <div class="section-title">
            <span>Today's Activity Timeline</span>
          </div>
          <div class="section-meta">Latest 12 events</div>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Event Type</th>
                <th>Device</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {events_table}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Escalations & Caretaker Audit Log -->
      <div class="panel">
        <div class="section-header" style="margin-top: 0;">
          <div class="section-title">
            <span>Escalation & Caretaker Log</span>
          </div>
          <div class="section-meta">Audit trail</div>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Fired</th>
                <th>Routine</th>
                <th>Outcome / Action</th>
                <th>Snapshot</th>
              </tr>
            </thead>
            <tbody>
              {esc_table}
            </tbody>
          </table>
        </div>
      </div>

    </div>

    <!-- Devices Health Section -->
    <div class="panel" style="margin-bottom: 24px;">
      <div class="section-header" style="margin-top: 0;">
        <div class="section-title">
          <span>Connected Ring Hardware</span>
        </div>
        <div class="section-meta">{summary.get('total_devices', 0)} devices registered in local health store</div>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Device Identifier</th>
              <th>Status</th>
              <th>Last Checked</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {devices_table}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Footer -->
    <footer>
      <div>
        <b>Threshold v1.0</b> — Pattern Absence Detection for Elderly Care
      </div>
      <div class="footer-pill">
        🔒 100% Local Edge Server • Zero AWS • Python Stdlib Only • <a href="/api/status" target="_blank">JSON API</a>
      </div>
      <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 6px;">
        Caregiver-support notification tool. Not an emergency or medical device. Does not contact emergency services.
      </div>
    </footer>

  </div>

  <!-- Snapshot Lightbox Modal -->
  <dialog id="snap-modal" class="snap-modal" onclick="if(event.target===this)this.close()">
    <div class="modal-box">
      <div class="modal-header">
        <span id="snap-modal-title">Snapshot Preview</span>
        <button type="button" class="modal-close" onclick="document.getElementById('snap-modal').close()">✕</button>
      </div>
      <img id="snap-modal-img" src="" alt="Full Snapshot" />
    </div>
  </dialog>

</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for Threshold local web dashboard."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logging in standard daemon mode."""
        pass

    def _send_response(self, status_code: int, content_type: str, body_bytes: bytes) -> None:
        """Helper to send HTTP response headers and body with security headers."""
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self) -> None:
        """Handle GET requests for dashboard, JSON API, snapshots, favicon, and healthz."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # Favicon handling
        if path in ("/favicon.ico", "/favicon.svg"):
            self._send_response(200, "image/svg+xml", FAVICON_SVG)
            return

        # Health check
        if path == "/healthz":
            resp_body = json.dumps({"status": "ok", "service": "threshold-dashboard"}).encode("utf-8")
            self._send_response(200, "application/json", resp_body)
            return

        # Snapshot static serving with strict directory traversal protection
        if path.startswith("/snapshots/"):
            raw_url_path = self.path[len("/snapshots/"):]
            decoded_path = urllib.parse.unquote(raw_url_path)

            # Prevent directory traversal, path separators, or suspicious encodings -> 404
            if ".." in raw_url_path or ".." in decoded_path or "/" in decoded_path or "\\" in decoded_path or "%2e" in raw_url_path.lower():
                self._send_response(404, "text/plain", b"Not Found")
                return

            filename = os.path.basename(decoded_path)
            if not filename or filename != decoded_path:
                self._send_response(404, "text/plain", b"Not Found")
                return

            snapshot_dir = getattr(self.server, "snapshot_dir", "snapshots")
            file_path = os.path.join(snapshot_dir, filename)

            # Reject symlinks -> 404
            if os.path.islink(file_path):
                self._send_response(404, "text/plain", b"Not Found")
                return

            # Ensure resolved path is strictly inside snapshot_dir
            real_base = os.path.realpath(snapshot_dir)
            real_target = os.path.realpath(file_path)
            if not real_target.startswith(real_base) or os.path.dirname(real_target) != real_base:
                self._send_response(404, "text/plain", b"Not Found")
                return

            if not os.path.isfile(real_target):
                self._send_response(404, "text/plain", b"Snapshot Not Found")
                return

            try:
                with open(real_target, "rb") as f:
                    img_bytes = f.read()
                mime = detect_image_mime(img_bytes)
                if mime == "application/octet-stream":
                    mime = "image/jpeg"
                self._send_response(200, mime, img_bytes)
            except Exception as exc:
                self._send_response(500, "text/plain", f"Failed reading snapshot: {exc}".encode("utf-8"))
            return

        # Connect to SQLite for dashboard data
        db_path = getattr(self.server, "db_path", "threshold.db")
        household_name = getattr(self.server, "household_name", "Mom's House — Dallas")
        timezone_name = getattr(self.server, "timezone_name", "America/Chicago")
        snapshot_dir = getattr(self.server, "snapshot_dir", "snapshots")

        # GET /api/investigations endpoint
        if path == "/api/investigations":
            query_params = urllib.parse.parse_qs(parsed_url.query)
            limit_str = query_params.get("limit", ["20"])[0]
            try:
                limit_val = int(limit_str)
            except ValueError:
                limit_val = 20
            limit_val = max(1, min(100, limit_val))

            try:
                conn = get_connection(db_path)
                try:
                    raw_invs = list_investigations(conn, limit=limit_val)
                finally:
                    conn.close()
            except Exception as exc:
                self._send_response(500, "text/plain", f"Database error: {exc}".encode("utf-8"))
                return

            parsed_invs = []
            for inv in raw_invs:
                inv_item = dict(inv)
                ev_json = inv_item.pop("evidence_json", None)
                inv_item.pop("trace_json", None)
                ev_list = []
                if ev_json:
                    try:
                        ev_list = json.loads(ev_json) if isinstance(ev_json, str) else ev_json
                        for item in ev_list:
                            if isinstance(item, dict) and item.get("tool") == "pull_snapshot":
                                t = item.get("text", "")
                                m = re.search(r"saved to\s+(.+?)(?:\s*\(captured at|\.\s*$|$)", t)
                                if m:
                                    full_path = m.group(1).rstrip(".")
                                    bname = os.path.basename(full_path)
                                    item["text"] = t.replace(full_path, f"snapshots/{bname}")
                    except Exception:
                        ev_list = []
                inv_item["evidence"] = ev_list
                parsed_invs.append(inv_item)

            resp_bytes = json.dumps(parsed_invs, indent=2).encode("utf-8")
            self._send_response(200, "application/json", resp_bytes)
            return

        clock = getattr(self.server, "clock", None)
        sim_now = getattr(self.server, "simulated_now", None)
        req_now = clock.now() if clock else (sim_now if sim_now else None)

        try:
            conn = get_connection(db_path)
            try:
                data = get_dashboard_data(
                    conn=conn,
                    now=req_now,
                    household_name=household_name,
                    timezone_name=timezone_name,
                    snapshot_dir=snapshot_dir,
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.error("Error generating dashboard data from SQLite: %s", exc)
            self._send_response(500, "text/plain", f"Database error: {exc}".encode("utf-8"))
            return

        # JSON API endpoints with query filtering support
        if path in ("/api/status", "/api/routines"):
            query_params = urllib.parse.parse_qs(parsed_url.query)
            status_filter = query_params.get("status", [None])[0]
            routine_filter = query_params.get("routine_id", [None])[0]

            filtered_data = dict(data)
            if status_filter:
                filtered_data["routines"] = [
                    r for r in filtered_data["routines"] if r.get("status") == status_filter.lower()
                ]
            if routine_filter:
                filtered_data["routines"] = [
                    r for r in filtered_data["routines"] if r.get("routine_id") == routine_filter
                ]

            if path == "/api/routines":
                resp_bytes = json.dumps(filtered_data["routines"], indent=2).encode("utf-8")
            else:
                resp_bytes = json.dumps(filtered_data, indent=2).encode("utf-8")

            self._send_response(200, "application/json", resp_bytes)
            return

        # HTML Dashboard homepage
        if path in ("/", "/index.html"):
            html_content = render_dashboard_html(data)
            self._send_response(200, "text/html; charset=utf-8", html_content.encode("utf-8"))
            return

        # 404 for any other path
        self._send_response(404, "text/plain", b"Not Found")

    def do_POST(self) -> None:
        """Reject POST requests: dashboard is strictly read-only."""
        self._send_response(405, "text/plain", b"Method Not Allowed: Dashboard is strictly read-only")

    def do_PUT(self) -> None:
        """Reject PUT requests: dashboard is strictly read-only."""
        self._send_response(405, "text/plain", b"Method Not Allowed: Dashboard is strictly read-only")

    def do_DELETE(self) -> None:
        """Reject DELETE requests: dashboard is strictly read-only."""
        self._send_response(405, "text/plain", b"Method Not Allowed: Dashboard is strictly read-only")

    def do_PATCH(self) -> None:
        """Reject PATCH requests: dashboard is strictly read-only."""
        self._send_response(405, "text/plain", b"Method Not Allowed: Dashboard is strictly read-only")


class DashboardServer(http.server.ThreadingHTTPServer):
    """Configured ThreadingHTTPServer holding dashboard configuration."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple,
        RequestHandlerClass: type,
        db_path: str = "threshold.db",
        household_name: str = "Mom's House — Dallas",
        timezone_name: str = "America/Chicago",
        snapshot_dir: str = "snapshots",
        clock: Optional[Any] = None,
        simulated_now: Optional[datetime] = None,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.db_path = db_path
        self.household_name = household_name
        self.timezone_name = timezone_name
        self.snapshot_dir = snapshot_dir
        self.clock = clock
        self.simulated_now = simulated_now


def start_dashboard_server(
    db_path: str = "threshold.db",
    port: int = 8080,
    host: str = "127.0.0.1",
    household_name: str = "Mom's House — Dallas",
    timezone_name: str = "America/Chicago",
    snapshot_dir: str = "snapshots",
    fallback_ports: bool = True,
    clock: Optional[Any] = None,
    simulated_now: Optional[datetime] = None,
) -> DashboardServer:
    """
    Instantiate and start the dashboard HTTP server on a background daemon thread.
    Default bind host is 127.0.0.1.
    Includes port collision fallback protection (e.g. if port 8080 is bound, tries 8081..8085).
    Returns the running DashboardServer instance.
    """
    attempt_port = port
    max_attempts = 10 if (fallback_ports and port != 0) else 1
    server = None

    for attempt in range(max_attempts):
        try:
            server = DashboardServer(
                server_address=(host, attempt_port),
                RequestHandlerClass=DashboardHandler,
                db_path=db_path,
                household_name=household_name,
                timezone_name=timezone_name,
                snapshot_dir=snapshot_dir,
                clock=clock,
                simulated_now=simulated_now,
            )
            break
        except OSError as exc:
            if attempt < max_attempts - 1 and port != 0:
                logger.warning("Port %d in use, trying fallback port %d...", attempt_port, attempt_port + 1)
                attempt_port += 1
            else:
                raise exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_dashboard_command(
    db_path: str = "threshold.db",
    port: int = 8080,
    host: str = "127.0.0.1",
    household_name: str = "Mom's House — Dallas",
    timezone_name: str = "America/Chicago",
    snapshot_dir: str = "snapshots",
    open_browser: bool = False,
) -> int:
    """
    CLI command entry point: starts dashboard HTTP server and blocks until SIGINT/SIGTERM.
    Optionally launches default browser if open_browser=True.
    Default bind host is 127.0.0.1. Prints security warning if binding to non-loopback address.
    """
    if host not in ("127.0.0.1", "localhost"):
        logger.warning(
            "[SECURITY WARNING] Dashboard is binding to non-loopback host '%s'. "
            "The web dashboard displays camera images and status without authentication.",
            host,
        )
        print(
            f"[SECURITY WARNING] Dashboard is binding to non-loopback host '{host}'. "
            "The web dashboard displays camera images and status without authentication.",
            file=sys.stderr,
        )

    server = start_dashboard_server(
        db_path=db_path,
        port=port,
        host=host,
        household_name=household_name,
        timezone_name=timezone_name,
        snapshot_dir=snapshot_dir,
        fallback_ports=True,
    )
    actual_port = server.server_address[1]
    display_host = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    dash_url = f"http://{display_host}:{actual_port}"

    print("=" * 70)
    print("THRESHOLD LOCAL WEB DASHBOARD")
    print("=" * 70)
    print(f"Household:  {household_name}")
    print(f"Timezone:   {timezone_name}")
    print(f"Database:   {db_path}")
    print(f"Snapshots:  {snapshot_dir}")
    print(f"Dashboard:  {dash_url}")
    print(f"JSON API:   {dash_url}/api/status")
    print("-" * 70)
    print("Press Ctrl+C to stop the dashboard server.")

    if open_browser:
        try:
            import webbrowser

            threading.Timer(0.8, lambda: webbrowser.open(dash_url)).start()
        except Exception:
            pass

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.shutdown()
        server.server_close()
    return 0
