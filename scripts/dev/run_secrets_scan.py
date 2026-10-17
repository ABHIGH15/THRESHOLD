"""
Comprehensive computed secrets scan and pattern categorization for Threshold.
Does NOT open .env.
"""
import re
import subprocess
import sys

# 7 forbidden patterns
PATTERNS = [
    ("GitHub Personal Access Token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("Slack Token", re.compile(r"xox[baprs]-")),
    ("OpenAI / Anthropic Secret Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Bearer Token", re.compile(r"Bearer [A-Za-z0-9._-]{20,}")),
    ("JWT Token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ")),
    ("32+ Char Secret Assignment", re.compile(r"""(?i)(?:[a-z0-9_]*(?:token|key|secret|password)[a-z0-9_]*)\s*[:=]\s*["\']([a-zA-Z0-9_\-\.\/\+=]{32,})["\']""")),
]

print("=== 5a. SECRETS SCAN OVER TRACKED FILES AT HEAD ===")
tracked_files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()

tracked_matches = []
for fpath in tracked_files:
    if fpath == ".env":
        continue  # STRICT: do not open .env
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            for lno, line in enumerate(f, 1):
                for pname, pat in PATTERNS:
                    m = pat.search(line)
                    if m:
                        val = m.group(1) if m.groups() else m.group(0)
                        prefix = val[:6]
                        tracked_matches.append((pname, f"{fpath}:{lno}", prefix))
    except Exception as e:
        pass

print(f"Total pattern checks performed: {len(PATTERNS)} regex patterns across {len(tracked_files)} tracked files.")
print(f"Computed match count: {len(tracked_matches)}")
if len(tracked_matches) == 0:
    print("0 matches found across tracked files at HEAD.")
else:
    for pname, loc, prefix in tracked_matches:
        print(f"  [{pname}] {loc} prefix: {prefix}...")


print("\n=== 5b. SECRETS SCAN OVER GIT LOG -P --ALL ===")
log_proc = subprocess.run(["git", "log", "-p", "--all"], capture_output=True, text=True, errors="replace")
log_text = log_proc.stdout

log_matches = []
current_commit = "initial"
current_file = "unknown"

for line in log_text.splitlines():
    if line.startswith("commit "):
        current_commit = line.split()[1][:8]
    elif line.startswith("+++ b/"):
        current_file = line[6:]
    elif line.startswith("+") and not line.startswith("+++"):
        added_line = line[1:]
        for pname, pat in PATTERNS:
            m = pat.search(added_line)
            if m:
                val = m.group(1) if m.groups() else m.group(0)
                prefix = val[:6]
                log_matches.append((pname, f"commit {current_commit} ({current_file})", prefix))

print(f"Computed match count in git history: {len(log_matches)}")
if len(log_matches) == 0:
    print("0 matches found across git log -p --all history.")
else:
    for pname, loc, prefix in log_matches:
        print(f"  [{pname}] {loc} prefix: {prefix}...")


print("\n=== 5c. CATEGORIZATION OF 361 TOKEN|SECRET|API_KEY CODE PATTERN LINES ===")
cmd_361 = ["git", "grep", "-n", "-i", r"api[_-]\?key\|secret\|token", "--", ":!*.md", ":!evidence/*"]
lines_361 = subprocess.run(cmd_361, capture_output=True, text=True).stdout.splitlines()

total_count = len(lines_361)
print(f"Total pattern matches found: {total_count}")

cat_docs = []
cat_test = []
cat_code = []

for line in lines_361:
    parts = line.split(":", 2)
    if len(parts) < 3:
        continue
    fpath = parts[0]
    lno = parts[1]
    content = parts[2]
    stripped = content.strip()

    if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith("*"):
        cat_docs.append((fpath, lno, content))
    elif fpath.startswith("tests/") or "fake" in content.lower() or "mock" in content.lower():
        cat_test.append((fpath, lno, content))
    else:
        cat_code.append((fpath, lno, content))

print(f"Categorization breakdown (sum = {len(cat_docs) + len(cat_test) + len(cat_code)}):")
print(f"  1. Docstrings, comments, and documentation: {len(cat_docs)}")
print(f"  2. Test fixtures, mocks, and test assertions: {len(cat_test)}")
print(f"  3. Variable names, parameters, and signatures in src/: {len(cat_code)}")

print("\nSample lines from Category 1 (Docstrings & comments):")
for f, l, c in cat_docs[:3]:
    print(f"  {f}:{l}: {c.strip()[:80]}")

print("\nSample lines from Category 2 (Test fixtures & mocks):")
for f, l, c in cat_test[:3]:
    print(f"  {f}:{l}: {c.strip()[:80]}")

print("\nSample lines from Category 3 (Variable names & parameters in src/):")
for f, l, c in cat_code[:3]:
    print(f"  {f}:{l}: {c.strip()[:80]}")
