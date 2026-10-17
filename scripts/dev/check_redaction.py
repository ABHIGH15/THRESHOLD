#!/usr/bin/env python3
r"""
scripts/dev/check_redaction.py — Verifies that no real tokens, device IDs, or secret shapes exist in tracked files.

Specifications:
1. Reads real IDs/tokens from git-ignored evidence/ring/ in MEMORY ONLY (never prints values).
2. Scans every tracked file for:
   - In-memory collected real IDs/tokens
   - eyJ[A-Za-z0-9_-]{10,} (JWT tokens)
   - Bearer\s+\S{8,} (Live bearer tokens, ignoring template code)
   - X-Amz-Signature= (AWS signatures)
   - AKIA[0-9A-Z]{16} (AWS access keys)
   - \\d{8,10}:[A-Za-z0-9_-]{35} (Telegram bot tokens)
3. For every tracked file, prints "<file>: <count>".
4. Exits non-zero if any count > 0.
5. If --self-test is passed: injects a collected ID into scanner and proves detection (count 1, non-zero exit).
"""

import glob
import os
import re
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET_PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}")),
    ("amz_sig", re.compile(r"X-Amz-Signature=")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
]

BEARER_PATTERN = re.compile(r"Bearer\s+(\S{8,})")


def is_code_template(val: str) -> bool:
    """Ignore non-credential code templates, docstrings, and scanner regexes."""
    v = val.strip().rstrip('",;\')')
    if v.startswith(("{", "<", "[", "$")):
        return True
    if v.startswith("token") or v in ("<token>", "{token}", "{self.token}", "{self.access_token}"):
        return True
    if "[A-Za-z0-9" in v:
        return True
    return False


def collect_real_identifiers(evidence_dir: str) -> set[str]:
    """Collect real sensitive IDs and tokens from git-ignored evidence in memory only."""
    tokens = set()
    if not os.path.exists(evidence_dir):
        return tokens

    for root, _, files in os.walk(evidence_dir):
        for f in files:
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                    text = fp.read()
            except Exception:
                continue

            # Ring playground tokens
            for m in re.findall(r"Atzr\|[A-Za-z0-9_%-]+", text):
                if len(m) > 15:
                    tokens.add(m)

            # Ring entity IDs (ava1.ring.<kind>.<id>)
            for m in re.findall(r"ava1\.ring\.[A-Za-z0-9_.-]+", text):
                m_clean = m.rstrip(".,;\"'\n")
                last_part = m_clean.split(".")[-1]
                if last_part not in ("EXAMPLE", "device", "capabilities", "configurations", "status", "location", "events", "events-export") and len(last_part) >= 8:
                    tokens.add(m_clean)

    return tokens


def get_tracked_files() -> list[str]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=PROJECT_ROOT, text=True)
        return [f for f in out.splitlines() if f.strip()]
    except Exception:
        # Fallback if not a git repo
        files = []
        for root, _, fs in os.walk(PROJECT_ROOT):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), PROJECT_ROOT)
                files.append(rel)
        return files


def scan_file(rel_path: str, content: str, secret_tokens: set[str]) -> int:
    # Skip the scanner itself to avoid false positives on pattern definitions
    if rel_path in ("scripts/dev/check_redaction.py", "scripts/dev/run_secrets_scan.py"):
        return 0

    count = 0

    # 1. Check in-memory collected real tokens/IDs
    for token in secret_tokens:
        if token in content:
            count += content.count(token)

    # 2. Check regex patterns
    for name, pat in SECRET_PATTERNS:
        matches = pat.findall(content)
        count += len(matches)

    # 3. Check Bearer pattern
    for match in BEARER_PATTERN.findall(content):
        if not is_code_template(match):
            count += 1

    return count


def main() -> int:
    self_test = "--self-test" in sys.argv

    evidence_dir = os.path.join(PROJECT_ROOT, "evidence", "ring")
    secret_tokens = collect_real_identifiers(evidence_dir)

    if not os.path.exists(evidence_dir) and not self_test:
        print("SKIPPED: evidence/ring/ not present (clean clone without local evidence)")
        return 0

    if self_test:
        print("=== RUNNING SELF-TEST ===")
        # Inject an artificial real identifier into the secret set
        injected_token = "ava1.ring.device.INJECTED_TEST_SECRET_DEVICE_ID_12345"
        test_tokens = set(secret_tokens) | {injected_token}
        dummy_file = "sample_test_file.txt"
        dummy_content = f"Configuration with device={injected_token}\n"
        cnt = scan_file(dummy_file, dummy_content, test_tokens)
        print(f"{dummy_file}: {cnt}")
        if cnt == 1:
            print("SELF-TEST: Injected token successfully detected (count 1, non-zero exit).")
            return 1
        else:
            print(f"SELF-TEST FAILED: Expected count 1, got {cnt}", file=sys.stderr)
            return 2

    tracked_files = get_tracked_files()
    total_leaks = 0

    for f in tracked_files:
        full_path = os.path.join(PROJECT_ROOT, f)
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read()
        except Exception:
            continue

        cnt = scan_file(f, content, secret_tokens)
        print(f"{f}: {cnt}")
        if cnt > 0:
            total_leaks += cnt

    if total_leaks > 0:
        print(f"\nREDACTION FAILURE: {total_leaks} sensitive match(es) detected!", file=sys.stderr)
        return 1

    print("\nALL TRACKED FILES REDACTION CHECK PASSED (0 secret matches).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
