# Knowledge Base Governance

## Source Of Truth

GitHub repository `artemsaledev/company-knowledge` is the canonical source of
truth. Notion is an editing and review surface. NotebookLM and agent exports are
generated artifacts.

## Write Rules

- Loom/Telegram/Bitrix task flows write canonical task cases automatically.
- Derived `Systems`, `Features`, and `Instructions` are regenerated from task
  cases.
- Manual Notion edits are imported only as draft proposals.
- Canonical JSON changes require review before apply.
- Git history is the rollback mechanism.

## Notion Proposal Lifecycle

1. `import-knowledge-notion --notify-proposals` scans Notion.
2. Changed pages create `knowledge/drafts/notion_import/*`.
3. Telegram alert is sent when proposals are found.
4. Reviewer approves the metadata JSON by setting `status=approved`.
5. `apply-notion-import` updates canonical JSON and regenerated artifacts.
6. Pipeline rebuilds indexes, exports, Notion pages, and Git commit.

## Conflict Policy

- Demo evidence wins over older discussion evidence.
- Notion deletion of large sections is treated as a conflict, not as an
  automatic delete.
- Unknown Notion pages are skipped unless a canonical object with matching `ID`
  exists.
- Generated instructions and specifications must cite source object IDs.

## Operational Checks

Run:

```bash
python -m meeting_digest_bot knowledge-health --knowledge-dir /opt/company-knowledge
```

## Operational commands

Use `kb health` in the Telegram admin group for a short live status: pending
proposals, RAG chunk count, usage estimate, and quality issue count.

Use `kb ask <question>` to query the canonical knowledge base from Telegram. The
bot uses the external embedding/LLM RAG path when configured and falls back to
local lexical search when external AI credentials are missing.

Use the CLI for controlled governance actions:

```bash
python -m meeting_digest_bot knowledge-quality-report --knowledge-dir /opt/company-knowledge
python -m meeting_digest_bot set-knowledge-object-status --knowledge-dir /opt/company-knowledge --object-id task_case__... --status approved
python -m meeting_digest_bot set-knowledge-object-status --knowledge-dir /opt/company-knowledge --object-id task_case__... --status archived
python -m meeting_digest_bot knowledge-rag-costs --knowledge-dir /opt/company-knowledge
```

Archived objects remain in Git for audit, but they are excluded from derived
catalogs, the lexical index, chunk index, RAG index, and external export bundles.

Backups are handled by `meeting-digest-bot-backup.timer`; archives are written
to `/opt/backups/meeting-digest-bot` with a default 14 day retention window.

Expected healthy state:

- last pipeline run status is `success`;
- Notion sync applies without missing env;
- Git working tree is clean after scheduled jobs;
- pending Notion proposals are visible and reviewed.
