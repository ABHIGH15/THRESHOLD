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


def format_evidence_text_for_caregiver(text: str) -> str:
    """
    Format evidence text for caregiver-facing notifications.
    Completely drops the internal 'saved to <path>' clause, leaving clean user-facing text
    such as 'Snapshot retrieved successfully for device X (captured at ...)'.
    Guarantees no internal filesystem paths (/var/folders, /Users, /tmp, snapshots/)
    appear in messages sent to caregivers.
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
        return cleaned
    return text
