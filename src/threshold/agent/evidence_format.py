"""
Shared evidence text formatting and path sanitization helpers for Threshold agent.

Used by:
- src.threshold.agent.runner: formats caregiver-facing alert messages (dropping machine paths).
- src.threshold.dashboard: sanitizes evidence for dashboard HTML and API JSON (snapshots/<basename>).
"""

import os
import re
from typing import Optional


def extract_snapshot_filename(text: str) -> Optional[str]:
    """
    Extract snapshot filename (basename) from evidence text if present.
    Returns None if no snapshot path is found.
    """
    if not text:
        return None
    m = re.search(r"saved to\s+(.+?)(?:\s*\(captured at|\.\s*$|$)", text)
    if m:
        full_path = m.group(1).strip().rstrip(".")
        if full_path:
            return os.path.basename(full_path)
    return None


def format_evidence_text_for_dashboard(text: str) -> str:
    """
    Format evidence text for dashboard display and API responses.
    Replaces full filesystem machine paths with sanitized 'snapshots/<basename>' references.
    """
    if not text:
        return ""
    m = re.search(r"saved to\s+(.+?)(?:\s*\(captured at|\.\s*$|$)", text)
    if m:
        full_path = m.group(1).rstrip(".")
        bname = os.path.basename(full_path.strip())
        return text.replace(full_path, f"snapshots/{bname}")
    return text


def format_evidence_text_for_caregiver(text: str, timezone_name: Optional[str] = None) -> str:
    """
    Format evidence text for caregiver-facing notifications.
    Completely drops the internal 'saved to <path>' clause, leaving clean user-facing text
    such as 'Snapshot retrieved successfully for device X (captured 02:10 CDT)'.
    Guarantees no internal filesystem paths (/var/folders, /Users, /tmp, snapshots/)
    appear in messages sent to caregivers.
    If timezone_name is provided and an ISO timestamp is present, converts '(captured at <iso>)'
    to '(captured <HH:MM TZ>)' in the specified timezone.
    If timezone is not available or conversion fails, leaves timestamp as is.
    """
    if not text:
        return ""
    m = re.search(r"\s+saved to\s+(.+?)(?:\s*\(captured at|\.\s*$|$)", text)
    if m:
        full_path = m.group(1).rstrip(".")
        clause = f" saved to {full_path}"
        cleaned = text.replace(clause, "")
        cleaned = re.sub(r"\s+\.", ".", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    else:
        cleaned = text

    if timezone_name:
        m_cap = re.search(r"\(captured at\s+([^)]+)\)", cleaned)
        if m_cap:
            iso_str = m_cap.group(1).strip()
            try:
                from datetime import datetime, timezone
                from zoneinfo import ZoneInfo
                clean_iso = iso_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                tz = ZoneInfo(timezone_name)
                local_dt = dt.astimezone(tz)
                time_str = local_dt.strftime("%H:%M %Z")
                cleaned = cleaned.replace(m_cap.group(0), f"(captured {time_str})")
            except Exception:
                pass

    return cleaned
