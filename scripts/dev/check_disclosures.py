"""
Verifies the disclosures added in Task A9c:
1. Banner line: Device health polls: SIMULATED
2. Closing-table row: Device health polls
3. Agent tool execution row update
4. Scene 6 softened statement: unaffected by the agent
"""
from src.threshold.demo import DemoPrinter

printer = DemoPrinter(no_color=True)

print("=== 1. DEMO BANNER DISCLOSURE ===")
printer.print_banner()

print("\n=== 2. CLOSING TABLE DISCLOSURES ===")
printer.print_closing_table()

print("\n=== 3. SCENE 6 STATEMENT (from demo.py source) ===")
with open("src/threshold/demo.py") as f:
    demo_source = f.read()

for line_num, line in enumerate(demo_source.splitlines(), 1):
    if "unaffected by the agent" in line:
        print(f"Line {line_num}: {line.strip()}")
    elif "Device health polls" in line:
        print(f"Line {line_num}: {line.strip()}")
    elif "placeholder JPEG" in line:
        print(f"Line {line_num}: {line.strip()}")
