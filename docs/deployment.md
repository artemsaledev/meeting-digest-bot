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

TELEGRAM_BOT_TOKEN=...
MEETING_DIGEST_SHARED_SECRET=...
```

## Systemd

```bash
sudo cp deploy/linux/systemd/meeting-digest-bot-api.service.example /etc/systemd/system/meeting-digest-bot-api.service
sudo cp deploy/linux/systemd/meeting-digest-bot-poller.service.example /etc/systemd/system/meeting-digest-bot-poller.service
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-digest-bot-api.service
sudo systemctl enable --now meeting-digest-bot-poller.service
```

## Verify

```bash
curl http://127.0.0.1:8011/health
systemctl status meeting-digest-bot-api.service
systemctl status meeting-digest-bot-poller.service
journalctl -u meeting-digest-bot-api.service -n 100 --no-pager
journalctl -u meeting-digest-bot-poller.service -n 100 --no-pager
```

