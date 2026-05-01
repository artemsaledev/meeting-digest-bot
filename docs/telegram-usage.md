# Telegram Usage

The bot is intended to work inside a Telegram group that receives Loom digest posts from `AIcallorder`.

## Basic Flow

1. Find the Loom digest post in the group.
2. Reply to that post.
3. Mention the bot and add a command.

Example:

```text
@LLMeets_bot preview
```

## Commands

### Preview

```text
@LLMeets_bot preview
```

Shows what the bot recognized and what it would do. It does not write to CRM.

### Create A New CRM Task

```text
@LLMeets_bot создать
```

Creates a new task in project `Bitrix Develop Team`.

### Add Comment To Existing Task

```text
@LLMeets_bot коммент 166229
```

Adds meeting results from processed transcript artifacts as a task comment.

The comment includes:

- short summary
- accepted decisions
- action items / commitments
- completed / confirmed items
- blockers
- remaining tech debt
- business requests for estimation
- open questions
- Loom, Google Doc, and Transcript Doc links

### Add Checklists To Existing Task

```text
@LLMeets_bot чеклист 166229
```

Adds QA and PM acceptance checklist groups. Existing checklist items are deduplicated by normalized text.

### Merge Update Existing Task

```text
@LLMeets_bot обновить 166229
```

Updates the task description in non-destructive merge mode:

- existing description is preserved as context if needed
- the current meeting is added as `Point N`
- the same source can be rerun and will replace only its own point
- QA/PM checklist items are added under a point-specific checklist group
- a meeting-results comment is also added

### Register Old Digest Post

```text
@LLMeets_bot зарегистрировать
```

Use this when the bot says the publication is not registered. The command must be sent as a reply to the original digest post.

The bot extracts:

- Telegram post URL
- Loom URL / video ID
- meeting title
- Google Doc URL
- Transcript Doc URL
- document section titles

## Explicit Post Link Mode

If needed, the post URL can be sent directly:

```text
@LLMeets_bot https://t.me/c/<chat>/<message> preview
@LLMeets_bot https://t.me/c/<chat>/<message> коммент 166229
```

## Daily And Weekly Commands

```text
/day@LLMeets_bot 2026-05-01 week 166229
/week@LLMeets_bot 2026-04-27 2026-05-03
```

Daily-to-weekly mode appends daily commitments to a weekly task.

