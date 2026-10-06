#!/usr/bin/env python3
"""
find_chat_id.py -- Helps you find the exact Chat ID for your channel or group.

Usage:
    python find_chat_id.py
"""

import sys
import requests

token = input("Enter your TELEGRAM_BOT_TOKEN: ").strip()
if not token:
    print("Bot token cannot be empty.")
    sys.exit(1)

print("\nFetching recent updates from Telegram...")
try:
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    data = resp.json()
except Exception as e:
    print(f"Error connecting to Telegram: {e}")
    sys.exit(1)

if not data.get("ok"):
    print(f"Telegram API Error: {data.get('description')}")
    sys.exit(1)

results = data.get("result", [])
if not results:
    print("\n⚠️ No recent updates found!")
    print("To make your channel ID appear:")
    print("1. Open your Telegram channel where the bot is an admin.")
    print("2. Send any dummy message (e.g. 'hello').")
    print("3. Run this script again!\n")
    sys.exit(0)

print("\nFound the following chats/channels:")
print("=" * 60)
seen_chats = set()
for update in results:
    chat = None
    msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member", {}).get("chat")
    if msg and isinstance(msg, dict):
        chat = msg.get("chat") if "chat" in msg else msg
    
    if chat and isinstance(chat, dict):
        cid = chat.get("id")
        title = chat.get("title") or chat.get("username") or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
        ctype = chat.get("type", "unknown")
        if cid and cid not in seen_chats:
            seen_chats.add(cid)
            print(f"  📌 Title: {title}")
            print(f"     Type:  {ctype}")
            print(f"     Chat ID to copy: {cid}")
            print("-" * 60)

print("\nCopy the Chat ID above (including the minus sign '-') and paste it into GitHub Secrets as TELEGRAM_CHAT_ID!")
