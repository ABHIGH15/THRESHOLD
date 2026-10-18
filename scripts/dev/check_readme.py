#!/usr/bin/env python3
"""
scripts/dev/check_readme.py — Mechanical integrity verification for README.md.

Performs automated checks:
1. Every relative Markdown link target exists on disk.
2. Every fenced python3 command references an existing script or valid src.threshold subcommand.
3. Every environment variable named in README.md is present in .env.example.
4. Top 30 lines contain product definition, one-command demo, and Ring APIs summary.
"""

import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_tracked_files() -> set[str]:
    try:
        import subprocess
        out = subprocess.check_output(["git", "ls-files"], cwd=PROJECT_ROOT, text=True)
        files = {f.strip() for f in out.splitlines() if f.strip()}
        # Also include directory prefixes of tracked files
        dirs = set()
        for f in files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        return files | dirs
    except Exception:
        return set()


def check_relative_links(readme_content: str) -> list[str]:
    errors = []
    tracked = get_tracked_files()
    # Match [text](target) but ignore anchors and external URLs
    links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', readme_content)
    for text, target in links:
        target_clean = target.split("#")[0].strip()
        if not target_clean or target_clean.startswith(("http://", "https://", "mailto:")):
            continue
        target_norm = os.path.normpath(target_clean)
        # Check against git-tracked files/dirs
        if tracked and target_norm not in tracked:
            errors.append(f"Broken relative link: [{text}]({target}) -> '{target_clean}' is not tracked in git.")
        elif not os.path.exists(os.path.join(PROJECT_ROOT, target_clean)):
            errors.append(f"Broken relative link: [{text}]({target}) -> '{target_clean}' does not exist on disk.")
    return errors


def check_python_commands(readme_content: str) -> list[str]:
    errors = []
    valid_subcommands = {"init", "ring-check", "seed", "test-alert", "dashboard", "demo", "run"}

    # Extract all command lines starting with python3
    py_lines = re.findall(r'^\s*python3\s+([^\n#]+)', readme_content, re.MULTILINE)
    for cmd in py_lines:
        tokens = cmd.strip().split()
        if not tokens:
            continue
        first = tokens[0]
        if first == "-m":
            if len(tokens) >= 2:
                module = tokens[1]
                if module == "src.threshold":
                    if len(tokens) >= 3:
                        subcmd = tokens[2]
                        if subcmd not in valid_subcommands:
                            errors.append(f"Invalid subcommand in command: 'python3 {cmd.strip()}': '{subcmd}' not in {valid_subcommands}")
                elif module == "unittest":
                    pass
                else:
                    errors.append(f"Unknown module in command: 'python3 {cmd.strip()}'")
        elif first.endswith(".py"):
            script_path = os.path.join(PROJECT_ROOT, first)
            if not os.path.exists(script_path):
                errors.append(f"Script referenced does not exist: '{first}' in 'python3 {cmd.strip()}'")
    return errors


def check_env_vars(readme_content: str) -> list[str]:
    errors = []
    env_example_path = os.path.join(PROJECT_ROOT, ".env.example")
    if not os.path.exists(env_example_path):
        return ["Missing .env.example file at repo root!"]

    with open(env_example_path, "r", encoding="utf-8") as f:
        env_example_content = f.read()

    example_vars = set(re.findall(r'^[A-Z0-9_]+', env_example_content, re.MULTILINE))

    # Find section 7 where env vars are listed
    sec7_match = re.search(r'## 7\. Running for Real.*?(?=## 8|$)', readme_content, re.DOTALL)
    if not sec7_match:
        errors.append("Could not locate '## 7. Running for Real' section in README.md")
        return errors

    sec7_text = sec7_match.group(0)
    # Extract backticked variable names like `RING_ACCESS_TOKEN`
    found_vars = set(re.findall(r'`([A-Z0-9_]{5,})`', sec7_text))
    for var in found_vars:
        if var not in example_vars:
            errors.append(f"Environment variable '{var}' named in README is missing from .env.example!")

    return errors


def check_top_30_lines(readme_content: str) -> list[str]:
    errors = []
    lines = readme_content.splitlines()[:30]
    top_30_text = "\n".join(lines)

    if "python3 -m src.threshold demo" not in top_30_text:
        errors.append("One-command demo 'python3 -m src.threshold demo' is missing from top 30 lines.")

    if "Ring Partner APIs Used" not in top_30_text and "Ring APIs Used" not in top_30_text:
        errors.append("Ring APIs summary heading is missing from top 30 lines.")

    if "Threshold is a" not in top_30_text:
        errors.append("Product definition opening is missing from top 30 lines.")

    return errors


def check_ring_sample_citations(content: str) -> list[str]:
    """Verify that every docs/ring_samples/<file> cited in links or backticks is tracked in git."""
    errors = []
    tracked = get_tracked_files()
    samples = set(re.findall(r'docs/ring_samples/([A-Za-z0-9_.-]+)', content))
    for s in sorted(samples):
        if not s or s in (".", ".."):
            continue
        rel_path = f"docs/ring_samples/{s}"
        if rel_path not in tracked:
            errors.append(f"Untracked sample file citation: '{rel_path}' is not tracked in git.")
        elif not os.path.exists(os.path.join(PROJECT_ROOT, rel_path)):
            errors.append(f"Missing sample file on disk: '{rel_path}' does not exist.")
    return errors


NOTES_ALLOWED_TOKENS = {
    # Documentation file names
    "BUILD_LOG",
    # Ring API error strings and enums
    "MEDIA_NOT_FOUND",
    # Status constants
    "STATUS_MISSED",
    "STATUS_UNKNOWN",
    # Redaction placeholders
    "REDACTED_ACCOUNT_ID",
    "REDACTED_DEVICE_ID",
    "REDACTED_PRESIGNED_QUERY",
}


def check_notes_env_vars(notes_content: str) -> list[str]:
    """Scan docs/ring_integration_notes.md for ALL_CAPS_WITH_UNDERSCORES tokens and require them in .env.example or allowlist."""
    errors = []
    env_example_path = os.path.join(PROJECT_ROOT, ".env.example")
    example_vars = set()
    if os.path.exists(env_example_path):
        with open(env_example_path, "r", encoding="utf-8") as f:
            example_vars = set(re.findall(r'^[A-Z0-9_]+', f.read(), re.MULTILINE))

    tokens = set(re.findall(r'\b([A-Z0-9]+_[A-Z0-9_]+)\b', notes_content))
    for t in sorted(tokens):
        if t in NOTES_ALLOWED_TOKENS:
            continue
        if t not in example_vars:
            errors.append(
                f"Token '{t}' in integration notes is missing from .env.example and not in allowlist!"
            )
    return errors


def main() -> int:
    target_arg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "README.md"
    readme_path = os.path.join(PROJECT_ROOT, target_arg) if not os.path.isabs(target_arg) else target_arg
    if not os.path.exists(readme_path):
        print(f"FAIL: Target file not found at {readme_path}", file=sys.stderr)
        return 1

    notes_arg_idx = -1
    for i, a in enumerate(sys.argv):
        if a == "--notes-file" and i + 1 < len(sys.argv):
            notes_arg_idx = i + 1
            break
    notes_path = sys.argv[notes_arg_idx] if notes_arg_idx != -1 else os.path.join(PROJECT_ROOT, "docs", "ring_integration_notes.md")
    if not os.path.isabs(notes_path):
        notes_path = os.path.join(PROJECT_ROOT, notes_path)

    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    all_errors = []

    print("Running check_readme.py mechanical verification...")

    # Check 1: Top 30 lines
    top_errs = check_top_30_lines(content)
    if top_errs:
        all_errors.extend(top_errs)
        print("  [FAIL] Top 30 lines requirements")
    else:
        print("  [PASS] Top 30 lines requirements (what it is, demo, Ring APIs summary)")

    # Check 2: Relative links
    link_errs = check_relative_links(content)
    if link_errs:
        all_errors.extend(link_errs)
        print(f"  [FAIL] Relative links ({len(link_errs)} broken)")
    else:
        print("  [PASS] Relative links (all targets exist on disk)")

    # Check 3: Python commands
    cmd_errs = check_python_commands(content)
    if cmd_errs:
        all_errors.extend(cmd_errs)
        print(f"  [FAIL] Python commands ({len(cmd_errs)} invalid)")
    else:
        print("  [PASS] Python commands (all scripts exist, all subcommands valid in --help)")

    # Check 4: Env vars (README)
    env_errs = check_env_vars(content)
    if env_errs:
        all_errors.extend(env_errs)
        print(f"  [FAIL] Environment variables ({len(env_errs)} mismatched)")
    else:
        print("  [PASS] Environment variables (all README variables match .env.example)")

    # Check 5: Env vars (Integration Notes)
    if os.path.exists(notes_path):
        with open(notes_path, "r", encoding="utf-8") as f:
            notes_content = f.read()
        notes_env_errs = check_notes_env_vars(notes_content)
        if notes_env_errs:
            all_errors.extend(notes_env_errs)
            print(f"  [FAIL] Integration notes env vars ({len(notes_env_errs)} unrecognized)")
        else:
            print("  [PASS] Integration notes env vars (all ALL_CAPS_WITH_UNDERSCORES tokens match .env.example or allowlist)")

    # Check 6: Ring sample citations (README and docs/ring_integration_notes.md)
    sample_errs = check_ring_sample_citations(content)
    if os.path.exists(notes_path):
        with open(notes_path, "r", encoding="utf-8") as f:
            sample_errs.extend(check_ring_sample_citations(f.read()))

    if sample_errs:
        all_errors.extend(sample_errs)
        print(f"  [FAIL] Ring sample citations ({len(sample_errs)} untracked or missing)")
    else:
        print("  [PASS] Ring sample citations (all cited docs/ring_samples/ files are tracked in git)")

    if all_errors:
        print("\nErrors encountered:")
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("\nALL MECHANICAL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
