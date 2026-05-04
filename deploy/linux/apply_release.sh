#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/meeting-digest-bot}"
ZIP_PATH="${ZIP_PATH:-/tmp/meeting-digest-bot-release.zip}"
RELEASE_DIR="${RELEASE_DIR:-/tmp/meeting-digest-bot-release}"

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
unzip -oq "$ZIP_PATH" -d "$RELEASE_DIR"
mkdir -p "$APP_DIR"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='.env' \
    --exclude='data/' \
    --exclude='.venv/' \
    --exclude='.deploy-backup/' \
    "$RELEASE_DIR"/ "$APP_DIR"/
else
  find "$APP_DIR" -mindepth 1 -maxdepth 1 \
    ! -name '.env' \
    ! -name 'data' \
    ! -name '.venv' \
    ! -name '.deploy-backup' \
    -exec rm -rf {} +
  cp -a "$RELEASE_DIR"/. "$APP_DIR"/
fi

cd "$APP_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m compileall -q meeting_digest_bot

systemctl restart meeting-digest-bot-api.service meeting-digest-bot-poller.service
sleep 2
systemctl is-active meeting-digest-bot-api.service
systemctl is-active meeting-digest-bot-poller.service
curl -fsS http://127.0.0.1:8011/health
