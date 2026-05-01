#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/meeting-digest-bot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "App directory does not exist: $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"

SUDO="sudo"
if [[ "$(id -u)" == "0" ]]; then
  SUDO=""
fi

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update
  $SUDO apt-get install -y python3 python3-venv python3-pip curl unzip
fi

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

mkdir -p data logs

if [[ ! -f .env && -f deploy/linux/meeting-digest-bot.env.example ]]; then
  cp deploy/linux/meeting-digest-bot.env.example .env
  chmod 600 .env
  echo "Created .env from example. Fill production values before starting services."
fi

echo "Runtime installed in $APP_DIR"
echo "Next: edit $APP_DIR/.env and install systemd units from deploy/linux/systemd."
