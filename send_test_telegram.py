#!/usr/bin/env python3
"""
send_test_telegram.py -- Send an instant test message to your Telegram chat/channel.

Usage:
    python send_test_telegram.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("=" * 60)
    print("Enter your Telegram details to test:")
    print("=" * 60)
    if not TOKEN:
        TOKEN = input("Enter TELEGRAM_BOT_TOKEN: ").strip()
    if not CHAT_ID:
        CHAT_ID = input("Enter TELEGRAM_CHAT_ID: ").strip()

if not TOKEN or not CHAT_ID:
    print("Error: Both token and chat_id are required.")
    sys.exit(1)

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
payload = {
    "chat_id": CHAT_ID,
    "text": (
        "🎉 <b>Placement Watcher: Test Message</b>\n\n"
        "Your Telegram bot is successfully connected and working!\n"
        "You will receive alerts here whenever a new notice is posted."
    ),
    "parse_mode": "HTML",
}

print(f"Sending test message to chat {CHAT_ID}...")
try:
    resp = requests.post(url, json=payload, timeout=15)
    data = resp.json()
    if data.get("ok"):
        print("✅ SUCCESS! Test message sent to your Telegram.")
    else:
        print(f"❌ Telegram API Error: {data.get('description')}")
        print(f"Details: {data}")
except Exception as e:
    print(f"❌ Connection error: {e}")
