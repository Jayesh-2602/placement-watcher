# Placement Portal Watcher

Polls your college placement portal for new messages and instantly notifies you + friends via **Telegram** and **Email**.

> **Zero passwords stored.** The watcher uses a saved browser session (`state.json`) — you log in once in a visible browser and the script reuses those cookies. No SSO credentials ever touch the code or config files.

---

## Architecture

```
┌─────────────┐    manual login     ┌──────────────┐
│  Your Laptop │ ──────────────────→ │  state.json  │
│  (login_once)│                     │  (cookies)   │
└─────────────┘                     └──────┬───────┘
                                           │ scp
                                           ▼
                                    ┌──────────────┐
                                    │  Oracle VM   │
                                    │  (watch.py)  │
                                    │              │
                                    │  every 5 min:│
                                    │  ┌─────────┐ │
                                    │  │ Playwright│─── loads portal ──→ extract messages
                                    │  └─────────┘ │
                                    │       │      │       │
                                    │  ┌────▼────┐ │  ┌────▼────┐
                                    │  │ SQLite  │ │  │ Notify  │
                                    │  │ (dedup) │ │  │ TG+Email│
                                    │  └─────────┘ │  └─────────┘
                                    │       │      │
                                    │  ┌────▼────┐ │
                                    │  │Healthchk│ │
                                    │  │ping     │ │
                                    │  └─────────┘ │
                                    └──────────────┘
```

---

## Prerequisites

- **Python 3.10+**
- A **free Oracle Cloud** Ubuntu VM (or any always-on Linux box)
- A **Telegram** account
- A **Gmail** account with 2FA enabled
- A **Healthchecks.io** free account

---

## Step-by-Step Setup

### 1. Create a Telegram Bot & Group

1. Open Telegram, search **@BotFather**, send `/newbot`
2. Choose a name and username, copy the **bot token** (e.g., `123456:ABC-DEF`)
3. Create a group with your friends, add the bot to the group
4. Send any message in the group
5. Get the **chat ID**:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Look for `"chat": {"id": -100XXXXXXXXXX}` — that negative number is your chat ID.

### 2. Create a Gmail App Password

1. Go to [Google Account - Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Create an app password for "Mail" / "Other (Placement Watcher)"
5. Copy the 16-character password (looks like `abcd efgh ijkl mnop`)

### 3. Create a Healthchecks.io Check

1. Sign up at [healthchecks.io](https://healthchecks.io) (free tier: 20 checks)
2. Create a new check:
   - **Name**: Placement Watcher
   - **Period**: 10 minutes (slightly > polling interval)
   - **Grace**: 5 minutes
3. Copy the **ping URL** (e.g., `https://hc-ping.com/xxxxxxxx-...`)
4. Set up an alert channel (Telegram, email, etc.) so healthchecks.io notifies you if the watcher stops.

### 4. Set Up the VM

```bash
# SSH into your Oracle Cloud VM
ssh ubuntu@your-vm-ip

# Install system deps
sudo apt update && sudo apt install -y python3-pip python3-venv

# Create project directory
mkdir -p ~/watcher && cd ~/watcher

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python deps
pip install playwright python-dotenv requests
playwright install --with-deps chromium
```

### 5. Copy Files to the VM

From your **laptop**, upload the project files:

```bash
scp login_once.py watch.py requirements.txt .env.example watcher.service \
    ubuntu@your-vm-ip:~/watcher/
```

### 6. Configure Environment

```bash
ssh ubuntu@your-vm-ip
cd ~/watcher

# Create .env from the template
cp .env.example .env
nano .env   # fill in ALL values
```

### 7. Log In and Copy Session

This is done on your **laptop** (needs a GUI browser):

```bash
# On your laptop
cd /path/to/watcher
pip install playwright
playwright install chromium

python login_once.py
# A browser opens. Log in with your college SSO.
# Navigate to the messages page.
# Come back to terminal. Press Enter.
# state.json is saved.
```

Copy the session to the VM:

```bash
scp state.json ubuntu@your-vm-ip:~/watcher/
```

### 8. Test Run

```bash
ssh ubuntu@your-vm-ip
cd ~/watcher
source venv/bin/activate

# Run once to seed existing messages (no notifications sent)
python watch.py
# Press Ctrl+C after you see "Sleeping..."
```

Check the logs:
```bash
cat watcher.log
```

### 9. Install as a systemd Service

```bash
# Copy the unit file
sudo cp watcher.service /etc/systemd/system/

# Adjust paths in the unit file if needed
sudo nano /etc/systemd/system/watcher.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable watcher.service
sudo systemctl start watcher.service

# Check status
sudo systemctl status watcher.service
sudo journalctl -u watcher.service -f
```

### 10. Cron Fallback (Alternative)

If you prefer cron over systemd (or as a belt-and-suspenders fallback):

```bash
crontab -e
```

Add:
```cron
# Run watcher every 6 minutes (single cycle mode).
# This is a FALLBACK — the systemd service is the primary runner.
# Uncomment only if not using systemd.
# */6 * * * * cd /home/ubuntu/watcher && /home/ubuntu/watcher/venv/bin/python watch.py --single-cycle >> /home/ubuntu/watcher/cron.log 2>&1
```

> **Note:** The cron approach runs one cycle per invocation. If you use this instead of systemd, add `--single-cycle` support to `watch.py` (break after one iteration of the loop).

---

## Re-Login When Session Expires

You will get an alert (Telegram + Email) saying **"Session expired, re-login needed"**.

1. On your **laptop**, run `login_once.py` again
2. Log in via the browser
3. Press Enter to save `state.json`
4. Copy it to the VM:
   ```bash
   scp state.json ubuntu@your-vm-ip:~/watcher/
   ```
5. The watcher picks up the new session automatically on the next cycle — **no restart needed**.

---

## Customising Selectors

The CSS selectors in `watch.py` are set to common patterns:

```python
SELECTOR_MESSAGE_ITEM = ".message-item, .notice-card, tr.message-row"
SELECTOR_TITLE        = ".message-title, .notice-title, td.subject a"
SELECTOR_DATE         = ".message-date, .notice-date, td.date"
SELECTOR_LINK         = "a[href]"
SELECTOR_BODY_PREVIEW = ".message-body, .notice-body, td.preview"
```

To find the right selectors for **your** portal:

1. Open the messages page in Chrome
2. Right-click a message then click **Inspect**
3. Identify the repeating container element (the card/row)
4. Note the CSS classes or attributes and update `SELECTOR_MESSAGE_ITEM`
5. Inside that container, find the title, date, link, and body elements
6. Update the other selectors accordingly

**Tip:** Use the browser console to test:
```js
document.querySelectorAll(".your-selector").length
```

---

## File Structure

```
watcher/
├── .env                 ← your secrets (never commit!)
├── .env.example         ← template
├── login_once.py        ← run on laptop to save session
├── watch.py             ← the main watcher daemon
├── test_extraction.py   ← test suite
├── requirements.txt     ← pip dependencies
├── watcher.service      ← systemd unit file
├── state.json           ← saved browser session (created by login_once.py)
├── seen_messages.db     ← SQLite dedup database (created by watch.py)
└── watcher.log          ← rotating log file (created by watch.py)
```

---

## Reliability Guarantees

| Feature | Implementation |
|---|---|
| **Never miss a message** | Messages only marked "seen" after at least 1 notification succeeds |
| **Never send duplicates** | SHA-256 dedup by link + date in SQLite |
| **First run does not spam** | Existing messages seeded silently on first cycle |
| **Session expiry** | URL-based detection + immediate alert |
| **Page structure change** | Zero-items-when-expected detection + alert |
| **Dual-channel failover** | Telegram + Email independent; failure in one does not block other |
| **Script death detection** | Healthchecks.io heartbeat + systemd auto-restart |
| **Gentle polling** | 5 min interval + random jitter of plus or minus 15% |
| **Persistent state** | SQLite DB survives restarts; session reloaded each cycle |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `state.json not found` | Run `login_once.py` on your laptop and `scp` it over |
| `Session expired` alert | Re-login (see above) |
| `Page structure change` alert | Portal HTML changed — update CSS selectors in `watch.py` |
| No notifications received | Check `.env` values, Telegram bot is in the group, Gmail app password is correct |
| Healthchecks.io says "down" | Check `sudo systemctl status watcher.service` and `watcher.log` |
| `playwright install` fails | Run `playwright install --with-deps chromium` (needs sudo for system deps) |

---

## Cost

**$0/month.** Everything used is free-tier:

- Oracle Cloud free VM (ARM, always-free)
- Telegram Bot API (free, unlimited)
- Gmail SMTP (free, 500 emails/day)
- Healthchecks.io (free, 20 checks)
- Playwright / Python / SQLite (open source)
