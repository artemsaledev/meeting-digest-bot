# Deployment

The production deployment runs on the VPS as two systemd services:

```text
meeting-digest-bot-api.service
meeting-digest-bot-poller.service
```

Recommended paths:

```text
/opt/meeting-digest-bot
/opt/AIcallorder
```

## Build Release Archive

On Windows:

```powershell
cd "meeting-digest-bot"
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\package_for_vps.ps1
```

Archive output:

```text
data\runtime\meeting-digest-bot-release.zip
```

The archive excludes secrets, SQLite DB files, caches, logs, and local runtime data.

## Install On VPS

```bash
sudo mkdir -p /opt/meeting-digest-bot
sudo unzip -o /tmp/meeting-digest-bot-release.zip -d /opt/meeting-digest-bot
cd /opt/meeting-digest-bot
chmod +x deploy/linux/install_runtime_ubuntu.sh deploy/linux/verify.sh
APP_DIR=/opt/meeting-digest-bot ./deploy/linux/install_runtime_ubuntu.sh
```

For subsequent releases, upload the archive to `/tmp/meeting-digest-bot-release.zip`
and apply it without deleting runtime files:

```bash
cd /opt/meeting-digest-bot
APP_DIR=/opt/meeting-digest-bot ZIP_PATH=/tmp/meeting-digest-bot-release.zip bash deploy/linux/apply_release.sh
```

The release applier keeps `.env`, `data/`, `.venv/`, and `.deploy-backup/`.

## Environment

Create `/opt/meeting-digest-bot/.env` from:

```text
deploy/linux/meeting-digest-bot.env.example
```

Required production values:

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
BITRIX_DAILY_PLAN_ACCOMPLICE_IDS=51977,58194,127124,114736,137230,50760,123170,120601,426,162783,163323

TELEGRAM_BOT_TOKEN=...
TELEGRAM_REPORT_CHAT_ID=
MEETING_DIGEST_SHARED_SECRET=...
```

`TELEGRAM_REPORT_CHAT_ID` is optional. If it is empty, the service uses the latest Telegram chat ID registered from `AIcallorder` publications.

## Systemd

```bash
sudo cp deploy/linux/systemd/meeting-digest-bot-api.service.example /etc/systemd/system/meeting-digest-bot-api.service
sudo cp deploy/linux/systemd/meeting-digest-bot-poller.service.example /etc/systemd/system/meeting-digest-bot-poller.service
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-digest-bot-api.service
sudo systemctl enable --now meeting-digest-bot-poller.service
```

## Cron Reports

Daily and weekly checklist reports are scheduled through cron:

```bash
sudo cp deploy/linux/cron.d/meeting-digest-bot-reports /etc/cron.d/meeting-digest-bot-reports
sudo chmod 0644 /etc/cron.d/meeting-digest-bot-reports
sudo systemctl restart cron || sudo systemctl restart crond
```

Schedule:

```text
Hourly cron gate: sends daily report when current Europe/Kyiv hour is 09
Hourly cron gate: sends weekly report when current Europe/Kyiv weekday/hour is Friday 16
```

The daily report adds a comment to the day plan task and posts a Telegram report with responsible usernames for open checklist items. The weekly report tags all responsible users with open items at once.

The VPS system timezone may remain UTC. The cron file gates execution with `TZ=Europe/Kyiv date ...`, because Debian/Ubuntu cron may ignore `CRON_TZ` inside `/etc/cron.d` files.

Manual report commands on the VPS:

```bash
cd /opt/meeting-digest-bot
.venv/bin/python -m meeting_digest_bot daily-report --report-date 2026-05-04 --no-telegram --force
.venv/bin/python -m meeting_digest_bot weekly-report --week-from 2026-05-04 --week-to 2026-05-08 --no-telegram --force
```

`--no-telegram` suppresses Telegram sending. For weekly reports, a run without Telegram and without an existing weekly CRM task is safe for dry checks: it prepares output but does not mark the weekly report as published.

## Verify

```bash
curl http://127.0.0.1:8011/health
systemctl status meeting-digest-bot-api.service
systemctl status meeting-digest-bot-poller.service
journalctl -u meeting-digest-bot-api.service -n 100 --no-pager
journalctl -u meeting-digest-bot-poller.service -n 100 --no-pager
```

