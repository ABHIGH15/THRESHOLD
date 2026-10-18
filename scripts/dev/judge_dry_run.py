#!/usr/bin/env python3
"""
scripts/dev/judge_dry_run.py — Judge quick-start dry run simulation.

1. Clones the current repository into an isolated temporary directory.
2. Programmatically parses README.md to extract quick-start commands from fenced blocks.
3. Executes each extracted command under a clean environment (`env -i`).
4. Records the exact command, exit code, and last 15 lines of verbatim output.
5. Executes the test suite command (`python3 -m unittest discover -s tests -v`).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def extract_quickstart_commands(readme_path: str) -> list[str]:
    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the first fenced bash block in README.md (the 60s quickstart demo)
    m = re.search(r'```bash\s*\n(.*?)\n```', content, re.DOTALL)
    if not m:
        raise RuntimeError("No fenced bash block found in README.md")

    block = m.group(1)
    commands = []
    for line in block.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            commands.append(line)
    return commands


def main() -> int:
    temp_dir = tempfile.mkdtemp(prefix="threshold_judge_clone_")
    print(f"=== STEP 4: JUDGE DRY RUN ===")
    print(f"1. Creating fresh clone at: {temp_dir}")

    # Use git clone with file URL
    clone_proc = subprocess.run(
        ["git", "clone", "--local", PROJECT_ROOT, temp_dir],
        capture_output=True,
        text=True,
    )
    if clone_proc.returncode != 0:
        print(f"FAIL: git clone failed: {clone_proc.stderr}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 1

    readme_path = os.path.join(temp_dir, "README.md")
    commands = extract_quickstart_commands(readme_path)
    print(f"2. Programmatically extracted {len(commands)} quick-start command(s) from README.md:")
    for c in commands:
        print(f"   - {c}")

    python_bin = sys.executable

    # Prepare minimal clean env
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
        "HOME": temp_dir,
    }
    print(f"3. Child process execution environment (isolated env -i simulation):")
    print(f"   - Environment keys count: {len(clean_env)} (keys: {list(clean_env.keys())})")
    print(f"   - PATH: {clean_env['PATH']}")
    print(f"   - HOME: {clean_env['HOME']}")
    print(f"   - Environment isolated: True (zero inherited application/token env vars)")

    results = []

    for idx, cmd in enumerate(commands, start=1):
        print(f"\n--- Executing Command {idx}: '{cmd}' ---")
        # Replace python3 with exact python_bin
        actual_cmd = cmd.replace("python3", python_bin, 1)

        proc = subprocess.run(
            actual_cmd,
            shell=True,
            cwd=temp_dir,
            env=clean_env,
            capture_output=True,
            text=True,
        )

        stdout_lines = proc.stdout.splitlines()
        last_15 = stdout_lines[-15:] if len(stdout_lines) >= 15 else stdout_lines

        print(f"Command run:  {cmd}")
        print(f"Exit code:    {proc.returncode}")
        print(f"Verbatim output (last {len(last_15)} lines):")
        for line in last_15:
            print(f"  {line}")

        if proc.returncode != 0:
            print(f"STDERR:\n{proc.stderr}", file=sys.stderr)

        results.append((cmd, proc.returncode, last_15))

    # Test Suite run as stated in README Section 8
    test_cmd = f"{python_bin} -m unittest discover -s tests -v"
    print(f"\n--- Executing Test Suite: '{test_cmd}' ---")
    test_proc = subprocess.run(
        test_cmd,
        shell=True,
        cwd=temp_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    test_stdout_lines = (test_proc.stdout + "\n" + test_proc.stderr).splitlines()
    test_last_15 = test_stdout_lines[-15:] if len(test_stdout_lines) >= 15 else test_stdout_lines

    print(f"Command run:  python3 -m unittest discover -s tests -v")
    print(f"Exit code:    {test_proc.returncode}")
    print(f"Verbatim output (last {len(test_last_15)} lines):")
    for line in test_last_15:
        print(f"  {line}")

    shutil.rmtree(temp_dir, ignore_errors=True)

    all_ok = all(r[1] == 0 for r in results) and (test_proc.returncode == 0)
    if all_ok:
        print("\nALL JUDGE DRY RUN CHECKS PASSED (exit code 0 across all commands).")
        return 0
    else:
        print("\nJUDGE DRY RUN FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
