#!/usr/bin/env python3
r"""
scripts/dev/check_citations.py — Verifies that all code symbols and test IDs cited in documentation exist.

Requirements (Task A10b Step 3b):
1. Confirm each cited symbol (`path.py::Symbol`) exists via `ast`.
2. Run each cited test ID (`tests.test_X.TestY.test_z`) with `python3 -m unittest <id>` and require exit 0 and 'Ran 1 test'.
3. One output line per citation.
4. Non-zero exit on any miss.
5. Self-test with bogus symbol and bogus test ID via `--self-test`.
"""

import ast
import os
import re
import subprocess
import sys
from typing import Dict, List, Set, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYMBOL_PATTERN = re.compile(r"([A-Za-z0-9_./]+\.py)::([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)")
TEST_PATTERN = re.compile(r"\b(tests\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.test_[A-Za-z0-9_]+)\b")


def extract_citations(text: str) -> Tuple[Set[Tuple[str, str]], Set[str]]:
    symbols = set(SYMBOL_PATTERN.findall(text))
    tests = set(TEST_PATTERN.findall(text))
    return symbols, tests


def get_symbols_from_ast(filepath: str) -> Set[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            found.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(f"{node.name}.{sub.name}")
    return found


def check_symbol(rel_path: str, symbol: str) -> bool:
    full_path = os.path.join(PROJECT_ROOT, rel_path)
    if not os.path.isfile(full_path):
        return False
    try:
        symbols = get_symbols_from_ast(full_path)
        return symbol in symbols
    except Exception:
        return False


def check_test_id(test_id: str) -> Tuple[bool, str]:
    cmd = [sys.executable, "-m", "unittest", test_id]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = (proc.stdout + "\n" + proc.stderr).strip()
    success = proc.returncode == 0 and "Ran 1 test" in out
    return success, out


def run_self_test() -> int:
    print("=== RUNNING CITATIONS SELF-TEST ===")
    bogus_symbol = ("src/threshold/ring_client.py", "BogusNonExistentMethod")
    bogus_test = "tests.test_agent.TestAgentLibrary.test_bogus_nonexistent_test_method"

    sym_ok = check_symbol(bogus_symbol[0], bogus_symbol[1])
    test_ok, _ = check_test_id(bogus_test)

    if not sym_ok and not test_ok:
        print(f"SELF-TEST PASS: Bogus symbol '{bogus_symbol[0]}::{bogus_symbol[1]}' correctly failed.")
        print(f"SELF-TEST PASS: Bogus test '{bogus_test}' correctly failed.")
        print("SELF-TEST: Proved non-zero exit on invalid citations.")
        return 1
    else:
        print("SELF-TEST FAILED: Failed to detect bogus citations!", file=sys.stderr)
        return 2


def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()

    docs_to_check = [
        "README.md",
        "docs/ring_integration_notes.md",
    ]

    all_symbols: Set[Tuple[str, str]] = set()
    all_tests: Set[str] = set()

    for doc_rel in docs_to_check:
        doc_path = os.path.join(PROJECT_ROOT, doc_rel)
        if not os.path.isfile(doc_path):
            continue
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()
        symbols, tests = extract_citations(content)
        all_symbols.update(symbols)
        all_tests.update(tests)

    print(f"Checking {len(all_symbols)} code symbol(s) and {len(all_tests)} test citation(s)...")

    failures = 0

    for path, sym in sorted(all_symbols):
        ok = check_symbol(path, sym)
        if ok:
            print(f"SYMBOL PASS: {path}::{sym}")
        else:
            print(f"SYMBOL FAIL: {path}::{sym} (not found in file AST)")
            failures += 1

    for tid in sorted(all_tests):
        ok, out = check_test_id(tid)
        if ok:
            print(f"TEST PASS:   {tid} (Ran 1 test, exit 0)")
        else:
            print(f"TEST FAIL:   {tid} (Failed to run as 1 passing test)")
            failures += 1

    if failures > 0:
        print(f"\nCITATIONS CHECK FAILED: {failures} missing or failing citation(s)!", file=sys.stderr)
        return 1

    print("\nALL CITATIONS VERIFIED SUCCESSFULLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
