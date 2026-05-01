from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from .models import MeetingRecord, WeeklyRollup


@dataclass(slots=True)
class WeeklyLLMConfig:
    enabled: bool
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int = 120

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)


class WeeklyRollupLLM:
    def __init__(self, config: WeeklyLLMConfig) -> None:
        self.config = config

    def enhance(
        self,
        *,
        week_from: date,
        week_to: date,
        base_rollup: WeeklyRollup,
        meetings: list[MeetingRecord],
    ) -> WeeklyRollup | None:
        if not self.config.usable:
            return None

        payload = {
            "week_from": week_from.isoformat(),
            "week_to": week_to.isoformat(),
            "base_rollup": base_rollup.model_dump(mode="json"),
            "meetings": [self._meeting_payload(meeting) for meeting in meetings],
        }
        content = self._chat_completion(payload)
        parsed = self._parse_json_object(content)
        if not parsed:
            return None
        return WeeklyRollup(
            week_from=week_from,
            week_to=week_to,
            source_meeting_ids=base_rollup.source_meeting_ids,
            summary=str(parsed.get("summary") or base_rollup.summary).strip(),
            commitments=self._list_or_fallback(parsed.get("commitments"), base_rollup.commitments),
            blockers=self._list_or_fallback(parsed.get("blockers"), base_rollup.blockers),
            tech_debt=self._list_or_fallback(parsed.get("tech_debt"), base_rollup.tech_debt),
            business_requests=self._list_or_fallback(
                parsed.get("business_requests"),
                base_rollup.business_requests,
            ),
        )

    def _chat_completion(self, payload: dict[str, Any]) -> str:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.config.model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты помогаешь вести проектные итоги по Loom-встречам. "
                        "Верни только JSON object без markdown. "
                        "Поля: summary string, commitments string[], blockers string[], "
                        "tech_debt string[], business_requests string[]. "
                        "Commitments должны быть конкретными обязательствами недели, а не шумом."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.config.timeout_seconds,
        )
        if response.status_code >= 400 and "response_format" in body:
            body.pop("response_format", None)
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.config.timeout_seconds,
            )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    @staticmethod
    def _meeting_payload(meeting: MeetingRecord) -> dict[str, Any]:
        artifacts = meeting.artifacts or {}
        return {
            "loom_video_id": meeting.loom_video_id,
            "title": meeting.title,
            "source_url": meeting.source_url,
            "recorded_at": meeting.recorded_at,
            "summary": artifacts.get("summary"),
            "decisions": artifacts.get("decisions"),
            "action_items": artifacts.get("action_items"),
            "blockers": artifacts.get("blockers"),
            "tech_debt": artifacts.get("remaining_tech_debt"),
            "business_requests": artifacts.get("business_requests_for_estimation"),
        }

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any] | None:
        text = content.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _list_or_fallback(value: object, fallback: list[str]) -> list[str]:
        if not isinstance(value, list):
            return fallback
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned or fallback
