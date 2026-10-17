"""
Runs demo twice with --no-color --pace 0.
Masks non-deterministic per-run values (ports, temporary paths, random request IDs).
Computes unified diff between run 1 and run 2 to prove determinism.
"""
import difflib
import re
import subprocess
import sys

demo_cmd = [sys.executable, "-m", "src.threshold", "demo", "--no-color", "--pace", "0"]

print("=== 1. EXECUTING RUN 1 ===")
p1 = subprocess.run(demo_cmd, capture_output=True, text=True)
out1 = p1.stdout + p1.stderr
print(f"Run 1 exit code: {p1.returncode}, length: {len(out1)} chars, lines: {len(out1.splitlines())}")

print("\n=== 2. EXECUTING RUN 2 ===")
p2 = subprocess.run(demo_cmd, capture_output=True, text=True)
out2 = p2.stdout + p2.stderr
print(f"Run 2 exit code: {p2.returncode}, length: {len(out2)} chars, lines: {len(out2.splitlines())}")

MASK_REGEXES = [
    (r"/var/folders/[^\s]+", "<TEMP_PATH>"),
    (r"/tmp/[^\s]+", "<TEMP_PATH>"),
    (r"demo_req_[a-f0-9]+", "demo_req_<ID>"),
    (r"127\.0\.0\.1:\d+", "127.0.0.1:<PORT>"),
]

print("\n=== 3. MASK REGEXES USED ===")
for pat, rep in MASK_REGEXES:
    print(f"  Pattern: {pat} -> Replacement: {rep}")

print("\n=== 4. UNMASKED DIFF LINES (WHAT ACTUALLY DIFFERS BEFORE MASKING) ===")
unmasked_diff = list(difflib.unified_diff(
    out1.splitlines(keepends=True),
    out2.splitlines(keepends=True),
    fromfile="run_1_unmasked",
    tofile="run_2_unmasked"
))
if not unmasked_diff:
    print("Zero differences between unmasked runs.")
else:
    for line in unmasked_diff:
        print(line, end="")

def mask_output(text: str) -> str:
    for pat, rep in MASK_REGEXES:
        text = re.sub(pat, rep, text)
    return text

masked1 = mask_output(out1).splitlines(keepends=True)
masked2 = mask_output(out2).splitlines(keepends=True)

print("\n=== 5. MASKED UNIFIED DIFF (RUN 1 vs RUN 2) ===")
diff = list(difflib.unified_diff(masked1, masked2, fromfile="run_1_masked", tofile="run_2_masked"))
if not diff:
    print("DIFF IS EMPTY: Output is 100% deterministic between successive runs.")
else:
    for line in diff:
        print(line, end="")

assert not diff, "Masked outputs differed between run 1 and run 2!"
print("\n✓ Determinism verified.")
