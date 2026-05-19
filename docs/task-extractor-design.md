# Task Extractor Design

## Идея

`Task Extractor` - отдельный Telegram-контур для сбора нескольких встреч,
Loom-публикаций, ссылок на задачи и ручных вводных в один рабочий пакет. Его
цель - подготовить глубокую выгрузку для NotebookLM/LLM-проработки ТЗ, а после
подтверждения пользователя создать или обновить задачу в Bitrix по правилам,
близким к `MeetingDigestBot`.

В отличие от текущего сценария `MeetingDigestBot`, где команда обычно
обрабатывает одну публикацию, `Task Extractor` работает с пулом источников:

- несколько Telegram-постов из группы `Task Extractor`;
- Loom-ссылки из forwarded messages или обычного текста;
- ссылки на существующие Bitrix-задачи;
- текстовые списки задач, комментариев, требований и уточнений;
- Telegram-посты с ссылкой на задачу и ручным контекстом;
- транскрипции и артефакты из базы `AIcallorder`.

## Основной пользовательский сценарий

1. Пользователь пересылает в отдельную группу один или несколько постов со
   встречами, Loom-ссылками или списками задач.
2. Пользователь пишет команду вроде:

   ```text
   @TaskExtractorBot собрать
   ```

3. Бот создает или продолжает `extraction_session` и добавляет найденные
   источники в пул.
4. Пользователь может дослать дополнительные сообщения:

   ```text
   @TaskExtractorBot добавить
   ```

   или просто ответить на ранее созданную сессию списком ссылок/комментариев.

5. Команда:

   ```text
   @TaskExtractorBot preview
   ```

   показывает, какие источники распознаны, какие транскрипции найдены, какие
   Bitrix-задачи прочитаны и чего не хватает.

6. Команда:

   ```text
   @TaskExtractorBot выгрузка
   ```

   создает пакет для NotebookLM: Markdown-документы, JSON-манифест, ссылки на
   первоисточники и промпт для проработки ТЗ.

7. После анализа и подтверждения:

   ```text
   @TaskExtractorBot создать
   ```

   бот создает новую задачу в Bitrix.

   После успешного создания задачи бот должен сохранить export/handoff package,
   чтобы будущий агент-скилл мог забрать файлы для NotebookLM и создать по этой
   задаче отдельный проект/блокнот.

8. Для существующей задачи:

   ```text
   @TaskExtractorBot обновить 168334
   @TaskExtractorBot коммент 168334
   @TaskExtractorBot чеклист 168334
   ```

   бот использует тот же общий пакет источников, но пишет результат в выбранную
   задачу.

9. После завершения пользователь может очистить рабочую сессию:

   ```text
   @TaskExtractorBot очистить
   ```

   Это закрывает текущий пул источников, чтобы в группу можно было забросить
   следующую независимую порцию материалов.

## Дополнительные сценарии

### Пост с задачей и ручным контекстом

Пользователь может отправить или переслать в группу обычный Telegram-пост, где
есть ссылка на Bitrix-задачу и дополнительный текст:

```text
https://totiscrm.com/workgroups/group/512/tasks/task/view/168334/

Факт-чекнул итоги дня: AI summary пропустил два невыполненных пункта.
Нужно вынести это в отдельную задачу на доработку процесса.

Невыполнено:
- пункт 1
- пункт 2
```

Бот должен распознать это не как команду на одну существующую задачу, а как
`task_context_post` внутри текущей сессии:

- сохранить ссылку на задачу;
- прочитать описание, комментарии и checklist этой задачи из Bitrix;
- сохранить ручной текст как пользовательский факт-чек;
- отметить, что ручной факт-чек имеет высокий приоритет над AI summary;
- использовать этот контекст при генерации новой задачи или выгрузки для
  NotebookLM.

### Несколько встреч и ручные заметки по разным командам

Сценарий:

1. Прошло несколько встреч с разными командами.
2. Пользователь сохранял ручные Telegram-заметки и ссылки на задачи.
3. Потом закидывает эти заметки и ссылки в отдельную группу.
4. Запускает:

   ```text
   @TaskExtractorBot собрать
   ```

5. Бот собирает все посты текущей сессии:

   - встречи;
   - Loom/transcript artifacts;
   - task links;
   - ручные заметки;
   - комментарии к задачам.

6. Пользователь делает `preview`, затем `выгрузка` или `создать`.
7. После завершения пользователь очищает сессию:

   ```text
   @TaskExtractorBot очистить
   ```

8. Следующая пачка постов считается новой независимой задачей.

## Telegram UX

Рекомендуемая модель - отдельная группа и отдельный бот:

- группа: `Task Extractor`;
- invite link: `https://t.me/+yMgztS_nb4dmZmM6`;
- бот: `Task Extractor`;
- режим работы: session-first.

Минимальный набор команд:

```text
@TaskExtractorBot start
@TaskExtractorBot собрать
@TaskExtractorBot добавить
@TaskExtractorBot preview
@TaskExtractorBot выгрузка
@TaskExtractorBot создать
@TaskExtractorBot обновить 168334
@TaskExtractorBot коммент 168334
@TaskExtractorBot чеклист 168334
@TaskExtractorBot очистить
@TaskExtractorBot статус
```

Команды можно поддержать на русском и английском:

```text
collect/add/preview/export/create/update/comment/checklist/clear/status
```

Кнопки после `preview`:

- `Добавить источники`
- `Сделать выгрузку`
- `Создать задачу`
- `Обновить задачу`
- `Очистить сессию`

## Сессии

Ключевая сущность - `extraction_session`.

Сессия нужна, потому что пользователь собирает контекст постепенно. Она может
быть привязана к:

- Telegram chat id;
- инициатору;
- корневому сообщению бота;
- optional target task id;
- статусу: `collecting`, `ready`, `exported`, `approved`, `published`,
  `cancelled`.

Одна активная сессия на чат - самый простой MVP. Позже можно добавить named
sessions:

```text
@TaskExtractorBot новая task-extractor-v2
@TaskExtractorBot открыть task-extractor-v2
```

## Источники данных

### Telegram publications

Если сообщение является reply/forward на digest-пост, бот должен извлечь:

- Telegram post URL;
- `telegram_chat_id`;
- `telegram_message_id`;
- Loom URL;
- `loom_video_id`;
- Google Doc URL;
- Transcript Doc URL;
- заголовок встречи;
- tags: `#task_discussion`, `#task_demo`, другие.

Для старых постов можно переиспользовать логику `зарегистрировать` из
`MeetingDigestBot`.

### AIcallorder

По `loom_video_id` бот читает из `AIcallorder`:

- transcript text;
- processed artifacts JSON;
- title;
- meeting type;
- recorded_at;
- source URL;
- links to generated documents.

`AIcallorder` остается источником правды для транскриптов и meeting artifacts.

### Bitrix tasks

Из текстового сообщения бот должен извлекать:

- task id;
- task URL;
- комментарии пользователя к задаче;
- список связанных задач.

Для каждой Bitrix-задачи бот читает:

- title;
- description;
- status;
- responsible/accomplices/auditors;
- comments;
- checklist items;
- existing source markers, если задача уже велась через `MeetingDigestBot`.

### Manual notes

Любое сообщение без ссылок может быть добавлено как ручная вводная:

- пожелания пользователя;
- ограничения;
- бизнес-контекст;
- список вопросов;
- критерии результата;
- указание, что именно надо получить на выходе.

## State DB

Новые таблицы можно добавить в текущую SQLite DB `meeting_digest_bot.db`.

```text
task_extractor_sessions
  id
  chat_id
  root_message_id
  created_by_user_id
  title
  status
  target_task_id
  created_at
  updated_at
  exported_at
  published_at
```

```text
task_extractor_sources
  id
  session_id
  source_type
  source_key
  source_url
  telegram_chat_id
  telegram_message_id
  loom_video_id
  bitrix_task_id
  title
  raw_text
  normalized_json
  status
  created_at
  updated_at
```

`source_type`:

- `telegram_post`;
- `loom`;
- `aicallorder_meeting`;
- `bitrix_task`;
- `task_context_post`;
- `manual_note`;
- `external_url`.

Daily summary, daily reports and weekly operational reports не являются
самостоятельным сценарием `Task Extractor`. Если пользователь вручную вставил
фрагмент такого текста как контекст для оценки нового функционала, бот должен
сохранить его как `manual_note`, но не запускать отдельный daily-процесс и не
создавать задачу про daily summary по умолчанию.

```text
task_extractor_exports
  id
  session_id
  export_type
  output_dir
  zip_path
  manifest_json
  llm_prompt
  status
  created_at
```

```text
task_extractor_publications
  id
  session_id
  bitrix_task_id
  action
  result_json
  created_at
```

## NotebookLM export shape

Каждая сессия экспортируется в папку:

```text
exports/task_extractor/<session_id>/
  00_readme.md
  01_task_context.md
  02_meetings_digest.md
  03_transcripts.md
  04_existing_tasks.md
  05_comments_and_manual_notes.md
  06_decisions_and_requirements.md
  07_open_questions.md
  08_source_links.md
  prompt_for_notebooklm.md
  machine_manifest.json
```

Для Telegram можно прикреплять zip:

```text
task_extractor_<session_id>_notebooklm.zip
```

## Agent handoff for NotebookLM

В следующем этапе планируется отдельный Codex/agent skill, который будет:

1. Заходить в Telegram-группу `Task Extractor`.
2. Находить задачи, созданные через `Task Extractor`.
3. Получать подготовленные файлы для NotebookLM.
4. Создавать отдельный NotebookLM project/notebook под каждый новый функционал.
5. Загружать туда source bundle.
6. Сохранять ссылку на NotebookLM-проект обратно в контекст задачи или в
   handoff registry.

Поэтому Task Extractor должен публиковать и хранить стабильный handoff package,
а не только отвечать человеку в Telegram.

### Handoff package

После команды `выгрузка` или успешной команды `создать` бот должен иметь
готовый пакет:

```text
exports/task_extractor/<session_id>/
  source_bundle/
    00_readme.md
    01_task_context.md
    02_meetings_digest.md
    03_transcripts.md
    04_existing_tasks.md
    05_comments_and_manual_notes.md
    06_decisions_and_requirements.md
    07_open_questions.md
    08_source_links.md
  prompt_workspace/
    prompt_for_notebooklm.md
    generate_functional_spec.md
    generate_estimation_questions.md
    generate_acceptance_criteria.md
  machine_bundle/
    handoff_manifest.json
    source_manifest.json
    bitrix_manifest.json
```

Telegram attachment:

```text
task_extractor_<session_id>__task_<bitrix_task_id>__notebooklm.zip
```

If the task is not published yet:

```text
task_extractor_<session_id>__draft__notebooklm.zip
```

### Handoff manifest

`machine_bundle/handoff_manifest.json` should be the stable contract for the
future skill.

Required fields:

```json
{
  "schema_version": 1,
  "session_id": "uuid-or-local-id",
  "telegram_chat_id": "-100...",
  "telegram_root_message_id": "123",
  "telegram_export_message_id": "124",
  "created_by_user_id": "telegram-user-id",
  "status": "exported|published|notebook_created",
  "title": "short task/project title",
  "bitrix_task_id": 168334,
  "bitrix_task_url": "https://totiscrm.com/workgroups/group/512/tasks/task/view/168334/",
  "notebooklm_project_title": "Task 168334 - short task/project title",
  "notebooklm_project_url": "",
  "zip_path": "exports/task_extractor/...zip",
  "source_bundle_files": [
    "source_bundle/00_readme.md",
    "source_bundle/01_task_context.md"
  ],
  "source_count": 5,
  "loom_video_ids": [],
  "source_task_ids": [],
  "created_at": "2026-05-14T00:00:00+03:00",
  "updated_at": "2026-05-14T00:00:00+03:00"
}
```

The future skill should be able to work only from this manifest and the zip
contents.

### Telegram handoff message

When export or task creation succeeds, the bot should post a concise machine-
and-human-readable message:

```text
Task Extractor export ready
Session: <session_id>
Bitrix task: #168334
NotebookLM title: Task 168334 - <short title>
Package: task_extractor_<session_id>__task_168334__notebooklm.zip
Status: exported
```

The agent skill can search the group for messages containing:

```text
Task Extractor export ready
Status: exported
```

After it creates the NotebookLM project, it should update the registry/status if
the integration path allows it:

```text
Status: notebook_created
NotebookLM: <url>
```

### Idempotency

The future skill must not create duplicate NotebookLM notebooks for the same
Task Extractor task. Task Extractor should support idempotency through:

- `session_id`;
- `bitrix_task_id`;
- deterministic `notebooklm_project_title`;
- handoff status;
- optional `notebooklm_project_url` saved after creation.

If the agent sees an existing manifest with `notebooklm_project_url`, it should
skip creation and only report the existing project.

### Naming

NotebookLM project title:

```text
Task <bitrix_task_id> - <short functional title>
```

For draft exports:

```text
Draft <session_id> - <short functional title>
```

The title should describe the new functionality, not the meeting names.

Detailed browser-agent design is tracked separately:

```text
docs/notebooklm-agent-design.md
```

## Prompt pipeline

Пайплайн должен быть двухступенчатым.

### 1. Source normalizer

Задача: привести разные источники к единому виду.

Выход:

- list of source events;
- decisions;
- requirements;
- user stories / scenarios;
- integration points;
- blockers;
- risks;
- open questions;
- explicit source references.

### 2. Specification drafter

Задача: подготовить рабочий документ для ТЗ.

Выход:

- краткое описание задачи;
- бизнес-цель;
- текущий контекст;
- scope / out of scope;
- функциональные требования;
- технические требования;
- данные и интеграции;
- UX/admin flow, если применимо;
- acceptance criteria;
- checklist for QA/PM;
- риски;
- вопросы на подтверждение;
- список источников.

## Качество и подтверждение

Перед записью в Bitrix бот должен делать quality gate:

- нет ли пустых транскриптов;
- все ли Loom video id найдены в `AIcallorder`;
- все ли Bitrix task URLs распознаны;
- не конфликтуют ли источники между собой;
- достаточно ли данных для создания задачи;
- какие вопросы требуют подтверждения.

Если есть критичные пробелы, `создать` должен вернуть preview с вопросами, а
не публиковать задачу.

## Bitrix publication

Публикация должна переиспользовать существующие правила `MeetingDigestBot`:

- `GROUP_ID=512`;
- `CREATED_BY=114736`;
- `RESPONSIBLE_ID=114736`;
- auditors из текущей конфигурации;
- comments through `task.commentitem.add`;
- checklist groups through `task.checklistitem.add`;
- non-destructive update for existing tasks.

Новые tags:

```text
task-extractor
notebooklm-export
llm-context
```

Для update-режима лучше использовать отдельные markers:

```text
=== TASK_EXTRACTOR_CONTEXT START session_id=<id> ===
...
=== TASK_EXTRACTOR_CONTEXT END session_id=<id> ===
```

## Архитектура модулей

Вариант MVP внутри текущего пакета:

```text
meeting_digest_bot/
  task_extractor_models.py
  task_extractor_state.py
  task_extractor_sources.py
  task_extractor_export.py
  task_extractor_prompts.py
  task_extractor_service.py
```

Из существующих модулей переиспользовать:

- `telegram_bot.py` - command routing;
- `telegram_links.py` - Telegram/Bitrix link parsing;
- `aicallorder_db.py` - чтение Loom artifacts;
- `bitrix_client.py` - чтение и запись задач;
- `task_drafts.py` - часть правил форматирования;
- `state_db.py` - миграции и общая SQLite инфраструктура.

Если контур вырастет, его можно вынести в отдельный сервис, но MVP дешевле
собрать рядом с `MeetingDigestBot`.

## MVP

Минимальная версия:

1. Создание одной активной сессии на Telegram-группу.
2. Добавление источников из reply/forward/text.
3. Распознавание Loom, Telegram post URL, Bitrix task URL/id.
4. Чтение `AIcallorder` по `loom_video_id`.
5. Чтение Bitrix task description/comments/checklists.
6. `preview` с перечнем источников и missing items.
7. `выгрузка` в Markdown + zip для NotebookLM.
8. `создать` новую Bitrix-задачу из draft-документа после подтверждения.

Не входит в MVP:

- daily summary processing;
- daily/weekly operational reporting;
- автоматическое создание задач по невыполненным daily-пунктам.

## Runtime MVP commands

Implemented runtime entrypoints:

```powershell
python -m meeting_digest_bot task-extractor collect --chat-id <chat> --text "<message>"
python -m meeting_digest_bot task-extractor preview --chat-id <chat>
python -m meeting_digest_bot task-extractor export --chat-id <chat>
python -m meeting_digest_bot task-extractor create --chat-id <chat>
python -m meeting_digest_bot task-extractor update --chat-id <chat> --task-id 168334
python -m meeting_digest_bot task-extractor clear --chat-id <chat>
python -m meeting_digest_bot poll-task-extractor
```

Server env:

```text
TASK_EXTRACTOR_BOT_TOKEN=<BotFather token for @TaskExtractorBot>
```

Systemd scaffold:

```text
deploy/linux/systemd/task-extractor-bot-poller.service.example
```

The Task Extractor poller runs in `task_extractor_mode`, so ordinary messages
that look like source material can be collected passively after the bot sees
them in the group. Telegram Bot API still cannot read historical messages from
before the bot was added or before polling/webhook delivery, so the first MVP
collects messages that pass through the bot runtime.

## Следующие этапы

После MVP:

- named sessions;
- inline buttons;
- LLM-классификация источников и команд;
- авто-dedupe одинаковых Loom/задач;
- conflict report;
- режим `обновить task_id`;
- публикация комментариев и checklists отдельно;
- хранение approved spec draft;
- экспорт в Google Docs/Notion;
- связь с knowledge-base объектами.

## Главный продуктовый принцип

`Task Extractor` должен ощущаться как временная рабочая папка в Telegram:
пользователь кидает туда все, что относится к будущей задаче, бот аккуратно
собирает фактуру, показывает что понял, готовит пакет для глубокой LLM-работы
и только после подтверждения пишет в Bitrix.
