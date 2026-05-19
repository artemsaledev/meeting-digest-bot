#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/meeting-digest-bot}"
AGENT_USER="${NOTEBOOKLM_AGENT_USER:-notebooklm-agent}"
AGENT_GROUP="${NOTEBOOKLM_AGENT_GROUP:-meetingdigest}"
VNC_PORT="${NOTEBOOKLM_AGENT_VNC_PORT:-5905}"
DISPLAY_ID="${NOTEBOOKLM_AGENT_DISPLAY:-:98}"

if [ "$(id -u)" != "0" ]; then
  echo "Run as root." >&2
  exit 1
fi

if ! getent group "$AGENT_GROUP" >/dev/null; then
  groupadd --system "$AGENT_GROUP"
fi

if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system \
    --create-home \
    --home-dir "/var/lib/$AGENT_USER" \
    --shell /usr/sbin/nologin \
    --gid "$AGENT_GROUP" \
    "$AGENT_USER"
fi

usermod -aG "$AGENT_GROUP" root

mkdir -p \
  "$APP_DIR/data/notebooklm-browser-profile" \
  "$APP_DIR/data/runtime" \
  "$APP_DIR/exports/task_extractor"

chgrp -R "$AGENT_GROUP" "$APP_DIR/data" "$APP_DIR/exports"
chmod -R g+rwX "$APP_DIR/data" "$APP_DIR/exports"
find "$APP_DIR/data" "$APP_DIR/exports" -type d -exec chmod g+s {} +

if ! command -v google-chrome >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates wget fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libu2f-udev libvulkan1 \
    libxcomposite1 libxdamage1 libxkbcommon0 libxrandr2 xdg-utils
  chrome_deb="/tmp/google-chrome-stable_current_amd64.deb"
  wget -q -O "$chrome_deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y "$chrome_deb"
  rm -f "$chrome_deb"
fi

if ! command -v Xvfb >/dev/null 2>&1 || ! command -v x11vnc >/dev/null 2>&1; then
  apt-get update
  apt-get install -y xvfb x11vnc
fi

if [ -x "$APP_DIR/.venv/bin/pip" ]; then
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

if [ ! -f "$APP_DIR/data/notebooklm-vnc.pass" ]; then
  VNC_PASSWORD="${NOTEBOOKLM_AGENT_VNC_PASSWORD:-}"
  if [ -z "$VNC_PASSWORD" ]; then
    VNC_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
    echo "Generated VNC password: $VNC_PASSWORD"
    echo "Save it now. It is also stored in $APP_DIR/data/notebooklm-vnc.pass for x11vnc."
  fi
  x11vnc -storepasswd "$VNC_PASSWORD" "$APP_DIR/data/notebooklm-vnc.pass" >/dev/null
  chown "$AGENT_USER:$AGENT_GROUP" "$APP_DIR/data/notebooklm-vnc.pass"
  chmod 0640 "$APP_DIR/data/notebooklm-vnc.pass"
fi

cat >"$APP_DIR/data/notebooklm-agent.env" <<EOF
NOTEBOOKLM_AGENT_DISPLAY=$DISPLAY_ID
NOTEBOOKLM_AGENT_VNC_PORT=$VNC_PORT
NOTEBOOKLM_AGENT_PROFILE_DIR=$APP_DIR/data/notebooklm-browser-profile
NOTEBOOKLM_AGENT_EXPORTS_ROOT=$APP_DIR/exports/task_extractor
EOF

chown "$AGENT_USER:$AGENT_GROUP" "$APP_DIR/data/notebooklm-agent.env"
chmod 0640 "$APP_DIR/data/notebooklm-agent.env"

echo "NotebookLM agent runtime prepared."
echo "User: $AGENT_USER"
echo "Group: $AGENT_GROUP"
echo "Display: $DISPLAY_ID"
echo "VNC port: $VNC_PORT"
