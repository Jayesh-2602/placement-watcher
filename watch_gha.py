#!/usr/bin/env python3
"""
watch_gha.py — Placement Portal Watcher (GitHub Actions version)

This is a single-cycle version of the watcher optimized for running as a
GitHub Actions scheduled workflow. It:
  1. Decodes state.json from the STATE_JSON_B64 env var (GitHub secret)
  2. Runs one poll-extract-notify cycle
  3. Exits (GitHub Actions handles the schedule)

All other logic (extraction, dedup, notifications) is identical to watch.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import smtplib
import sqlite3
import sys
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, Page, Error as PlaywrightError

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
DB_FILE = BASE_DIR / "seen_messages.db"

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("watcher")

# ─── Config ───────────────────────────────────────────────────────────────────


def _env(key: str, required: bool = True, default: str = "") -> str:
    """Read an env var."""
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
SMTP_USERNAME = _env("SMTP_USERNAME", required=False)
SMTP_PASSWORD = _env("SMTP_PASSWORD", required=False)
EMAIL_RECIPIENTS = [r.strip() for r in _env("EMAIL_RECIPIENTS", required=False).split(",") if r.strip()]
HEALTHCHECK_URL = _env("HEALTHCHECK_PING_URL", required=False)

# ─── CSS Selectors ────────────────────────────────────────────────────────────
# ADJUST THESE to match your portal's actual HTML structure.
SELECTOR_MESSAGE_ITEM = "article, .post, .blog-post, .message-item, .notice-card, tr.message-row, .placement-item, li.list-group-item"
SELECTOR_TITLE = "h2 a, h3 a, .entry-title a, .post-title a, .message-title, a.title"
SELECTOR_DATE = "time, .date, .entry-date, .post-date, .message-date, .meta-date, span.text-muted"
SELECTOR_LINK = "a[href]"
SELECTOR_BODY_PREVIEW = "p, .entry-summary, .excerpt, .message-body, .post-content"

LOGIN_URL_MARKERS = ["/login", "/sso", "/cas/", "/auth", "signin", "saml", "/accounts/"]


# ─── Data ─────────────────────────────────────────────────────────────────────
@dataclass
class Message:
    uid: str
    title: str
    date: str
    link: str
    body_preview: str


def _make_uid(link: str, date: str) -> str:
    raw = f"{link.strip()}|{date.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ─── Database ─────────────────────────────────────────────────────────────────
def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            uid TEXT PRIMARY KEY, title TEXT, date TEXT, link TEXT, first_seen REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
    """)
    conn.commit()


def _is_first_run(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone() is None


def _mark_seeded(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
    conn.commit()


def _is_seen(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_messages WHERE uid = ?", (uid,)).fetchone() is not None


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
    items = page.query_selector_all(SELECTOR_MESSAGE_ITEM)
    messages: list[Message] = []

    for item in items:
        title_el = item.query_selector(SELECTOR_TITLE)
        title = (title_el.inner_text() if title_el else "").strip()

        date_el = item.query_selector(SELECTOR_DATE)
        date = (date_el.inner_text() if date_el else "").strip()

        link_el = item.query_selector(SELECTOR_LINK)
        raw_href = link_el.get_attribute("href") if link_el else ""

        if not title and not raw_href:
            continue

        link = urljoin(page.url, raw_href) if raw_href else page.url

        body_el = item.query_selector(SELECTOR_BODY_PREVIEW)
        body_preview = (body_el.inner_text() if body_el else "").strip()[:200]

        uid = _make_uid(link, date)
        messages.append(Message(uid=uid, title=title, date=date, link=link, body_preview=body_preview))

    return messages


def _detect_session_expiry(page: Page) -> bool:
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
    if not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_RECIPIENTS:
        logger.info("Email not configured, skipping.")
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
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _notify_new_message(msg: Message) -> bool:
    tg_text = (
        f"\U0001f4e2 <b>New Placement Notice</b>\n\n"
        f"<b>{_html_escape(msg.title)}</b>\n"
        f"\U0001f4c5 {_html_escape(msg.date)}\n"
        f"\U0001f517 <a href=\"{_html_escape(msg.link)}\">Open Message</a>"
    )
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
    tg_text = f"\u26a0\ufe0f Watcher Alert\n\n{alert_text}"
    _send_telegram(tg_text)
    _send_email("[Watcher Alert]", f"<h3>Watcher Alert</h3><p>{_html_escape(alert_text)}</p>")


def _ping_healthcheck() -> None:
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
        logger.debug("Healthcheck pinged OK")
    except Exception:
        logger.exception("Healthcheck ping failed")


def _ping_healthcheck_fail() -> None:
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(f"{HEALTHCHECK_URL}/fail", timeout=10)
    except Exception:
        pass


# ─── Session Handling ─────────────────────────────────────────────────────────
def _restore_session() -> None:
    """Decode state.json from the STATE_JSON_B64 env var (GitHub secret)."""
    b64 = os.getenv("STATE_JSON_B64", "").strip()
    if b64:
        logger.info("Restoring state.json from STATE_JSON_B64 secret...")
        data = base64.b64decode(b64)
        STATE_FILE.write_bytes(data)
        logger.info("state.json restored (%d bytes).", len(data))
    elif not STATE_FILE.exists():
        logger.critical(
            "No state.json found and STATE_JSON_B64 is empty. "
            "Run login_once.py first, then add the base64-encoded state.json as a GitHub secret."
        )
        sys.exit(1)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    _restore_session()

    conn = sqlite3.connect(str(DB_FILE))
    _init_db(conn)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE_FILE))

        page = context.new_page()
        try:
            logger.info("Navigating to %s", PORTAL_URL)
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)

            # Session expiry
            if _detect_session_expiry(page):
                logger.error("Session expired! URL: %s", page.url)
                _notify_alert(
                    "Session expired -- the watcher was redirected to the login page.\n"
                    "Re-run login_once.py on your laptop, then update the STATE_JSON_B64 GitHub secret."
                )
                _ping_healthcheck_fail()
                return

            # Extract
            messages = _extract_messages(page)
            logger.info("Extracted %d message(s).", len(messages))

            # Structure change
            prev_count = _seen_count(conn)
            if len(messages) == 0 and prev_count > 0:
                logger.error("Page structure change: 0 found, DB has %d.", prev_count)
                _notify_alert(
                    f"Page structure change: found 0 messages but DB has {prev_count}.\n"
                    "Check CSS selectors in watch_gha.py."
                )
                _ping_healthcheck_fail()
                return

            # First run seed
            if _is_first_run(conn):
                logger.info("First run: seeding %d messages (no notifications).", len(messages))
                for msg in messages:
                    _mark_seen(conn, msg)
                _mark_seeded(conn)
                _ping_healthcheck()
                return

            # New messages
            new_messages = [m for m in messages if not _is_seen(conn, m.uid)]
            if not new_messages:
                logger.info("No new messages.")
                _ping_healthcheck()
                return

            logger.info("Found %d NEW message(s)!", len(new_messages))
            for msg in new_messages:
                logger.info("  New: %s | %s | %s", msg.date, msg.title, msg.link)
                if _notify_new_message(msg):
                    _mark_seen(conn, msg)
                    logger.info("  Marked seen: %s", msg.uid[:12])
                else:
                    logger.error("  BOTH channels failed for '%s', will retry next run.", msg.title)

            _ping_healthcheck()

        except PlaywrightError as e:
            logger.exception("Playwright error")
            _ping_healthcheck_fail()
        except Exception:
            logger.exception("Unexpected error")
            _ping_healthcheck_fail()
        finally:
            page.close()
            context.close()
            browser.close()
            conn.close()

    logger.info("Cycle complete.")


if __name__ == "__main__":
    main()
