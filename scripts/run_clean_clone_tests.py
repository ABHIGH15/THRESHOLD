"""
Executes clean-clone full test suite via Python subprocess (timeout=900).
Prints: pwd, HEAD, EXIT, last 8 lines.
"""
import os
import shutil
import subprocess
import sys
import tempfile

repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
temp_clone_dir = tempfile.mkdtemp(prefix="threshold_clean_test_")

try:
    # 1. Clone local repo
    clone_cmd = ["git", "clone", repo_dir, temp_clone_dir]
    subprocess.run(clone_cmd, check=True, capture_output=True, text=True)

    # 2. Get HEAD commit in clone
    head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_clone_dir, capture_output=True, text=True)
    head_commit = head_proc.stdout.strip()

    print(f"PWD:  {temp_clone_dir}")
    print(f"HEAD: {head_commit}")

    # 3. Run full unittest suite with timeout=900
    test_cmd = [sys.executable, "-m", "unittest"]
    test_proc = subprocess.run(
        test_cmd,
        cwd=temp_clone_dir,
        capture_output=True,
        text=True,
        timeout=900,
    )
    exit_code = test_proc.returncode
    output = test_proc.stdout + test_proc.stderr

    print(f"EXIT: {exit_code}")
    print("--- LAST 8 LINES OF TEST OUTPUT ---")
    last_8 = output.strip().splitlines()[-8:]
    for l in last_8:
        print(l)

finally:
    shutil.rmtree(temp_clone_dir, ignore_errors=True)
