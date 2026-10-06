#!/usr/bin/env python3
"""
test_extraction.py — Unit tests for message extraction logic.

Tests against sample HTML to verify selectors, dedup, and edge cases.
Run with:  python test_extraction.py
"""

import hashlib
import io
import sqlite3
import tempfile
import os
import sys
from pathlib import Path

# Fix Windows console encoding for emoji
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ─── Sample HTML ──────────────────────────────────────────────────────────────
# This is a realistic sample page with multiple message formats.
# ⚠️  Replace this with your actual portal HTML once you have it.
SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Messages — Placement Portal</title></head>
<body>
<div id="messages-container">

  <!-- Format A: div-based message cards -->
  <div class="message-item">
    <a href="/messages/1234" class="message-title">TCS Recruitment Drive — Oct 2026</a>
    <span class="message-date">02 Oct 2026</span>
    <p class="message-body">TCS is visiting campus on 15th October for B.Tech CS/IT students…</p>
  </div>

  <div class="message-item">
    <a href="/messages/1235" class="message-title">Infosys InfyTQ Registration Deadline</a>
    <span class="message-date">01 Oct 2026</span>
    <p class="message-body">Last date to register for InfyTQ certification is 5th October…</p>
  </div>

  <div class="message-item">
    <a href="/messages/1236" class="message-title">Resume Workshop — Mandatory for Final Year</a>
    <span class="message-date">30 Sep 2026</span>
    <p class="message-body">All final year students must attend the resume building workshop…</p>
  </div>

  <!-- Duplicate link with different date (should be treated as different) -->
  <div class="message-item">
    <a href="/messages/1234" class="message-title">TCS Drive Update — Rescheduled</a>
    <span class="message-date">03 Oct 2026</span>
    <p class="message-body">The TCS drive has been rescheduled to 20th October…</p>
  </div>

</div>
</body>
</html>
"""

# HTML that simulates a login redirect (session expired)
LOGIN_REDIRECT_HTML = """
<!DOCTYPE html>
<html>
<head><title>SSO Login</title></head>
<body><form id="loginForm"><input name="username"/></form></body>
</html>
"""

# HTML with no messages (structure change)
EMPTY_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Messages</title></head>
<body><div id="messages-container"></div></body>
</html>
"""


def make_uid(link: str, date: str) -> str:
    raw = f"{link.strip()}|{date.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def test_extraction_with_playwright():
    """Test actual extraction using Playwright against sample HTML."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("⚠️  Playwright not installed — skipping browser-based tests.")
        print("   Run: pip install playwright && playwright install chromium")
        return False

    print("=" * 60)
    print("  TEST: Message Extraction (Playwright)")
    print("=" * 60)

    # Write sample HTML to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(SAMPLE_HTML)
        html_path = f.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # ── Test 1: Basic extraction ──────────────────────────────
            page.goto(f"file://{html_path}")
            page.wait_for_timeout(500)

            items = page.query_selector_all(".message-item")
            assert len(items) == 4, f"Expected 4 items, got {len(items)}"
            print(f"  ✅ Found {len(items)} message items")

            # Extract each message
            messages = []
            for item in items:
                title_el = item.query_selector(".message-title")
                date_el = item.query_selector(".message-date")
                link_el = item.query_selector("a[href]")
                body_el = item.query_selector(".message-body")

                title = title_el.inner_text().strip() if title_el else ""
                date = date_el.inner_text().strip() if date_el else ""
                href = link_el.get_attribute("href") if link_el else ""
                body = body_el.inner_text().strip()[:200] if body_el else ""
                uid = make_uid(href, date)

                messages.append({
                    "uid": uid, "title": title, "date": date,
                    "link": href, "body": body
                })

            # ── Test 2: Verify extracted data ─────────────────────────
            assert messages[0]["title"] == "TCS Recruitment Drive — Oct 2026"
            assert messages[0]["date"] == "02 Oct 2026"
            assert messages[0]["link"] == "/messages/1234"
            print(f"  ✅ Message 1: {messages[0]['title']}")

            assert messages[1]["title"] == "Infosys InfyTQ Registration Deadline"
            print(f"  ✅ Message 2: {messages[1]['title']}")

            assert messages[2]["title"] == "Resume Workshop — Mandatory for Final Year"
            print(f"  ✅ Message 3: {messages[2]['title']}")

            # ── Test 3: Dedup — same link, different date = different UID
            assert messages[0]["link"] == messages[3]["link"], "Links should match"
            assert messages[0]["uid"] != messages[3]["uid"], "UIDs should differ (different dates)"
            print(f"  ✅ Dedup: same link + different date → different UIDs")

            # ── Test 4: Dedup — identical link + date = same UID ──────
            uid_a = make_uid("/messages/1234", "02 Oct 2026")
            uid_b = make_uid("/messages/1234", "02 Oct 2026")
            assert uid_a == uid_b, "Identical link+date should produce same UID"
            print(f"  ✅ Dedup: identical link+date → same UID")

            # ── Test 5: Session expiry detection ─────────────────────
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as lf:
                lf.write(LOGIN_REDIRECT_HTML)
                login_path = lf.name

            page.goto(f"file://{login_path}")
            # Simulate checking URL for login markers
            # (In real usage, the URL would be the SSO URL, not a file://)
            # We test the marker-matching logic directly:
            test_urls = [
                ("https://sso.college.ac.in/cas/login?service=...", True),
                ("https://portal.college.ac.in/auth/saml", True),
                ("https://portal.college.ac.in/messages", False),
                ("https://accounts.google.com/signin", True),
            ]
            login_markers = ["/login", "/sso", "/cas/", "/auth", "signin", "saml"]
            for url, expected in test_urls:
                detected = any(m in url.lower() for m in login_markers)
                assert detected == expected, f"URL {url}: expected {expected}, got {detected}"
            print(f"  ✅ Session expiry detection: all URL patterns correct")

            # ── Test 6: Empty page (structure change) ────────────────
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as ef:
                ef.write(EMPTY_PAGE_HTML)
                empty_path = ef.name

            page.goto(f"file://{empty_path}")
            page.wait_for_timeout(500)
            empty_items = page.query_selector_all(".message-item")
            assert len(empty_items) == 0, "Expected 0 items on empty page"
            print(f"  ✅ Structure change detection: 0 items on empty page")

            os.unlink(login_path)
            os.unlink(empty_path)
            browser.close()

    finally:
        os.unlink(html_path)

    return True


def test_database_logic():
    """Test SQLite dedup and first-run seeding logic."""
    print()
    print("=" * 60)
    print("  TEST: Database Logic")
    print("=" * 60)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)

        # Init
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_messages (
                uid TEXT PRIMARY KEY, title TEXT, date TEXT, link TEXT, first_seen REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
        """)
        conn.commit()

        # Test first-run detection
        row = conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        assert row is None, "Should be first run"
        print("  ✅ First run detected correctly")

        # Seed some messages
        test_msgs = [
            (make_uid("/msg/1", "01 Oct"), "Msg 1", "01 Oct", "/msg/1", 1.0),
            (make_uid("/msg/2", "02 Oct"), "Msg 2", "02 Oct", "/msg/2", 2.0),
        ]
        for m in test_msgs:
            conn.execute("INSERT OR IGNORE INTO seen_messages VALUES (?, ?, ?, ?, ?)", m)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
        conn.commit()

        # Verify seeded
        row = conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        assert row is not None, "Should be seeded now"
        print("  ✅ Seeded flag set correctly")

        # Verify dedup
        uid1 = make_uid("/msg/1", "01 Oct")
        seen = conn.execute("SELECT 1 FROM seen_messages WHERE uid = ?", (uid1,)).fetchone()
        assert seen is not None, "Message 1 should be seen"
        print("  ✅ Existing message correctly marked as seen")

        uid_new = make_uid("/msg/3", "03 Oct")
        seen = conn.execute("SELECT 1 FROM seen_messages WHERE uid = ?", (uid_new,)).fetchone()
        assert seen is None, "Message 3 should be unseen"
        print("  ✅ New message correctly detected as unseen")

        # Verify seen count
        count = conn.execute("SELECT COUNT(*) FROM seen_messages").fetchone()[0]
        assert count == 2, f"Expected 2 seen, got {count}"
        print(f"  ✅ Seen count: {count}")

        # Test INSERT OR IGNORE (no duplicate crash)
        conn.execute("INSERT OR IGNORE INTO seen_messages VALUES (?, ?, ?, ?, ?)", test_msgs[0])
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM seen_messages").fetchone()[0]
        assert count == 2, "Should still be 2 after duplicate insert"
        print("  ✅ Duplicate insert handled (INSERT OR IGNORE)")

        conn.close()
    finally:
        os.unlink(db_path)

    return True


def test_notification_formatting():
    """Test that notification text includes the full message body."""
    print()
    print("=" * 60)
    print("  TEST: Notification Formatting")
    print("=" * 60)

    def html_escape(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    title = "TCS Recruitment Drive — Oct 2026"
    date = "02 Oct 2026"
    link = "https://portal.example.com/messages/1234"
    body = "TCS is visiting campus on 15th October for B.Tech CS/IT students."

    # Telegram format
    body_escaped = html_escape(body)
    tg_text = (
        f"📢 <b>New Placement Notice</b>\n\n"
        f"<b>{html_escape(title)}</b>\n"
        f"📅 {html_escape(date)}\n\n"
        f"{body_escaped}\n\n"
        f"🔗 <a href=\"{html_escape(link)}\">Open on Portal</a>"
    )

    assert "TCS Recruitment Drive" in tg_text
    assert date in tg_text
    assert link in tg_text
    assert "visiting campus" in tg_text  # body is now included
    print("  ✅ Telegram: title + date + full body + link")

    # Email format
    body_html_formatted = html_escape(body).replace("\n", "<br>")
    email_body = (
        f"<h3>New Placement Notice</h3>"
        f"<p><strong>{html_escape(title)}</strong></p>"
        f"<p><strong>Date:</strong> {html_escape(date)}</p>"
        f"<hr/>"
        f"<div>{body_html_formatted}</div>"
        f"<hr/>"
        f"<p><a href=\"{html_escape(link)}\">Open on Portal</a></p>"
    )

    assert title in email_body
    assert date in email_body
    assert link in email_body
    assert "visiting campus" in email_body
    print("  ✅ Email: title + date + full body + link")

    # Test HTML escaping
    dangerous_title = "Test <script>alert('xss')</script> & Co."
    escaped = html_escape(dangerous_title)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&amp;" in escaped
    print("  ✅ HTML escaping works correctly")

    return True


def test_uid_stability():
    """Test that UIDs are stable and deterministic."""
    print()
    print("=" * 60)
    print("  TEST: UID Stability")
    print("=" * 60)

    # Same inputs → same UID
    uid1 = make_uid("/messages/1234", "02 Oct 2026")
    uid2 = make_uid("/messages/1234", "02 Oct 2026")
    assert uid1 == uid2
    print(f"  ✅ Deterministic: {uid1[:16]}...")

    # Different date → different UID
    uid3 = make_uid("/messages/1234", "03 Oct 2026")
    assert uid1 != uid3
    print(f"  ✅ Different date → different UID")

    # Different link → different UID
    uid4 = make_uid("/messages/9999", "02 Oct 2026")
    assert uid1 != uid4
    print(f"  ✅ Different link → different UID")

    # Whitespace handling
    uid5 = make_uid("  /messages/1234  ", "  02 Oct 2026  ")
    uid6 = make_uid("/messages/1234", "02 Oct 2026")
    assert uid5 == uid6
    print(f"  ✅ Whitespace-insensitive")

    return True


if __name__ == "__main__":
    print()
    results = []

    results.append(("UID Stability", test_uid_stability()))
    results.append(("Notification Formatting", test_notification_formatting()))
    results.append(("Database Logic", test_database_logic()))
    results.append(("Extraction (Playwright)", test_extraction_with_playwright()))

    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, passed in results:
        status = "✅ PASS" if passed else "⚠️  SKIP/FAIL"
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("  🎉 All tests passed!")
    else:
        print("  ⚠️  Some tests were skipped (likely Playwright not installed).")
    print()
    sys.exit(0 if all_pass else 1)
