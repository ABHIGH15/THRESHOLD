"""
Runs the demo in a fresh clone with env -i.
Saves the full out.txt, prints EXIT, wc -l, line matches, and checks snapshots dir.
"""
import os
import shutil
import subprocess
import sys
import tempfile

repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
work_dir = tempfile.mkdtemp(prefix="threshold_fresh_clone_")

try:
    clone_dir = os.path.join(work_dir, "c")
    print("=== 1. CLONING REPOSITORY ===")
    subprocess.run(["git", "clone", repo_dir, clone_dir], check=True, capture_output=True, text=True)

    head_proc = subprocess.run(["git", "log", "--oneline", "-1"], cwd=clone_dir, capture_output=True, text=True)
    print(f"HEAD in fresh clone: {head_proc.stdout.strip()}")

    print("\n=== 2. RUNNING DEMO UNDER env -i ===")
    fake_home = tempfile.mkdtemp(prefix="fake_home_")
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": fake_home,
    }

    demo_cmd = [sys.executable, "-m", "src.threshold", "demo", "--no-color", "--pace", "0"]
    demo_proc = subprocess.run(
        demo_cmd,
        cwd=clone_dir,
        env=clean_env,
        capture_output=True,
        text=True,
    )
    exit_code = demo_proc.returncode
    out_text = demo_proc.stdout + demo_proc.stderr

    out_file = os.path.join(clone_dir, "out.txt")
    with open(out_file, "w") as f:
        f.write(out_text)

    line_count = len(out_text.splitlines())
    print(f"EXIT={exit_code}")
    print(f"wc -l out.txt: {line_count}")

    print("\n=== 3. SEARCHING OUT.TXT FOR REQUIRED PATTERNS ===")
    patterns_to_check = ["PHOTO", "Fired at", "false alarm(s)", "/var/folders"]
    for pat in patterns_to_check:
        matches = [line for line in out_text.splitlines() if pat in line]
        print(f"Pattern '{pat}': {len(matches)} match(es)")
        for m in matches:
            print(f"  {m}")

    print("\n=== 4. CHECKING SNAPSHOTS DIRECTORY ===")
    snap_proc = subprocess.run(["ls", "snapshots"], cwd=clone_dir, capture_output=True, text=True)
    if snap_proc.returncode != 0:
        print(f"ls snapshots result: {snap_proc.stderr.strip()} (returncode: {snap_proc.returncode})")
        print("✓ Verified: No demo files were written into repository snapshots directory.")
    else:
        print(f"ls snapshots found files: {snap_proc.stdout.strip()}")

    print("\n=== 5. FULL DEMO OUTPUT (out.txt) ===")
    print(out_text)

finally:
    shutil.rmtree(work_dir, ignore_errors=True)
