# MeetingDigestBot Linux Deployment

`MeetingDigestBot` runs as a separate service next to `AIcallorder`.

Recommended VPS layout:

- `/opt/AIcallorder` - existing Loom processing app.
- `/opt/meeting-digest-bot` - MeetingDigestBot service.
- `meeting-digest-bot-api.service` - FastAPI HTTP API for AIcallorder publication registration.
- `meeting-digest-bot-poller.service` - Telegram polling worker for bot commands.

The service can stay private on `127.0.0.1`; Telegram commands are handled through polling, so a public HTTPS endpoint is not required for MVP.

## 1. Build Release On Windows

From the local project:

```powershell
cd "C:\Users\artem\Downloads\dev-scripts\6. Task Manager"
powershell -ExecutionPolicy Bypass -File .\deploy\windows\package_for_vps.ps1
```

The archive is created at:

```text
C:\Users\artem\Downloads\dev-scripts\6. Task Manager\data\runtime\meeting-digest-bot-release.zip
```

The archive intentionally excludes `.env`, SQLite state DB files, caches, and local runtime data.

## 2. Transfer To VPS

Example:

```bash
scp meeting-digest-bot-release.zip USER@SERVER:/tmp/
ssh USER@SERVER
sudo mkdir -p /opt/meeting-digest-bot
sudo unzip -o /tmp/meeting-digest-bot-release.zip -d /opt/meeting-digest-bot
sudo chown -R USER:USER /opt/meeting-digest-bot
```

Replace `USER` and `SERVER` with the real VPS SSH user and host.

## 3. Install Runtime

```bash
cd /opt/meeting-digest-bot
chmod +x deploy/linux/install_runtime_ubuntu.sh deploy/linux/verify.sh
APP_DIR=/opt/meeting-digest-bot ./deploy/linux/install_runtime_ubuntu.sh
```

## 4. Configure MeetingDigestBot

Create or edit `/opt/meeting-digest-bot/.env` using:

- `deploy/linux/meeting-digest-bot.env.example`

Minimum production values:

```env
MEETING_DIGEST_BOT_HOST=127.0.0.1
MEETING_DIGEST_BOT_PORT=8011
AICALLORDER_DB_PATH=/opt/AIcallorder/data/loom_automation.db
MEETING_DIGEST_STATE_DB_PATH=/opt/meeting-digest-bot/data/meeting_digest_bot.db

BITRIX_WEBHOOK_BASE=https://totiscrm.com/rest/USER/TOKEN/
BITRIX_GROUP_ID=512
BITRIX_ACTOR_USER_ID=114736
BITRIX_DEFAULT_RESPONSIBLE_ID=114736
BITRIX_CREATED_BY_ID=114736
BITRIX_DEFAULT_AUDITOR_IDS=50760,127124,137230,51977

TELEGRAM_BOT_TOKEN=...
MEETING_DIGEST_SHARED_SECRET=...
```

Optional weekly LLM enhancement:

```env
MEETING_DIGEST_WEEKLY_LLM_ENABLED=true
LLM_API_KEY=...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
```

## 5. Configure AIcallorder

In `/opt/AIcallorder/.env` add values from:

- `deploy/linux/aicallorder.env.additions.example`

Required additions:

```env
MEETING_DIGEST_BOT_BASE_URL=http://127.0.0.1:8011
MEETING_DIGEST_BOT_TIMEOUT_SECONDS=15
MEETING_DIGEST_SHARED_SECRET=same-secret-as-meeting-digest-bot
```

Restart `AIcallorder` after changing env.

## 6. Install Systemd Services

```bash
sudo cp deploy/linux/systemd/meeting-digest-bot-api.service.example /etc/systemd/system/meeting-digest-bot-api.service
sudo cp deploy/linux/systemd/meeting-digest-bot-poller.service.example /etc/systemd/system/meeting-digest-bot-poller.service
sudo nano /etc/systemd/system/meeting-digest-bot-api.service
sudo nano /etc/systemd/system/meeting-digest-bot-poller.service
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-digest-bot-api.service
sudo systemctl enable --now meeting-digest-bot-poller.service
```

In both unit files replace:

- `YOUR_LINUX_USER` with the real Linux user.
- Paths if the app is not installed in `/opt/meeting-digest-bot`.

## 7. Verify

```bash
cd /opt/meeting-digest-bot
./deploy/linux/verify.sh
```

Manual checks:

```bash
curl http://127.0.0.1:8011/health
systemctl status meeting-digest-bot-api.service
systemctl status meeting-digest-bot-poller.service
journalctl -u meeting-digest-bot-api.service -n 100 --no-pager
journalctl -u meeting-digest-bot-poller.service -n 100 --no-pager
```

## 8. Bot Commands

The bot supports English and Russian action words. English examples are safer for console copy/paste:

```text
https://t.me/c/5147878786/120
https://t.me/c/5147878786/120 create
https://t.me/c/5147878786/120 comment 168334
https://t.me/c/5147878786/120 checklist 168334
https://t.me/c/5147878786/120 update 168334
/day 2026-04-14 week 168336
/week 2026-04-13 2026-04-19
```

Russian aliases also work:

```text
создать
коммент
чеклист
обновить
неделя
```

Default mode for a post link is preview. If the meeting is already bound to a CRM task, `auto` appends a comment to the existing task; otherwise it creates a new task.
