from __future__ import annotations

import re
from dataclasses import dataclass


POST_URL_PATTERN = re.compile(
    r"(https?://t\.me/(?:(?:c/(?P<internal_chat>\d+)/(?P<internal_id>\d+))|(?:(?P<channel>[A-Za-z0-9_]+)/(?P<public_id>\d+))))",
    re.IGNORECASE,
)
TASK_URL_PATTERN = re.compile(r"/tasks/task/view/(?P<task_id>\d+)/?", re.IGNORECASE)


@dataclass(slots=True)
class TelegramPostLink:
    raw_url: str
    channel_slug: str | None
    message_id: int


def extract_post_link(text: str) -> TelegramPostLink | None:
    if not text:
        return None
    match = POST_URL_PATTERN.search(text)
    if not match:
        return None
    raw_url = match.group(1)
    channel_slug = match.group("channel")
    message_id = int(match.group("public_id") or match.group("internal_id"))
    return TelegramPostLink(raw_url=raw_url, channel_slug=channel_slug, message_id=message_id)


def extract_task_id(text: str) -> int | None:
    if not text:
        return None
    match = TASK_URL_PATTERN.search(text)
    if match:
        return int(match.group("task_id"))
    plain = re.search(r"(?<![-/\d])\b(\d{3,})\b(?![-/\d])", text)
    if plain:
        return int(plain.group(1))
    return None
