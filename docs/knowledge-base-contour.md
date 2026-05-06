# Knowledge Base Intake Contour

This contour turns large Loom task flows into accumulated knowledge objects.
It is read-only for the current MeetingDigestBot runtime: it does not write to
CRM, Notion, or a future Git knowledge repository.

## Scope

Included source meetings:

- `#task_discussion`
- `#task_demo`

Excluded source meetings:

- `#daily`
- daily plan tasks
- daily and weekly completion reports

The intent is to preserve high-signal product and implementation knowledge,
not operational day planning.

AIcallorder should pass the tags explicitly during publication registration:

```json
{
  "source_tags": ["#task_discussion"],
  "payload": {
    "source_tags": ["#task_discussion"]
  }
}
```

The intake still falls back to hashtags found in the meeting title, artifacts,
and publication payload, but `source_tags` is the stable integration contract.

## Source Flow

```text
AIcallorder meeting artifacts
  + registered Telegram publication
  + MeetingDigestBot TaskDraft
  + Bitrix task binding
  -> KB intake
  -> accumulated knowledge object
  -> Markdown/JSON bundle for Git, NotebookLM, or another AI
```

## CLI

Initialize the Git-friendly knowledge repository scaffold:

```powershell
python -m meeting_digest_bot init-knowledge-repo --knowledge-dir company-knowledge
```

Export all registered meeting publications that qualify:

```powershell
python -m meeting_digest_bot export-knowledge --output-dir exports/knowledge
```

Export one Telegram publication:

```powershell
python -m meeting_digest_bot export-knowledge --post-url https://t.me/c/5147878786/120
```

Export a date window:

```powershell
python -m meeting_digest_bot export-knowledge --date-from 2026-05-01 --date-to 2026-05-05
```

Upsert accumulated objects into the Git-friendly repository:

```powershell
python -m meeting_digest_bot upsert-knowledge --knowledge-dir company-knowledge
```

Create review drafts instead of final task-case documents:

```powershell
python -m meeting_digest_bot upsert-knowledge --knowledge-dir company-knowledge --draft
```

Build a local MVP search index and query it:

```powershell
python -m meeting_digest_bot index-knowledge --knowledge-dir company-knowledge
python -m meeting_digest_bot search-knowledge "Bitrix checklist" --knowledge-dir company-knowledge
python -m meeting_digest_bot ask-knowledge "как работает синхронизация чеклистов?" --knowledge-dir company-knowledge
```

Create a prompt-correction proposal:

```powershell
python -m meeting_digest_bot revise-knowledge `
  --knowledge-dir company-knowledge `
  --object-id task_case__bitrix_123 `
  --correction "учти новый demo-сценарий с дедупликацией"
```

Backfill explicit source tags for old publications and inspect pending
knowledge candidates:

```powershell
python -m meeting_digest_bot backfill-knowledge-tags
python -m meeting_digest_bot list-knowledge-candidates
```

## Bundle Shape

Each object is exported into:

```text
exports/knowledge/<object_id>/
  source_bundle/
    00_readme.md
    01_overview.md
    02_functional_spec.md
    03_decisions.md
    04_acceptance_criteria.md
    05_demo_feedback.md
    06_source_events.md
    07_sources.md

  machine_bundle/
    knowledge_object.json
    ai_context.json
    retrieval_manifest.json

  prompt_workspace/
    README.md
    object_context.md
    revise_knowledge_object.md
    generate_user_instruction.md
    generate_technical_spec.md
    detect_conflicts.md
```

Use `source_bundle/*.md` as NotebookLM sources. Use
`machine_bundle/ai_context.json` for API-based assistants and automation tools.
Use `prompt_workspace/*.md` as prompt templates for reviewable corrections.

The default command writes every bundle plus compatibility top-level files:

```powershell
python -m meeting_digest_bot export-knowledge --bundle all
```

Focused exports are also supported:

```powershell
python -m meeting_digest_bot export-knowledge --bundle source
python -m meeting_digest_bot export-knowledge --bundle machine
python -m meeting_digest_bot export-knowledge --bundle prompts
```

## Prompt Correction Pattern

External AI tools should work against exported bundles and propose changes.
They should not directly mutate the source of truth.

Recommended prompt pattern:

```text
Use the attached knowledge object as grounded context.
Update the functional specification only from source events in this bundle.
If discussion and demo conflict, prefer demo.
Do not delete older requirements; mark them as superseded in the proposed patch.
Return a change proposal with source references.
```

The future Git-backed knowledge repository should accept those proposals as
reviewable diffs before Notion sync or vector index rebuild.

## Implementation Coverage

The current contour covers the original eight steps and the next catalog layer:

1. Git knowledge repository scaffold in `company-knowledge/`.
2. Cumulative upsert into `knowledge/task_cases`.
3. Draft/proposal mode for human review.
4. Notion mapping scaffold and per-object Notion projection JSON.
5. Local lexical index plus `search-knowledge` / `ask-knowledge` MVP.
6. Prompt correction proposal command.
7. Automatic KB candidate marking during publication registration.
8. Backfill command for old publication tags.
9. Derived catalogs for `Systems`, `Features`, and `Instructions`.
10. API endpoints for search, grounded answers, object lookup, and machine bundles.

Generate catalogs manually:

```powershell
python -m meeting_digest_bot derive-knowledge-catalogs --knowledge-dir company-knowledge
```

The automatic pipeline runs this derive step after task-case upsert and before
index/export/sync.

## Automatic Pipeline

Run the whole local pipeline:

```powershell
python -m meeting_digest_bot process-knowledge-pipeline `
  --knowledge-dir company-knowledge `
  --export-target notebooklm
```

Lifecycle:

```text
registered publication
  -> kb_candidates.pending
  -> upsert-knowledge
  -> derive Systems / Features / Instructions
  -> kb_candidates.exported
  -> index-knowledge + chunk-index-knowledge
  -> kb_candidates.indexed
  -> optional external export zip
  -> Notion sync
  -> optional Git commit/push wrapper on VPS
```

Inspect pipeline runs:

```powershell
python -m meeting_digest_bot list-knowledge-runs
```

## Revision Review

Create a proposal:

```powershell
python -m meeting_digest_bot revise-knowledge `
  --knowledge-dir company-knowledge `
  --object-id task_case__bitrix_123 `
  --correction "уточнить demo-поведение"
```

Approve and apply:

```powershell
python -m meeting_digest_bot set-revision-status `
  --metadata-path company-knowledge/knowledge/drafts/task_case__bitrix_123__revision_proposal.json `
  --status approved

python -m meeting_digest_bot apply-revision `
  --metadata-path company-knowledge/knowledge/drafts/task_case__bitrix_123__revision_proposal.json
```

## Notion Back-Import

Manual edits in Notion are imported as review proposals, not as direct writes
to canonical JSON:

```powershell
python -m meeting_digest_bot import-knowledge-notion --knowledge-dir company-knowledge
```

Narrow the import when reviewing a specific area:

```powershell
python -m meeting_digest_bot import-knowledge-notion `
  --knowledge-dir company-knowledge `
  --database "Task Cases" `
  --object-id task_case__bitrix_123
```

Detected changes are written to:

```text
knowledge/drafts/notion_import/<object_id>__notion_import.md
knowledge/drafts/notion_import/<object_id>__notion_import.json
knowledge/drafts/notion_import/<object_id>__notion_live.md
```

The Markdown proposal contains a unified diff between the local Notion
projection and the live Notion page. After review, approve/apply through the
normal revision flow or manually patch canonical JSON and let the pipeline
regenerate indexes, exports, Notion, and Git.

## External AI Export

NotebookLM/RAG zip:

```powershell
python -m meeting_digest_bot export-external-knowledge `
  --knowledge-dir company-knowledge `
  --target notebooklm
```

Agent/API JSON zip:

```powershell
python -m meeting_digest_bot export-external-knowledge `
  --knowledge-dir company-knowledge `
  --target agents
```

## Notion Setup Gate

Dry-run is local:

```powershell
python -m meeting_digest_bot sync-knowledge-notion --knowledge-dir company-knowledge
```

Apply currently stops unless these external resources exist:

- Notion integration token in `NOTION_API_KEY`.
- Prefer Notion data source ID in `NOTION_DATA_SOURCE_TASK_CASES`.
- Legacy fallback: Notion database ID in `NOTION_DB_TASK_CASES`.
- Matching env vars for `SYSTEMS`, `FEATURES`, and `INSTRUCTIONS` when those
  derived projections exist.

Manual action required before enabling real writes: create Notion databases for
Task Cases, Systems, Features, and Instructions, then configure the env vars.

For the Task Cases database, create these properties:

```text
Title          title
ID             text
Type           select
Status         select
System         select
Feature Area   text
Tags           multi-select
Bitrix Tasks   text
Updated At     date
```

Then:

1. Create an internal integration at https://www.notion.so/my-integrations.
2. Enable read, insert, and update content capabilities.
3. Open the real Task Cases database page in Notion, not a linked database view.
4. Use `...` -> `Add connections` and select the integration.
5. Copy the data source ID from Notion if available and set:

```bash
NOTION_API_KEY=secret_...
NOTION_DATA_SOURCE_TASK_CASES=...
```

If you only have the database ID, set:

```bash
NOTION_DB_TASK_CASES=...
NOTION_DB_SYSTEMS=...
NOTION_DB_FEATURES=...
NOTION_DB_INSTRUCTIONS=...
```

With env configured, apply sync:

```powershell
python -m meeting_digest_bot sync-knowledge-notion --knowledge-dir company-knowledge --apply
```

Current Notion IDs for workspace `artemsaledev`, page `База знаний AI`:

```text
Parent page ID: 357ab9ab-91de-80c4-99f7-fbcb722768ee
Task Cases:     357ab9ab-91de-81a8-b20b-d3f0eae0601e
Systems:        357ab9ab-91de-813f-b926-e6fadaaeac84
Features:       357ab9ab-91de-8185-a229-f575efda9c0c
Instructions:   357ab9ab-91de-81b6-8428-cc79e732b225
```

The API sync was validated with `Task Cases` using a temporary smoke object.

## Knowledge API

When `KNOWLEDGE_REPO_PATH` points to the repo, the FastAPI service exposes:

```text
GET /knowledge/search?q=bitrix checklist&limit=5
GET /knowledge/ask?q=how checklist sync works&limit=5
GET /knowledge/object/{object_id}
GET /knowledge/machine-bundle/{object_id}
POST /knowledge/notion/import
GET /knowledge/rag/stats
GET /knowledge/rag/search?q=bitrix checklist&limit=5
POST /knowledge/rag/ask
```

These endpoints are intentionally read-only. They are the API surface for
agents, RAG services, and prompt workspaces that need grounded context. The
Notion import endpoint writes only draft proposals.

## Lightweight RAG Layer

The current VPS uses a lightweight RAG contour instead of a local vector
service:

- chunk source: `indexes/knowledge_chunks.json`
- embedding generation: external OpenAI-compatible `/embeddings` API
- storage: local SQLite cache at `indexes/knowledge_vectors.sqlite`
- retrieval: cosine top-k over cached chunk embeddings
- answer generation: external OpenAI-compatible `/chat/completions` API

Useful commands:

```bash
/opt/meeting-digest-bot/.venv/bin/python -m meeting_digest_bot build-knowledge-rag-index --knowledge-dir /opt/company-knowledge
/opt/meeting-digest-bot/.venv/bin/python -m meeting_digest_bot rag-search-knowledge --knowledge-dir /opt/company-knowledge "Bitrix checklist"
/opt/meeting-digest-bot/.venv/bin/python -m meeting_digest_bot rag-knowledge --knowledge-dir /opt/company-knowledge "How does Bitrix checklist sync work?"
```

Set `KNOWLEDGE_RAG_ENABLED=true` in `knowledge-pipeline.env` to rebuild the
SQLite vector cache during the automatic KB pipeline. If the external API key is
missing, the legacy lexical search and all Notion/GitHub/Telegram sync paths
continue to work.

## VPS Deployment Scaffold

Files:

```text
deploy/linux/knowledge-pipeline.env.example
deploy/linux/systemd/meeting-digest-bot-knowledge.service.example
deploy/linux/systemd/meeting-digest-bot-knowledge.timer.example
```

Manual action required before deploy: choose where the Git knowledge repo lives
(`/opt/company-knowledge` or GitHub checkout), configure backups, and install the
systemd service/timer on the VPS.
