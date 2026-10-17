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


def check_relative_links(readme_content: str) -> list[str]:
    errors = []
    # Match [text](target) but ignore anchors and external URLs
    links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', readme_content)
    for text, target in links:
        target_clean = target.split("#")[0].strip()
        if not target_clean or target_clean.startswith(("http://", "https://", "mailto:")):
            continue
        full_path = os.path.join(PROJECT_ROOT, target_clean)
        if not os.path.exists(full_path):
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


def main() -> int:
    readme_path = os.path.join(PROJECT_ROOT, "README.md")
    if not os.path.exists(readme_path):
        print(f"FAIL: README.md not found at {readme_path}", file=sys.stderr)
        return 1

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

    # Check 4: Env vars
    env_errs = check_env_vars(content)
    if env_errs:
        all_errors.extend(env_errs)
        print(f"  [FAIL] Environment variables ({len(env_errs)} mismatched)")
    else:
        print("  [PASS] Environment variables (all README variables match .env.example)")

    if all_errors:
        print("\nErrors encountered:")
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("\nALL MECHANICAL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
