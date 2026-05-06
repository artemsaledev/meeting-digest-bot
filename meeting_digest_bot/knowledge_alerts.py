from __future__ import annotations

from typing import Any
from pathlib import Path

import requests

from .config import Settings


def send_knowledge_alert(settings: Settings, text: str) -> dict[str, Any]:
    if not settings.telegram_bot_token:
        return {"sent": False, "reason": "telegram_bot_token_missing"}
    chat_id = settings.knowledge_alert_chat_id or read_knowledge_alert_chat_id()
    if not chat_id:
        return {"sent": False, "reason": "knowledge_alert_chat_id_missing"}
    response = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    return {"sent": True, "chat_id": str(chat_id)}


def knowledge_alert_chat_id_path() -> Path:
    return Path.cwd() / "data" / "knowledge_alert_chat_id.txt"


def read_knowledge_alert_chat_id() -> str:
    path = knowledge_alert_chat_id_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_knowledge_alert_chat_id(chat_id: int | str) -> str:
    path = knowledge_alert_chat_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = str(chat_id).strip()
    path.write_text(value + "\n", encoding="utf-8")
    return value


def format_notion_import_alert(result: Any) -> str:
    proposals = [item for item in result.planned_pages if item.get("action") == "propose_revision"]
    lines = [
        "Knowledge Base: найдены ручные правки в Notion",
        f"Proposals: {len(proposals)}",
        f"Scanned pages: {result.scanned_pages}",
        "",
    ]
    for item in proposals[:10]:
        lines.extend(
            [
                f"- {item.get('object_id')} ({item.get('database')})",
                f"  {item.get('proposal_path')}",
            ]
        )
    if len(proposals) > 10:
        lines.append(f"...и еще {len(proposals) - 10}")
    return "\n".join(lines).strip()
