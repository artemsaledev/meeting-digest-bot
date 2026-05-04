# Daily Plan Design

Daily planning must be separated from generic meeting digests. The bot should use only Loom meetings explicitly marked with `#daily`.

## Meeting Format

Recommended spoken structure:

```text
Daily 2026-05-04. Команда Bitrix Develop Team.

Иван Карповец.
План на сегодня:
1. Проверить смену способа оплаты.
2. Подготовить демо.
Блокеры: жду ответ Игоря.

Михаил Конев.
План на сегодня:
1. Протестировать Payments Pro.
Блокеры: нет.
```

Rules:

- Say the full person name before their plan.
- Use stable section markers: `План на сегодня`, `Блокеры`, optionally `Сделано вчера`.
- Avoid only short names when the name is ambiguous.
- Keep one responsible person per block.

## People Directory

The static people directory is stored in:

```text
meeting_digest_bot/people_directory.json
```

Each person has:

- `full_name`
- `bitrix_user_id`
- `profile_url`
- `aliases`
- `telegram_username`

Current Bitrix user mapping:

| Person | Bitrix user ID |
|---|---:|
| Иван Карповец | 51977 |
| Анатолий Карповец | 58194 |
| Михаил Конев | 127124 |
| Артем Явдокименко | 114736 |
| Виктор Шавловский | 137230 |
| Эмиль Смолин | 50760 |
| Николай Ладенко | 123170 |
| Василий Точилин | 120601 |
| Игорь Закорчемный | 426 |
| Валентин Семенихин | 162783 |
| Андрей Решетицкий | 163323 |

Telegram usernames are used only for Telegram reports and mentions. If a person has no `telegram_username`, the report falls back to their full name.

## Checklist Members

Bitrix checklist items support the writable field `MEMBERS`.

For daily plan items:

```json
{
  "TITLE": "Проверить смену способа оплаты",
  "PARENT_ID": 123,
  "IS_COMPLETE": "N",
  "MEMBERS": [51977]
}
```

If the responsible person cannot be matched, the item should still be created, but the responsible should be included in the text as fallback.

## Implemented Commands

```text
@LLMeets_bot план 2026-05-04 preview
@LLMeets_bot план 2026-05-04 создать
@LLMeets_bot план 2026-05-04 создать команда Bitrix Develop Team
@LLMeets_bot итоги вчера
@LLMeets_bot итоги 2026-05-04
@LLMeets_bot итоги недели 2026-05-04 2026-05-08
python -m meeting_digest_bot sync-daily-plan --report-date 2026-05-04 --action preview
python -m meeting_digest_bot daily-report --yesterday
python -m meeting_digest_bot weekly-report --current-week
```

The command creates or previews a Bitrix task named `План дня DD.MM.YYYY / <team>`.
Only meetings tagged as `#daily` are included in the daily plan source set.

## Completion Reports

Daily completion reports:

- read the daily plan task checklist for the selected date
- count completed and open items
- group open items by responsible person
- add a comment to the daily plan task
- optionally send a Telegram report with responsible usernames
- do not move open items to the next day

Cron schedule on the VPS:

```text
09:00 Europe/Kyiv every day: daily-report --yesterday
16:00 Europe/Kyiv every Friday: weekly-report --current-week
```

Manual commands:

```text
@LLMeets_bot итоги вчера
@LLMeets_bot итоги 2026-05-04
@LLMeets_bot итоги недели 2026-05-04 2026-05-08
```

## Weekly Daily-Plan Report

Weekly report should query daily plan tasks for a date range and summarize:

- completed daily checklist items
- incomplete items from previous days
- incomplete items still open next morning
- responsible person for each incomplete item
- recurring blockers

If a daily task was deleted and not recreated through the bot, the weekly report cannot find it by title and skips that day. If the daily task was recreated through the bot, the new `daily_plan` binding replaces the old task ID and the weekly report uses the new task.
