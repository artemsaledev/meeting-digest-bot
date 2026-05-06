#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/opt/backups/meeting-digest-bot}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
KNOWLEDGE_REPO="${KNOWLEDGE_REPO:-/opt/company-knowledge}"
BOT_DATA_DIR="${BOT_DATA_DIR:-/opt/meeting-digest-bot/data}"
AICALLORDER_DB="${AICALLORDER_DB:-/opt/AIcallorder/data/loom_automation.db}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target_dir="$BACKUP_ROOT/$timestamp"
archive_path="$BACKUP_ROOT/knowledge-backup-$timestamp.tar.gz"

mkdir -p "$target_dir" "$BACKUP_ROOT"

if [ -d "$KNOWLEDGE_REPO" ]; then
  rsync -a --delete --exclude='.git/' "$KNOWLEDGE_REPO/" "$target_dir/company-knowledge/"
fi

if [ -d "$BOT_DATA_DIR" ]; then
  rsync -a "$BOT_DATA_DIR/" "$target_dir/meeting-digest-bot-data/"
fi

if [ -f "$AICALLORDER_DB" ]; then
  mkdir -p "$target_dir/aicallorder-data"
  cp -a "$AICALLORDER_DB" "$target_dir/aicallorder-data/"
fi

tar -C "$target_dir" -czf "$archive_path" .
rm -rf "$target_dir"
find "$BACKUP_ROOT" -name 'knowledge-backup-*.tar.gz' -type f -mtime "+$RETENTION_DAYS" -delete

printf '{"ok":true,"archive":"%s","retention_days":%s}\n' "$archive_path" "$RETENTION_DAYS"
