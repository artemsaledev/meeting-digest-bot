#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MEETING_DIGEST_BOT_BASE_URL:-http://127.0.0.1:8011}"

echo "Health check: $BASE_URL/health"
curl -fsS "$BASE_URL/health"
echo

echo "API service status:"
systemctl --no-pager --full status meeting-digest-bot-api.service || true

echo
echo "Poller service status:"
systemctl --no-pager --full status meeting-digest-bot-poller.service || true

echo
echo "Recent API logs:"
journalctl -u meeting-digest-bot-api.service -n 80 --no-pager || true

echo
echo "Recent poller logs:"
journalctl -u meeting-digest-bot-poller.service -n 80 --no-pager || true
