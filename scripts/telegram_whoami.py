#!/usr/bin/env python3
"""
scripts/telegram_whoami.py — Resolve and print Telegram Chat ID for Threshold.

Reads TELEGRAM_BOT_TOKEN from environment / .env, queries getUpdates,
identifies the latest incoming message or callback, and outputs the chat ID.
Never logs or prints secret tokens.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.probe_ring import load_env
from src.threshold.notifier import redact_telegram_token


def get_whoami() -> int:
    load_env(".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not token.strip():
        print("ERROR: TELEGRAM_BOT_TOKEN is not set in environment or .env file.", file=sys.stderr)
        print("Please configure TELEGRAM_BOT_TOKEN in .env first.", file=sys.stderr)
        return 1

    token = token.strip()
    api_base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    url = f"{api_base}/bot{token}/getUpdates"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Threshold/1.0", "Accept": "application/json"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        safe_msg = redact_telegram_token(str(exc), token)
        print(f"ERROR: Failed to contact Telegram API: {safe_msg}", file=sys.stderr)
        return 1

    if not data.get("ok"):
        safe_desc = redact_telegram_token(data.get("description", "Unknown error"), token)
        print(f"ERROR: Telegram API returned not ok: {safe_desc}", file=sys.stderr)
        return 1

    updates = data.get("result", [])
    if not updates:
        print("No recent interactions found for this bot.")
        print("Please open Telegram, send a message (e.g. /start or 'hello') to your bot, and run this script again.")
        return 0

    # Look backwards from the most recent update
    for update in reversed(updates):
        chat = None
        user = None

        if "message" in update:
            chat = update["message"].get("chat")
            user = update["message"].get("from")
        elif "callback_query" in update:
            msg = update["callback_query"].get("message")
            if msg:
                chat = msg.get("chat")
            user = update["callback_query"].get("from")
        elif "channel_post" in update:
            chat = update["channel_post"].get("chat")

        if chat and "id" in chat:
            chat_id = chat["id"]
            chat_type = chat.get("type", "unknown")
            username = (user or {}).get("username") or chat.get("username") or "N/A"
            first_name = (user or {}).get("first_name") or chat.get("first_name") or ""
            last_name = (user or {}).get("last_name") or chat.get("last_name") or ""
            full_name = f"{first_name} {last_name}".strip() or "N/A"

            print("=" * 60)
            print("THRESHOLD TELEGRAM IDENTITY RESOLVED")
            print("=" * 60)
            print(f"Chat ID:   {chat_id}")
            print(f"Chat Type: {chat_type}")
            print(f"User:      {full_name} (@{username})")
            print("-" * 60)
            print(f"Configuration line for .env:\nTELEGRAM_CHAT_ID={chat_id}")
            print("=" * 60)
            return 0

    print("Updates found, but none contained a valid chat object.")
    print("Please send a regular text message to your bot and run this script again.")
    return 0


if __name__ == "__main__":
    sys.exit(get_whoami())
