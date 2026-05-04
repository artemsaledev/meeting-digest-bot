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

### Help

```text
@LLMeets_bot помощь
/help@LLMeets_bot
```

Shows a short Telegram help message with command examples.

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
@LLMeets_bot план 2026-05-04 preview
@LLMeets_bot план 2026-05-04 создать
@LLMeets_bot план 2026-05-04 создать команда Bitrix Develop Team
@LLMeets_bot итоги вчера
@LLMeets_bot итоги 2026-05-04
@LLMeets_bot итоги недели 2026-05-04 2026-05-08
/day@LLMeets_bot 2026-05-01 week 166229
/week@LLMeets_bot 2026-04-27 2026-05-03
```

Daily-to-weekly mode appends daily commitments to a weekly task.
Daily plan mode uses only Loom meetings marked with `#daily`, parses people blocks, and adds Bitrix checklist `MEMBERS` for recognized responsible users.

Daily report mode reads the checklist completion status of the daily plan task. It does not move open items to the next day. It adds a CRM comment to the daily task and returns a Telegram-formatted report with responsible usernames for open items.

Weekly daily-plan report mode scans daily plan task bindings in the requested date range. If a day task was deleted and not recreated through the bot, that day is skipped. If a day task was recreated through the bot, the new binding is used.

For a non-technical user guide, see [User Telegram Guide](user-telegram-guide.md).
