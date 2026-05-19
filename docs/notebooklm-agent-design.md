# NotebookLM Agent Design

## Назначение

`NotebookLM Agent` - отдельный браузерный агент/skill, который берет
результат `Task Extractor` и создает в NotebookLM отдельный notebook/project для
проработки новой функциональности строго по подготовленным источникам.

Task Extractor отвечает за сбор, нормализацию и упаковку источников. NotebookLM
Agent отвечает только за браузерную операцию:

- найти готовый Task Extractor export;
- открыть NotebookLM под нужным Google-аккаунтом;
- создать отдельный notebook;
- загрузить туда source bundle;
- проверить, что источники добавлены;
- сохранить ссылку на notebook обратно в handoff registry/Telegram/Bitrix.

## Принцип разделения

Task Extractor не должен зависеть от браузерной автоматизации NotebookLM.
Он только создает стабильный пакет:

```text
task_extractor_<session_id>__task_<bitrix_task_id>__notebooklm.zip
```

и manifest:

```text
machine_bundle/handoff_manifest.json
```

NotebookLM Agent использует этот package как единственный источник правды.

## Входные данные агента

Минимальный input:

```json
{
  "session_id": "20260514123456-abcd1234",
  "bitrix_task_id": 168334,
  "zip_path": "/opt/meeting-digest-bot/exports/task_extractor/task_extractor_...zip",
  "handoff_manifest_path": "/opt/meeting-digest-bot/exports/task_extractor/<session_id>/machine_bundle/handoff_manifest.json"
}
```

Агент может получать input тремя способами:

1. Из Telegram-группы `Task Extractor` по handoff-сообщению:

   ```text
   Task Extractor export ready
   Session: <session_id>
   Bitrix task: #168334
   Package: task_extractor_<session_id>__task_168334__notebooklm.zip
   Status: exported
   ```

2. Из локального registry в SQLite:

   ```text
   task_extractor_exports
   task_extractor_publications
   ```

3. Из прямой команды:

   ```powershell
   codex notebooklm-create --session-id <session_id>
   ```

## Источники для загрузки

NotebookLM должен получать только `source_bundle/*.md`.

Загружать:

```text
source_bundle/00_readme.md
source_bundle/01_task_context.md
source_bundle/02_meetings_digest.md
source_bundle/03_transcripts.md
source_bundle/04_existing_tasks.md
source_bundle/05_comments_and_manual_notes.md
source_bundle/06_decisions_and_requirements.md
source_bundle/07_open_questions.md
source_bundle/08_source_links.md
```

Не загружать как источники:

```text
prompt_workspace/*.md
machine_bundle/*.json
```

`prompt_workspace/prompt_for_notebooklm.md` используется как первый prompt после
создания notebook, но не как source.

## Название NotebookLM project

Название должно быть детерминированным:

```text
Task <bitrix_task_id> - <short functional title>
```

Для draft:

```text
Draft <session_id> - <short functional title>
```

Название должно описывать функциональность, а не список встреч.

## Idempotency

Агент не должен создавать дубликаты.

Перед созданием notebook:

1. Прочитать `handoff_manifest.json`.
2. Если `notebooklm_project_url` уже заполнен - не создавать новый notebook.
3. Если registry содержит `session_id` со статусом `notebook_created` - не
   создавать новый notebook.
4. Если в NotebookLM уже есть notebook с таким deterministic title - открыть
   его и проверить источники.
5. Если notebook существует, но источники неполные - дозагрузить недостающие
   файлы.

После успешного создания:

```json
{
  "status": "notebook_created",
  "notebooklm_project_url": "https://notebooklm.google.com/..."
}
```

## Browser workflow

### 1. Подготовка локального workspace

1. Скачать или открыть zip package.
2. Распаковать во временную директорию.
3. Проверить наличие:

   - `machine_bundle/handoff_manifest.json`;
   - всех файлов из `source_bundle_files`;
   - `prompt_workspace/prompt_for_notebooklm.md`.

4. Посчитать checksum каждого source-файла.
5. Создать локальный agent run log.

### 2. Открытие NotebookLM

1. Открыть браузер с постоянным профилем, где пользователь уже залогинен в
   нужный Google-аккаунт.
2. Перейти на:

   ```text
   https://notebooklm.google.com/
   ```

3. Проверить, что пользователь авторизован.
4. Если требуется login/2FA - остановиться и попросить ручное действие.

### 3. Поиск существующего notebook

1. Найти notebook по deterministic title.
2. Если найден:

   - открыть его;
   - сравнить список источников;
   - дозагрузить недостающие source files;
   - перейти к verification.

3. Если не найден - создать новый notebook.

### 4. Создание notebook

1. Нажать create/new notebook.
2. Установить title.
3. Загрузить `source_bundle/*.md`.
4. Дождаться завершения индексации/processing.
5. Если какой-то файл не принят NotebookLM, сохранить ошибку и продолжить с
   остальными, но отметить run как `partial`.

### 5. Первичный prompt

После загрузки источников агент отправляет prompt:

```text
Use the uploaded Task Extractor sources only.
Prepare a functional specification for this feature.
Respect manual notes over AI summaries.
Call out conflicts and open questions.
```

Фактический prompt берется из:

```text
prompt_workspace/prompt_for_notebooklm.md
```

Ответ NotebookLM не должен автоматически публиковаться в Bitrix как финальное
ТЗ. Он может быть сохранен как draft artifact для ручной проверки.

### 6. Verification

Агент должен проверить:

- notebook открыт;
- title совпадает с manifest;
- количество sources в NotebookLM совпадает с ожидаемым или явно отмечено как
  partial;
- названия загруженных файлов совпадают с `source_bundle_files`;
- NotebookLM project URL получен;
- первый prompt отправлен или сохранена причина, почему не отправлен.

## Output агента

После успешного run:

```json
{
  "session_id": "...",
  "bitrix_task_id": 168334,
  "notebooklm_project_title": "Task 168334 - ...",
  "notebooklm_project_url": "https://notebooklm.google.com/...",
  "uploaded_sources": [
    "00_readme.md",
    "01_task_context.md"
  ],
  "status": "notebook_created",
  "notes": []
}
```

При partial:

```json
{
  "status": "partial",
  "uploaded_sources": [],
  "failed_sources": [
    {
      "file": "03_transcripts.md",
      "reason": "NotebookLM rejected file"
    }
  ]
}
```

## Куда сохранять результат

Минимально:

- обновить локальный `handoff_manifest.json`;
- записать run result рядом:

  ```text
  machine_bundle/notebooklm_run.json
  ```

Желательно:

- добавить запись в SQLite registry;
- отправить Telegram reply в группу `Task Extractor`;
- добавить комментарий в Bitrix task:

  ```text
  NotebookLM project created:
  <url>
  Sources uploaded: 8/8
  Task Extractor session: <session_id>
  ```

## Skill design

Codex skill name:

```text
notebooklm-task-workspace
```

Skill trigger examples:

```text
создай NotebookLM по Task Extractor session <session_id>
обработай новые Task Extractor выгрузки
заведи блокнот NotebookLM для задачи 168334
```

Skill responsibilities:

- locate handoff package;
- open browser with persistent profile;
- drive NotebookLM UI;
- upload exact sources;
- verify result;
- update registry/comment.

Skill should not:

- re-summarize source content before uploading;
- choose extra files outside `source_bundle`;
- create Bitrix tasks;
- mutate Task Extractor session source data;
- overwrite an existing NotebookLM notebook without checking idempotency.

## Runtime modes

### Manual single-run

```powershell
codex notebooklm-create --session-id <session_id>
```

### Batch watcher

The agent scans `task_extractor_exports` for:

```text
status=exported
notebooklm_project_url empty
```

Then processes one package at a time.

### Telegram-triggered

User replies to handoff message:

```text
@NotebookLMAgent создать блокнот
```

The agent extracts session/package from the replied message and runs the same
workflow.

## Failure handling

Stop and ask for manual intervention if:

- Google login is required;
- NotebookLM UI changed and create/upload controls cannot be found;
- source package is missing;
- manifest is invalid;
- files are too large for NotebookLM;
- Bitrix task was deleted;
- browser profile is not available.

Retry automatically if:

- page load timeout;
- upload button temporarily unavailable;
- source processing is still pending.

## Security

- Do not store Google credentials in the repo.
- Use a dedicated browser profile for the authorized Google account.
- Do not print BotFather tokens or cookies in logs.
- Do not upload `machine_bundle/*.json` if it contains internal metadata that
  should not become NotebookLM source.

## First MVP

1. Manual command by `session_id`.
2. Local package discovery from `/opt/meeting-digest-bot/exports/task_extractor`.
3. Browser opens NotebookLM with a persistent authenticated profile.
4. Creates notebook by deterministic title.
5. Uploads `source_bundle/*.md`.
6. Sends first prompt from `prompt_workspace/prompt_for_notebooklm.md`.
7. Writes `machine_bundle/notebooklm_run.json`.
8. Posts result to Telegram or prints a summary for manual copy.

Implemented first local entrypoints:

```powershell
python -m meeting_digest_bot notebooklm-agent open-auth
python -m meeting_digest_bot notebooklm-agent prepare --session-id <session_id>
python -m meeting_digest_bot notebooklm-agent create --session-id <session_id>
python -m meeting_digest_bot notebooklm-agent watch --remote-host 173.242.60.148
```

`open-auth` opens NotebookLM in a persistent browser profile:

```text
data/notebooklm-browser-profile
```

Use it once to authorize the Google account manually. The profile is local
runtime state and is not committed.

For auto-run after Task Extractor export, run the watcher locally on the machine
with the authorized NotebookLM browser profile:

```powershell
$env:TASK_EXTRACTOR_REMOTE_HOST="173.242.60.148"
$env:TASK_EXTRACTOR_REMOTE_USER="root"
$env:TASK_EXTRACTOR_REMOTE_PASSWORD="<ssh password>"
python -m meeting_digest_bot notebooklm-agent watch --interval 60 --limit 1
```

Lifecycle:

```text
TaskExtractorBot собрать -> user adds sources -> TaskExtractorBot выгрузка
  -> VPS export package appears
  -> local notebooklm-agent watch downloads it
  -> creates NotebookLM notebook
  -> uploads source_bundle/*.md
  -> sends prompt_for_notebooklm.md
  -> writes notebooklm_project_url back to the VPS manifest
```

## Server Runtime

Best server-side runtime uses a dedicated Linux user, Xvfb, x11vnc for one-time
authorization, and a systemd watcher.

Install runtime:

```bash
cd /opt/meeting-digest-bot
NOTEBOOKLM_AGENT_VNC_PASSWORD="<temporary strong password>" \
  bash deploy/linux/install_notebooklm_agent_runtime.sh
```

Install services:

```bash
cp deploy/linux/systemd/notebooklm-agent-xvfb.service.example /etc/systemd/system/notebooklm-agent-xvfb.service
cp deploy/linux/systemd/notebooklm-agent-vnc.service.example /etc/systemd/system/notebooklm-agent-vnc.service
cp deploy/linux/systemd/notebooklm-agent-watch.service.example /etc/systemd/system/notebooklm-agent-watch.service
systemctl daemon-reload
systemctl enable --now notebooklm-agent-xvfb.service
systemctl enable --now notebooklm-agent-vnc.service
```

Authorize Google/NotebookLM once through an SSH tunnel:

```powershell
ssh -L 5905:127.0.0.1:5905 root@173.242.60.148
```

Then open a VNC client to:

```text
127.0.0.1:5905
```

Inside the VNC session run:

```bash
cd /opt/meeting-digest-bot
DISPLAY=:98 .venv/bin/python -m meeting_digest_bot notebooklm-agent open-auth \
  --profile-dir /opt/meeting-digest-bot/data/notebooklm-browser-profile
```

After login is complete:

```bash
systemctl enable --now notebooklm-agent-watch.service
```

Operational notes:

- Run the browser as `notebooklm-agent`, not as `root`.
- Keep VNC bound to localhost and access it only through SSH tunnel.
- Use a dedicated Google account for NotebookLM automation.
- Stop `notebooklm-agent-vnc.service` after authorization if you do not need
  live debugging:

  ```bash
  systemctl disable --now notebooklm-agent-vnc.service
  ```

## Later

- Batch watcher for new exports.
- Telegram command integration.
- Bitrix comment update.
- Screenshot-based verification.
- Source checksum diff and partial re-upload.
- NotebookLM project URL registry in SQLite.
