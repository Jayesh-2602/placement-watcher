#!/usr/bin/env python3
"""
watch.py — Placement Portal Notification Watcher

Polls the IITB placement portal's messages page every ~5 minutes using a
saved Playwright session, detects new messages, and sends notifications
via Telegram + Email.

Reliability guarantees:
  - Deduplication by message ID/link + date (SQLite).
  - Messages are marked "seen" ONLY after at least one notification succeeds.
  - First run seeds the DB without sending notifications.
  - Session-expiry and page-structure-change detection with alerts.
  - Healthchecks.io heartbeat after every successful cycle.
  - Rotating log file + stdout logging.
  - Random jitter on the polling interval.

Usage:
    python watch.py               # continuous polling loop
    python watch.py --single      # run one cycle and exit (for cron)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import signal
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, Error as PlaywrightError

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
DB_FILE = BASE_DIR / "seen_messages.db"
LOG_FILE = BASE_DIR / "watcher.log"
ENV_FILE = BASE_DIR / ".env"

# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("watcher")
logger.setLevel(logging.DEBUG)

# Rotating file handler: 5 MB x 3 backups
_fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logger.addHandler(_fh)

# Console handler
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logger.addHandler(_ch)

# ─── Config ───────────────────────────────────────────────────────────────────
load_dotenv(ENV_FILE)


def _env(key: str, required: bool = True, default: str = "") -> str:
    """Read an env var, raising immediately if required and missing."""
    val = os.getenv(key, "").strip()
    if not val:
        if required:
            logger.critical("Missing required env var: %s", key)
            sys.exit(1)
        return default
    return val


PORTAL_URL = _env("PORTAL_MESSAGES_URL")
TELEGRAM_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
SMTP_SERVER = _env("SMTP_SERVER", required=False, default="smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", required=False, default="587"))
SMTP_USERNAME = _env("SMTP_USERNAME")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
EMAIL_RECIPIENTS = [r.strip() for r in _env("EMAIL_RECIPIENTS").split(",") if r.strip()]
HEALTHCHECK_URL = _env("HEALTHCHECK_PING_URL", required=False)
POLL_INTERVAL = int(_env("POLL_INTERVAL_SECONDS", required=False, default="300"))

# ─── CSS Selectors ────────────────────────────────────────────────────────────
# These target the IITB placement portal (blog/placement/ page).
# The portal is behind SSO so we can't inspect it directly, but common
# Django/WordPress blog list patterns are covered here.
# ADJUST THESE once you inspect the actual HTML after logging in.
#
# SELECTOR_MESSAGE_ITEM: matches every message card/row on the page.
SELECTOR_MESSAGE_ITEM = "article, .post, .blog-post, .message-item, .notice-card, tr.message-row, .placement-item, li.list-group-item"

# Within each message item:
SELECTOR_TITLE = "h2 a, h3 a, .entry-title a, .post-title a, .message-title, a.title"
SELECTOR_DATE = "time, .date, .entry-date, .post-date, .message-date, .meta-date, span.text-muted"
SELECTOR_LINK = "a[href]"           # first <a> with href inside the item
SELECTOR_BODY_PREVIEW = "p, .entry-summary, .excerpt, .message-body, .post-content"

# Redirect / login-page detection: if the final URL contains any of these
# substrings after navigation, we treat it as a session-expiry redirect.
LOGIN_URL_MARKERS = ["/login", "/sso", "/cas/", "/auth", "signin", "saml", "/accounts/"]


# ─── Data ─────────────────────────────────────────────────────────────────────
@dataclass
class Message:
    """A single message extracted from the portal."""
    uid: str           # deterministic dedup key (hash of link + date)
    title: str
    date: str
    link: str          # absolute URL
    body_preview: str


def _make_uid(link: str, date: str) -> str:
    """Stable dedup key from link + date (not text, as requested)."""
    raw = f"{link.strip()}|{date.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ─── Database ─────────────────────────────────────────────────────────────────
def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            uid        TEXT PRIMARY KEY,
            title      TEXT,
            date       TEXT,
            link       TEXT,
            first_seen REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def _is_first_run(conn: sqlite3.Connection) -> bool:
    """True when the DB has never been seeded."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'seeded'"
    ).fetchone()
    return row is None


def _mark_seeded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')"
    )
    conn.commit()


def _is_seen(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_messages WHERE uid = ?", (uid,)
    ).fetchone() is not None


def _mark_seen(conn: sqlite3.Connection, msg: Message) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_messages (uid, title, date, link, first_seen) VALUES (?, ?, ?, ?, ?)",
        (msg.uid, msg.title, msg.date, msg.link, time.time()),
    )
    conn.commit()


def _seen_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM seen_messages").fetchone()
    return row[0] if row else 0


# ─── Page Extraction ─────────────────────────────────────────────────────────
def _extract_messages(page: Page) -> list[Message]:
    """
    Extract all messages from the currently loaded portal page.
    Returns a list of Message objects.
    """
    items = page.query_selector_all(SELECTOR_MESSAGE_ITEM)
    messages: list[Message] = []

    for item in items:
        # Title
        title_el = item.query_selector(SELECTOR_TITLE)
        title = (title_el.inner_text() if title_el else "").strip()

        # Date
        date_el = item.query_selector(SELECTOR_DATE)
        date = (date_el.inner_text() if date_el else "").strip()

        # Link (first anchor with href)
        link_el = item.query_selector(SELECTOR_LINK)
        raw_href = link_el.get_attribute("href") if link_el else ""

        # BUG FIX: use raw_href for the skip check, not the urljoin'd link.
        # Previously, `link` always had a value (fallback to page.url),
        # so broken items with no real href were never skipped.
        if not title and not raw_href:
            continue  # skip empty/broken items

        link = urljoin(page.url, raw_href) if raw_href else page.url

        # Body preview
        body_el = item.query_selector(SELECTOR_BODY_PREVIEW)
        body_preview = (body_el.inner_text() if body_el else "").strip()[:200]

        uid = _make_uid(link, date)
        messages.append(Message(uid=uid, title=title, date=date, link=link, body_preview=body_preview))

    return messages


def _detect_session_expiry(page: Page) -> bool:
    """Check if we were redirected to a login/SSO page or public landing page."""
    current_url = page.url.lower().rstrip("/")
    if any(marker in current_url for marker in LOGIN_URL_MARKERS):
        return True
    if current_url in ("https://campus.placements.iitb.ac.in", "http://campus.placements.iitb.ac.in"):
        return True
    if "/blog" in PORTAL_URL.lower() and "/blog" not in current_url:
        return True
    return False


# ─── Notifications ────────────────────────────────────────────────────────────
def _send_telegram(text: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.ok:
            logger.info("Telegram sent OK")
            return True
        logger.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:300])
        return False
    except Exception:
        logger.exception("Telegram send failed")
        return False


def _send_email(subject: str, body_html: str) -> bool:
    """Send an email to all recipients via Gmail SMTP. Returns True on success."""
    if not EMAIL_RECIPIENTS:
        logger.warning("No email recipients configured, skipping email.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USERNAME
        msg["To"] = ", ".join(EMAIL_RECIPIENTS)
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, EMAIL_RECIPIENTS, msg.as_string())

        logger.info("Email sent OK -> %s", EMAIL_RECIPIENTS)
        return True
    except Exception:
        logger.exception("Email send failed")
        return False


def _html_escape(s: str) -> str:
    """Escape text for safe use in HTML content."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _notify_new_message(msg: Message) -> bool:
    """
    Send notification for a new message via both channels.
    Returns True if at least one channel succeeded.
    """
    # Telegram message (HTML mode)
    tg_text = (
        f"\U0001f4e2 <b>New Placement Notice</b>\n\n"
        f"<b>{_html_escape(msg.title)}</b>\n"
        f"\U0001f4c5 {_html_escape(msg.date)}\n"
        f"\U0001f517 <a href=\"{_html_escape(msg.link)}\">Open Message</a>"
    )

    # Email
    email_subject = f"[Placement] {msg.title}"
    email_body = (
        f"<h3>New Placement Notice</h3>"
        f"<p><strong>{_html_escape(msg.title)}</strong></p>"
        f"<p>Date: {_html_escape(msg.date)}</p>"
        f"<p><a href=\"{_html_escape(msg.link)}\">Open Message</a></p>"
    )

    tg_ok = _send_telegram(tg_text)
    email_ok = _send_email(email_subject, email_body)

    if not tg_ok:
        logger.warning("Telegram failed for: %s", msg.title)
    if not email_ok:
        logger.warning("Email failed for: %s", msg.title)

    return tg_ok or email_ok


def _notify_alert(alert_text: str) -> None:
    """
    Send an operational alert (session expiry, structure change, etc.).
    BUG FIX: alert_text is treated as plain text and escaped properly.
    Previously, raw HTML like <code> was passed into _html_escape which
    destroyed the formatting.
    """
    # For Telegram: use plain text, no HTML formatting in the alert body
    tg_text = f"\u26a0\ufe0f Watcher Alert\n\n{alert_text}"
    _send_telegram(tg_text)

    # For Email: escape the alert text properly
    email_body = f"<h3>Watcher Alert</h3><p>{_html_escape(alert_text)}</p>"
    _send_email("[Watcher Alert]", email_body)


# ─── Healthcheck ──────────────────────────────────────────────────────────────
def _ping_healthcheck() -> None:
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
        logger.debug("Healthcheck pinged OK")
    except Exception:
        logger.exception("Healthcheck ping failed")


def _ping_healthcheck_fail() -> None:
    """Signal a failed cycle to healthchecks.io."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(f"{HEALTHCHECK_URL}/fail", timeout=10)
    except Exception:
        pass


# ─── Main Loop ────────────────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down after current cycle.", signum)
    _shutdown = True


def run_cycle(context: BrowserContext, conn: sqlite3.Connection) -> None:
    """Run one poll-extract-notify cycle."""
    page = context.new_page()
    try:
        logger.info("Navigating to %s", PORTAL_URL)
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
        # Give JS a moment to render dynamic content
        page.wait_for_timeout(3000)

        # ── Session-expiry check ──────────────────────────────────────
        if _detect_session_expiry(page):
            logger.error("Session expired! Current URL: %s", page.url)
            _notify_alert(
                "Session expired -- the watcher was redirected to the login page.\n"
                "Re-run login_once.py on your laptop, then copy state.json to the VM."
            )
            _ping_healthcheck_fail()
            return

        # ── Extract messages ──────────────────────────────────────────
        messages = _extract_messages(page)
        logger.info("Extracted %d message(s) from page.", len(messages))

        # ── Structure-change detection ────────────────────────────────
        prev_count = _seen_count(conn)
        if len(messages) == 0 and prev_count > 0:
            logger.error(
                "Page structure may have changed: 0 messages found but DB has %d.", prev_count
            )
            _notify_alert(
                f"Page structure change detected: found 0 messages on the page, "
                f"but the database has {prev_count} previously seen messages.\n"
                "The portal HTML may have changed. Please check the CSS selectors."
            )
            _ping_healthcheck_fail()
            return

        # ── First-run seeding ─────────────────────────────────────────
        first_run = _is_first_run(conn)
        if first_run:
            logger.info(
                "First run detected: seeding %d existing messages (no notifications).",
                len(messages),
            )
            for msg in messages:
                _mark_seen(conn, msg)
            _mark_seeded(conn)
            _ping_healthcheck()
            return

        # ── Process new messages ──────────────────────────────────────
        new_messages = [m for m in messages if not _is_seen(conn, m.uid)]

        if not new_messages:
            logger.info("No new messages.")
            _ping_healthcheck()
            return

        logger.info("Found %d NEW message(s)!", len(new_messages))

        for msg in new_messages:
            logger.info("  New: %s | %s | %s", msg.date, msg.title, msg.link)
            success = _notify_new_message(msg)
            if success:
                # Only mark seen AFTER at least one channel succeeded
                _mark_seen(conn, msg)
                logger.info("  Marked as seen: %s", msg.uid[:12])
            else:
                # Both channels failed — leave unseen for retry next cycle
                logger.error(
                    "  BOTH channels failed for '%s' -- will retry next cycle.", msg.title
                )

        _ping_healthcheck()

    except PlaywrightError as e:
        logger.exception("Playwright error during cycle")
        err_str = str(e).lower()
        if "net::" in err_str or "timeout" in err_str:
            logger.warning("Network issue, will retry next cycle.")
        _ping_healthcheck_fail()
    except Exception:
        logger.exception("Unexpected error during cycle")
        _ping_healthcheck_fail()
    finally:
        page.close()


def main() -> None:
    """Entry point: set up browser, DB, and run the polling loop."""
    parser = argparse.ArgumentParser(description="Placement Portal Watcher")
    parser.add_argument(
        "--single", action="store_true",
        help="Run a single poll cycle and exit (for use with cron).",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not STATE_FILE.exists():
        logger.critical("state.json not found! Run login_once.py first.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  Placement Portal Watcher starting")
    logger.info("  Portal: %s", PORTAL_URL)
    if args.single:
        logger.info("  Mode: single cycle (cron)")
    else:
        logger.info("  Mode: continuous polling, interval %ds (+ jitter)", POLL_INTERVAL)
    logger.info("=" * 60)

    conn = sqlite3.connect(str(DB_FILE))
    _init_db(conn)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        if args.single:
            # Single cycle mode for cron
            context = browser.new_context(storage_state=str(STATE_FILE))
            try:
                run_cycle(context, conn)
            except Exception:
                logger.exception("Cycle-level failure")
                _ping_healthcheck_fail()
            finally:
                context.close()
        else:
            # Continuous polling loop
            while not _shutdown:
                # Load session fresh each cycle (in case state.json is updated)
                context = browser.new_context(storage_state=str(STATE_FILE))
                try:
                    run_cycle(context, conn)
                except Exception:
                    logger.exception("Cycle-level failure")
                    _ping_healthcheck_fail()
                finally:
                    context.close()

                if _shutdown:
                    break

                # Sleep with random jitter (+/-15% of interval)
                jitter = random.uniform(-0.15, 0.15) * POLL_INTERVAL
                sleep_time = max(60, POLL_INTERVAL + jitter)  # minimum 60s
                logger.info("Sleeping %.0fs until next cycle...", sleep_time)
                time.sleep(sleep_time)

        browser.close()
        conn.close()
        logger.info("Watcher stopped cleanly.")


if __name__ == "__main__":
    main()
