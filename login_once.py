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
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)

        print("1. Log in with your college email & password on the SSO page.")
        print("2. Wait until the browser redirects BACK to the placement blog.")
        print("   (You must see the placement notices/messages on screen!)")
        print("3. Return here and press ENTER.")
        print()

        input(">>> Press ENTER once you are logged in and looking at the placement blog: ")
        
        # Check all open tabs/pages in context
        all_pages = context.pages
        print(f"\n[INFO] Open tabs detected: {len(all_pages)}")
        for idx, p in enumerate(all_pages):
            print(f"  Tab {idx + 1}: {p.url}")

        # Pick the page on the placement portal domain, or the newest tab
        active_page = None
        for p in reversed(all_pages):
            if "campus.placements.iitb.ac.in" in p.url:
                active_page = p
                break
        if not active_page:
            active_page = all_pages[-1]

        current_url = active_page.url
        print(f"[INFO] Active URL: {current_url}")

        # Save the full browser state (cookies, localStorage, etc.)
        context.storage_state(path=str(STATE_FILE))
        print(f"\n[SUCCESS] Session saved to {STATE_FILE}")

        # Verify session works
        print("\nVerifying session with headless browser...")
        test_context = browser.new_context(storage_state=str(STATE_FILE))
        test_page = test_context.new_page()
        try:
            resp = test_page.goto(PORTAL_URL, wait_until="networkidle", timeout=30_000)
            if "sso" in test_page.url.lower():
                print("[ERROR] Verification failed: session redirected to SSO. Please re-run login_once.py and log in again.")
            else:
                print(f"[VERIFIED] Session active! Page Title: {test_page.title()}")
                print(f"[VERIFIED] Final URL: {test_page.url}")
        except Exception as e:
            print(f"[WARNING] Verification check had an error: {e}")
        finally:
            test_context.close()

        # Generate base64 encoding directly
        import base64
        with open(STATE_FILE, "rb") as f:
            b64_str = base64.b64encode(f.read()).decode("utf-8")

        print("\n" + "=" * 60)
        print("  STATE_JSON_B64 — Copy this to GitHub Secrets")
        print("=" * 60)
        print(b64_str)
        print("=" * 60)
        print("Steps:")
        print("  1. Go to your GitHub repo -> Settings -> Secrets and variables -> Actions")
        print("  2. Click 'New repository secret'")
        print("  3. Name: STATE_JSON_B64")
        print("  4. Value: paste the base64 string above")
        print("  5. Click 'Add secret'\n")

        browser.close()


if __name__ == "__main__":
    main()
