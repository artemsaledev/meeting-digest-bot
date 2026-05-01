# Bitrix Publication Rules

## Task Creation

New tasks are created with these fields:

```text
GROUP_ID=512
CREATED_BY=114736
RESPONSIBLE_ID=114736
AUDITORS=50760,127124,137230,51977
TAGS=meeting-digest,loom-digest
```

The task description is generated from the processed meeting artifact and contains the key task statement, links, scope, requirements, dependencies, blockers, and open questions.

## Comments

Comments are sent through `task.commentitem.add` with:

```text
AUTHOR_ID=114736
```

This is used instead of the chat-message method when an explicit author is configured.

The comment is plain text and must not contain Markdown tables or pipe-delimited pseudo-tables.

Structured items are rendered like this:

```text
- Main item text
  Ответственный: name
  Срок: date
  Статус: status
```

Runtime formatting also converts:

- markdown tables into plain text rows
- `owner=`, `due=`, `status=`, `priority=` pipe metadata into indented fields

## Checklists

Checklist groups are added through `task.checklistitem.add`.

The service creates:

- `QA`
- `Критерии приемки PM`
- `Point N: <meeting title>` for merge updates

Deduplication:

- existing checklist groups are reused by title
- existing checklist items are skipped by normalized text

Bitrix limitation:

- `CREATED_BY` is readable/sortable for checklist items but not writable.
- The checklist author is the REST webhook user.
- To make checklist items authored by `114736`, use a webhook issued under user `114736`.

## Merge Update

The `обновить` command is non-destructive.

It uses plain markers:

```text
=== MEETING_DIGEST_POINT START source_type=meeting source_key=<loom_id> ===
...
=== MEETING_DIGEST_POINT END source_type=meeting source_key=<loom_id> ===
```

If the same source is processed again, only that source point is replaced.

## Required Existing Task

These commands require either an existing binding or explicit task ID:

- `коммент`
- `чеклист`
- `обновить`
- daily-to-weekly append

If no task is known, the bot returns an error instead of creating a new task accidentally.

