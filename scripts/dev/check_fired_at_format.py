"""
Compares format_fired_at and format_escalation_text in src/threshold/notifier.py
against ConsoleNotifier output in Scene 3.
"""
from datetime import datetime, timezone
import os
import sys

from src.threshold.models import Routine
from src.threshold.notifier import format_fired_at, format_escalation_text

fired_at_input = "2026-10-13T01:31:00Z"
timezone_name = "America/Chicago"

routine = Routine(
    property_id="prop_demo",
    name="Evening check-in",
    device_id="demo-cam-front-door",
    expected_event_type="motion_detected",
    window_start="18:00",
    window_end="20:00",
    timezone="America/Chicago",
    days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    escalation_delay_minutes=30,
)

print("=== 1. CALLING format_fired_at DIRECTLY ===")
print("Input fired_at:     ", fired_at_input)
print("Input timezone_name:", timezone_name)
formatted_fired_at = format_fired_at(fired_at_input, timezone_name)
print("Result of format_fired_at:", formatted_fired_at)

print("\n=== 2. CALLING format_escalation_text (REAL TELEGRAM FORMATTER) ===")
telegram_text = format_escalation_text(routine, fired_at_input)
print(telegram_text)

print("\n=== 3. COMPARISON & CODE CITATION ===")
print("QUESTION: Do REAL Telegram alerts show UTC here too, or only the demo?")
print("ANSWER: ONLY THE DEMO (ConsoleNotifier) shows UTC. Real Telegram alerts show local time.")
print()
print("CITATIONS:")
print("1. In src/threshold/notifier.py:72-88:")
print("   def format_fired_at(fired_at, timezone_name):")
print("       clean_str = fired_at.replace('Z', '+00:00') ...")
print("       tz = ZoneInfo(timezone_name)")
print("       return dt.astimezone(tz).strftime('%H:%M %Z (%Y-%m-%d)')")
print("   Output for '2026-10-13T01:31:00Z' in 'America/Chicago': '20:31 CDT (2026-10-12)'.")
print()
print("2. In src/threshold/notifier.py:167-186:")
print("   def format_escalation_text(routine, fired_at, device_health):")
print("       fired_str = format_fired_at(fired_at, routine.timezone)")
print("       ... Fired at: {fired_str} ...")
print()
print("3. In src/threshold/notifier.py:657:")
print("   TelegramNotifier.send_escalation calls format_escalation_text(routine, fired_at, device_health).")
print("   Therefore, real Telegram alerts display the localized time: '20:31 CDT (2026-10-12)'.")
print()
print("4. In src/threshold/notifier.py:335:")
print("   ConsoleNotifier.send_escalation does NOT call format_fired_at:")
print("   print(f'Fired at: {fired_at} (Date: {local_date})')")
print("   It directly prints the raw UTC string passed in: '2026-10-13T01:31:00Z (Date: 2026-10-12)'.")
