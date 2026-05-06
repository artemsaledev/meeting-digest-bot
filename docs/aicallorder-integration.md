# AIcallorder Integration

`AIcallorder` is responsible for:

- receiving Loom recordings
- producing transcript text
- producing processed meeting artifacts
- publishing digest posts to Telegram
- registering those Telegram publications in `MeetingDigestBot`

`MeetingDigestBot` is responsible for:

- reading AIcallorder artifacts by `loom_video_id`
- mapping digest posts to CRM actions
- publishing tasks, comments, and checklists to Bitrix

## Registration Endpoint

```http
POST http://127.0.0.1:8011/publications/register
```

The request should include the shared secret configured in both services.

Payload shape:

```json
{
  "post_url": "https://t.me/c/<chat>/<message>",
  "telegram_chat_id": "-100...",
  "telegram_message_id": "123",
  "digest_type": "meeting",
  "loom_video_id": "b1ef1d39ef8b46b181540a27fb8f265a",
  "meeting_title": "#task_discussion 01.05 ...",
  "source_url": "https://www.loom.com/share/...",
  "google_doc_url": "https://docs.google.com/document/d/.../edit",
  "transcript_doc_url": "https://docs.google.com/document/d/.../edit",
  "source_tags": ["#task_discussion"],
  "payload": {
    "source_tags": ["#task_discussion"],
    "doc_section_title": "Meeting Note: ...",
    "transcript_section_title": "Transcript: ..."
  }
}
```

`source_tags` is the stable contract for the knowledge-base intake contour.
Large task flows should use `#task_discussion` or `#task_demo`; `#daily`
remains an operational planning tag and is excluded from knowledge exports.

## Old Posts

Old Telegram posts can be registered from the group by replying:

```text
@LLMeets_bot зарегистрировать
```

This calls the same internal registration flow and stores the publication in the state DB.

## Data Source

MeetingDigestBot reads the AIcallorder SQLite database:

```text
/opt/AIcallorder/data/loom_automation.db
```

The relevant table must contain:

- `loom_video_id`
- `source_url`
- `title`
- `meeting_type`
- `recorded_at`
- `transcript_text`
- `artifacts_json`
