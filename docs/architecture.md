# Architecture

## Components

```text
Loom
  -> AIcallorder
     -> Transcript / artifacts SQLite DB
     -> Telegram digest post
     -> MeetingDigestBot /publications/register
  -> MeetingDigestBot
     -> Telegram command handler
     -> State DB bindings
     -> Bitrix REST API
```

## MeetingDigestBot Modules

- `app.py`: FastAPI API for registration and sync endpoints.
- `telegram_poller.py`: Telegram long-polling worker.
- `telegram_bot.py`: Telegram command parser and response formatter.
- `service.py`: core orchestration, publication sync, merge updates.
- `task_drafts.py`: CRM task/comment/checklist draft generation.
- `bitrix_client.py`: Bitrix REST API wrapper.
- `aicallorder_db.py`: AIcallorder SQLite read adapter.
- `state_db.py`: local state, publication records, task bindings.
- `weekly_llm.py`: optional daily/weekly LLM rollup enhancer.

## State

MeetingDigestBot keeps its own SQLite DB:

```text
data/meeting_digest_bot.db
```

It stores:

- registered Telegram publications
- source-to-Bitrix task bindings
- weekly rollup metadata

Runtime state is not committed to git.

## Why Separate From AIcallorder

The service has a separate lifecycle:

- independent Telegram bot commands
- independent Bitrix REST credentials and publication rules
- independent state DB
- independent systemd services
- independent deployment and rollback

AIcallorder remains focused on Loom ingestion and transcript/artifact production.

