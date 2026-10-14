#!/usr/bin/env python3
"""
scripts/run_demo.py — Executable entry point for Threshold Hackathon Demo Runner.

Runs the complete 3-beat emotional demonstration:
  Beat 1: Empathy & Morning Routine Baseline (07:30 CDT)
  Beat 2: The Silence & Pattern-Absence Detection (09:31 CDT)
  Beat 3: Caretaker Resolution & Graceful Recovery (09:35 - 10:00 CDT)

Usage:
  python3 scripts/run_demo.py               # Standard paced demonstration
  python3 scripts/run_demo.py --interactive # Step-by-step judge presentation mode
  python3 scripts/run_demo.py --open        # Open web dashboard in default browser
  python3 scripts/run_demo.py --fast        # Non-blocking rapid demonstration
"""

import os
import sys

# Ensure repository root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.threshold.demo import main

if __name__ == "__main__":
    sys.exit(main())
