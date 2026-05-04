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

## Planned Commands

```text
@LLMeets_bot план 2026-05-04
@LLMeets_bot хвосты 2026-05-04
@LLMeets_bot отчет недели 2026-04-27 2026-05-03
```

## Weekly Report

Weekly report should query daily plan tasks for a date range and summarize:

- completed daily checklist items
- incomplete items from previous days
- incomplete items still open next morning
- responsible person for each incomplete item
- recurring blockers

