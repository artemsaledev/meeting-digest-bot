# MeetingDigestBot

`MeetingDigestBot` is a standalone service around `AIcallorder` that turns Loom meeting digests from Telegram into Bitrix/CRM tasks, comments, checklists, and daily/weekly rollups.

The service is intentionally separated from `AIcallorder`: `AIcallorder` records and processes Loom artifacts, while `MeetingDigestBot` owns CRM publication, Telegram commands, Bitrix task formatting, state bindings, and deployment runtime.

## What It Does

- Registers Telegram digest posts published by `AIcallorder`.
- Reads processed meeting artifacts from the `AIcallorder` SQLite database by `loom_video_id`.
- Creates Bitrix tasks in project `Bitrix Develop Team` (`GROUP_ID=512`).
- Adds meeting summaries as CRM comments.
- Adds QA and PM acceptance checklists.
- Updates existing tasks in merge mode with `Point N` sections instead of destructive overwrites.
- Supports manual Telegram commands in a group by replying to a digest post.
- Can aggregate daily and weekly meeting results.
- Provides a foundation for `#daily` planning with Bitrix checklist members.
- Exports large `#task_discussion` / `#task_demo` flows as accumulated knowledge-object bundles for a future Git/Notion/RAG knowledge base.

## Current Production Defaults

- Bitrix project: `GROUP_ID=512`
- Task creator: `114736`
- Task responsible: `114736`
- Task auditors: `50760, 127124, 137230, 51977`
- Comment author: `114736`
- VPS app path: `/opt/meeting-digest-bot`
- AIcallorder path: `/opt/AIcallorder`
- API service: `meeting-digest-bot-api.service`
- Telegram poller service: `meeting-digest-bot-poller.service`

Checklist limitation: Bitrix does not expose `CREATED_BY` as a writable field for `task.checklistitem.add`. Checklist items are authored by the webhook user. To make checklist items visually authored by `114736`, create the Bitrix webhook under user `114736`.

## Telegram Commands

Reply to a Loom digest post in the Telegram group:

```text
@LLMeets_bot preview
@LLMeets_bot создать
@LLMeets_bot коммент 166229
@LLMeets_bot чеклист 166229
@LLMeets_bot обновить 166229
@LLMeets_bot зарегистрировать
@LLMeets_bot план 2026-05-04 создать
@LLMeets_bot итоги вчера
@LLMeets_bot итоги недели 2026-05-04 2026-05-08
```

Use `preview` first when unsure. If a post is old and not registered, reply to it with `@LLMeets_bot зарегистрировать`, then run the needed command again.

User-facing command guide: [Telegram User Guide](docs/user-telegram-guide.md).
Technical command notes: [Telegram Usage](docs/telegram-usage.md).

## CRM Publication Modes

- `preview`: shows what would be created/updated without writing to CRM.
- `создать` / `create`: creates a new Bitrix task.
- `коммент` / `comment`: adds a rich meeting-results comment to an existing task.
- `чеклист` / `checklist`: adds QA/PM checklist groups to an existing task.
- `обновить` / `update`: merges a meeting into the task description as a new `Point N`, creates a point-specific checklist, and adds a comment.

More details: [Bitrix Publication Rules](docs/bitrix-publication.md).

Daily planning design: [Daily Plan](docs/daily-plan.md).
Knowledge-base intake contour: [Knowledge Base Intake Contour](docs/knowledge-base-contour.md).
Knowledge-base user guide: [Knowledge Base User Guide](docs/knowledge-base-user-guide.md).
Knowledge-base governance: [Knowledge Base Governance](docs/knowledge-governance.md).

Knowledge-base MVP commands:

```powershell
python -m meeting_digest_bot init-knowledge-repo --knowledge-dir company-knowledge
python -m meeting_digest_bot upsert-knowledge --knowledge-dir company-knowledge
python -m meeting_digest_bot index-knowledge --knowledge-dir company-knowledge
python -m meeting_digest_bot ask-knowledge "Bitrix checklist" --knowledge-dir company-knowledge
```

Daily completion reports do not move open checklist items to the next day. They only read checklist completion status, add a report comment to the day task, and tag responsible users in Telegram. Cron runs daily reports at `09:00 Europe/Kyiv` and weekly daily-plan reports every Friday at `16:00 Europe/Kyiv`.

## Local Development

```powershell
cd "meeting-digest-bot"
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
python -m meeting_digest_bot api
python -m meeting_digest_bot poll-telegram
```

The `.env` file must not be committed.

## Deployment

Build a release archive on Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\package_for_vps.ps1
```

Deploy to Linux VPS and restart systemd services as described in [Deployment](docs/deployment.md).

## Integration With AIcallorder

`AIcallorder` should call the registration endpoint after publishing a Telegram digest:

```http
POST /publications/register
```

The payload must include the Telegram `post_url`, digest metadata, and Loom identifiers. Details: [AIcallorder Integration](docs/aicallorder-integration.md).

## Repository Layout

```text
meeting_digest_bot/       Python service
deploy/                   Windows packaging and Linux runtime files
docs/                     Project documentation
examples_task_artefacts/  Visual examples created during task analysis
MeetingDigestBot.readme   Long running implementation log
```

