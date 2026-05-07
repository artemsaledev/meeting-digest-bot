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

Weekly report queries daily plan tasks for a date range and summarizes the week in the same PM-oriented style as daily plans.

It reads actual CRM checklist state, not the original transcript snapshot. If a user edits the checklist during the week, the weekly report uses the current CRM state.

Weekly report includes:

- execution summary: found daily tasks, total checklist items, closed items, open items, missing daily tasks
- weekly focus extracted from daily task descriptions
- closed checklist items by day and responsible person
- open checklist items by day and responsible person
- PM follow-up items from `Чеклист ПМа`, `PM: Требует подтверждения`, and `PM: Не потерять сегодня`
- verification / risk items based on checklist group and item text
- links to source daily tasks

If a daily task was deleted and not recreated through the bot, the weekly report cannot find it by title and skips that day. If the daily task was recreated through the bot, the new `daily_plan` binding replaces the old task ID and the weekly report uses the new task.

The weekly report format is intentionally not a meeting digest. It is a PM completion digest:

```text
Единый weekly PM-дайджест 04.05 - 08.05.2026
Сводка выполнения
Фокус недели по daily-планам
Закрыто за неделю
Не закрыто по дням
PM follow-up / контроль ПМа
Требует проверки / риски
Daily-задачи не найдены
Источники daily-задач
```

The report does not re-run the full daily LLM parser. It combines the already published daily task descriptions with current checklist status from Bitrix. This keeps the weekly report stable when the transcript was noisy but the PM later cleaned the task manually.

Manual command examples:

```text
@LLMeets_bot итоги недели 2026-05-04 2026-05-08
python -m meeting_digest_bot weekly-report --week-from 2026-05-04 --week-to 2026-05-08 --force
```

## PM Daily Checklist Layer

Daily plan creation now has an optional LLM layer controlled by:

```text
MEETING_DIGEST_DAILY_PM_LLM_ENABLED=true
LLM_API_KEY=<OpenAI-compatible API key>
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=<model>
```

If these values are empty in MeetingDigestBot `.env`, the service also checks `AICALLORDER_ENV_PATH` or, by default, the `.env` file next to `AICALLORDER_DB_PATH`. This allows the daily PM layer to reuse the existing AIcallorder LLM credentials without duplicating secrets.

When enabled, the bot does not publish the raw transcript-like parser output directly. It first builds a PM operating checklist from the daily transcript, AI summary, and fallback parser result.

The transcript is treated as the source of truth. The AI summary is used only as helper structure. If the summary says something is done, but the transcript has markers like `вроде`, `кажется`, `надо проверить`, `после daily обсудим`, `скину`, `найду`, the item is moved to `needs_verification`, `in_progress`, or `waiting_dependency`.

The generated task description must contain these sections:

```text
1. Фокус дня
2. Чеклист ПМа
3. План по людям
4. Зависимости / последовательности
5. Требует подтверждения / ручной проверки
6. Не закрыто / в работе
7. Блокеры / риски
8. Не потерять сегодня
```

CRM checklists are created in four layers:

- `Чеклист ПМа`: PM follow-ups assigned to Artem Yavdokimenko, user ID `114736`.
- `PM: Требует подтверждения`: manual verification points assigned to user ID `114736`.
- `PM: Не потерять сегодня`: small follow-ups assigned to user ID `114736`.
- Per-person checklist groups: normalized LLM `people_plan`, assigned through `people_directory.json`.

If the LLM layer is disabled or unavailable, the bot falls back to the deterministic `daily_plan_v2` parser and adds a service note to the task comment.
