#!/usr/bin/env python3
"""
login_once.py — One-time manual login to the placement portal.

Opens a visible Chromium window and navigates to the placement portal,
which will redirect to the SSO login page. Log in with your college
credentials, then press Enter in the terminal to save the session.

Copy state.json to the VM where watch.py runs.

Usage:
    python login_once.py
"""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

STATE_FILE = Path(__file__).parent / "state.json"
PORTAL_URL = "https://campus.placements.iitb.ac.in/blog/placement/"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Navigate directly to the portal — it will redirect to SSO login.
        print("=" * 60)
        print("  PLACEMENT PORTAL LOGIN")
        print("=" * 60)
        print()
        print(f"Opening: {PORTAL_URL}")
        print("(This will redirect to the SSO login page.)")
        print()

        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)

        print("1. Log in with your college email & password (SSO).")
        print("2. Make sure you reach the messages/notices page.")
        print("3. Come back here and press ENTER to save the session.")
        print()
        input(">>> Press ENTER after you've logged in successfully... ")

        # Save the full browser state (cookies, localStorage, etc.)
        context.storage_state(path=str(STATE_FILE))
        print(f"\nSession saved to {STATE_FILE}")
        print("Copy this file to your VM with:")
        print(f"  scp {STATE_FILE.name} user@your-vm:/path/to/watcher/")

        browser.close()


if __name__ == "__main__":
    main()
