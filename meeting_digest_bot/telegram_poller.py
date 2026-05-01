from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .telegram_bot import TelegramBotFacade


@dataclass(slots=True)
class TelegramPollingWorker:
    bot: TelegramBotFacade
    poll_timeout_seconds: int = 30
    idle_sleep_seconds: float = 0.5

    @property
    def api_url(self) -> str:
        return self.bot.api_url

    def run(self, *, once: bool = False, start_offset: int | None = None, limit: int = 20) -> dict[str, Any]:
        offset = start_offset
        processed = 0
        failures = 0
        last_update_id = None

        while True:
            updates = self._get_updates(offset=offset, limit=limit)
            if not updates:
                if once:
                    break
                time.sleep(self.idle_sleep_seconds)
                continue

            for update in updates:
                update_id = int(update["update_id"])
                last_update_id = update_id
                offset = update_id + 1
                try:
                    self.bot.process_update(update)
                    processed += 1
                except Exception as exc:
                    failures += 1
                    self._reply_with_error(update, exc)

            if once:
                break

        return {
            "processed": processed,
            "failures": failures,
            "last_update_id": last_update_id,
            "next_offset": offset,
        }

    def drop_pending_updates(self) -> dict[str, Any]:
        response = requests.post(
            self.api_url + "deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _get_updates(self, *, offset: int | None, limit: int) -> list[dict[str, Any]]:
        response = requests.post(
            self.api_url + "getUpdates",
            json={
                "offset": offset,
                "timeout": self.poll_timeout_seconds,
                "limit": limit,
                "allowed_updates": ["message", "channel_post"],
            },
            timeout=self.poll_timeout_seconds + 10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        return list(payload.get("result") or [])

    def _reply_with_error(self, update: dict[str, Any], exc: Exception) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return
        text = f"Не удалось обработать команду: {exc}"
        try:
            self.bot.send_message(chat_id, text[:4000])
        except Exception:
            pass
