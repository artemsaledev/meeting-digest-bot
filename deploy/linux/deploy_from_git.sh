#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/meeting-digest-bot}"
REPO_URL="${REPO_URL:-https://github.com/artemsaledev/meeting-digest-bot.git}"
BRANCH="${BRANCH:-main}"

if [ ! -d "$APP_DIR/.git" ]; then
  tmp_dir="$(mktemp -d)"
  git clone --branch "$BRANCH" "$REPO_URL" "$tmp_dir"
  mkdir -p "$APP_DIR"
  rsync -a --delete \
    --exclude='.env' \
    --exclude='knowledge-pipeline.env' \
    --exclude='data/' \
    --exclude='.venv/' \
    "$tmp_dir"/ "$APP_DIR"/
  rm -rf "$tmp_dir"
else
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$APP_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q meeting_digest_bot

systemctl restart meeting-digest-bot-api.service meeting-digest-bot-poller.service
systemctl is-active meeting-digest-bot-api.service
systemctl is-active meeting-digest-bot-poller.service
curl -fsS http://127.0.0.1:8011/health
