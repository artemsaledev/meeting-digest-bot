from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BITRIX_ACTOR_USER_ID = 114736
DEFAULT_BITRIX_AUDITOR_IDS = [50760, 127124, 137230, 51977]
DEFAULT_DAILY_PLAN_ACCOMPLICE_IDS = [
    51977,
    58194,
    127124,
    114736,
    137230,
    50760,
    123170,
    120601,
    426,
    162783,
    163323,
]


def _load_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _default_aicallorder_db() -> Path:
    return Path(r"C:\Users\artem\Downloads\dev-scripts\4. Loom\data\loom_automation.db")


def _default_state_db() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "meeting_digest_bot.db"


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def _parse_int_list(value: str | None, default: list[int] | None = None) -> list[int]:
    if value is None or not value.strip():
        return list(default or [])
    result: list[int] = []
    for chunk in value.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            result.append(int(chunk))
    return result


def _parse_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    return float(value)


def _normalize_webhook_base(raw: str | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    value = raw.strip()
    json_suffix = value.endswith(".json")
    match = re.match(r"^(https?://.+?/rest(?:/api)?/\d+/[^/]+/)", value)
    if match:
        return match.group(1), json_suffix
    return (value if value.endswith("/") else value + "/"), json_suffix


@dataclass(slots=True)
class Settings:
    app_host: str
    app_port: int
    aicallorder_db_path: Path
    state_db_path: Path
    bitrix_webhook_base: str
    bitrix_webhook_json_suffix: bool
    bitrix_group_id: int
    bitrix_actor_user_id: int | None
    bitrix_default_responsible_id: int | None
    bitrix_created_by_id: int | None
    bitrix_default_auditor_ids: list[int]
    bitrix_daily_plan_accomplice_ids: list[int]
    bitrix_tags: list[str]
    telegram_bot_token: str | None
    telegram_webhook_secret: str | None
    telegram_channel_username: str | None
    telegram_report_chat_id: str | None
    knowledge_alert_chat_id: str | None
    meeting_digest_shared_secret: str | None
    matching_task_limit: int
    matching_score_threshold: float
    weekly_llm_enabled: bool
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: int
    debug: bool

    @property
    def bitrix_modern_webhook_base(self) -> str:
        if not self.bitrix_webhook_base:
            return ""
        if "/rest/api/" in self.bitrix_webhook_base:
            return self.bitrix_webhook_base
        return self.bitrix_webhook_base.replace("/rest/", "/rest/api/", 1)

    @classmethod
    def from_env(cls) -> "Settings":
        dotenv_values = _load_dotenv_file(Path.cwd() / ".env")

        def get_value(key: str, default: str | None = None) -> str | None:
            if key in os.environ and os.environ[key] != "":
                return os.environ[key]
            if key in dotenv_values and dotenv_values[key] != "":
                return dotenv_values[key]
            return default

        webhook_base, json_suffix = _normalize_webhook_base(get_value("BITRIX_WEBHOOK_BASE"))
        tags = [
            item.strip()
            for item in str(get_value("BITRIX_TAGS", "meeting-digest,loom-digest")).split(",")
            if item.strip()
        ]
        settings = cls(
            app_host=str(get_value("MEETING_DIGEST_BOT_HOST", "127.0.0.1")),
            app_port=int(str(get_value("MEETING_DIGEST_BOT_PORT", "8011"))),
            aicallorder_db_path=Path(str(get_value("AICALLORDER_DB_PATH", str(_default_aicallorder_db())))),
            state_db_path=Path(str(get_value("MEETING_DIGEST_STATE_DB_PATH", str(_default_state_db())))),
            bitrix_webhook_base=webhook_base,
            bitrix_webhook_json_suffix=json_suffix,
            bitrix_group_id=int(str(get_value("BITRIX_GROUP_ID", "512"))),
            bitrix_actor_user_id=_parse_int(get_value("BITRIX_ACTOR_USER_ID", str(DEFAULT_BITRIX_ACTOR_USER_ID))),
            bitrix_default_responsible_id=_parse_int(
                get_value("BITRIX_DEFAULT_RESPONSIBLE_ID", str(DEFAULT_BITRIX_ACTOR_USER_ID))
            ),
            bitrix_created_by_id=_parse_int(get_value("BITRIX_CREATED_BY_ID", str(DEFAULT_BITRIX_ACTOR_USER_ID))),
            bitrix_default_auditor_ids=_parse_int_list(
                get_value("BITRIX_DEFAULT_AUDITOR_IDS"),
                DEFAULT_BITRIX_AUDITOR_IDS,
            ),
            bitrix_daily_plan_accomplice_ids=_parse_int_list(
                get_value("BITRIX_DAILY_PLAN_ACCOMPLICE_IDS"),
                DEFAULT_DAILY_PLAN_ACCOMPLICE_IDS,
            ),
            bitrix_tags=tags,
            telegram_bot_token=get_value("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=get_value("TELEGRAM_WEBHOOK_SECRET"),
            telegram_channel_username=get_value("TELEGRAM_CHANNEL_USERNAME"),
            telegram_report_chat_id=get_value("TELEGRAM_REPORT_CHAT_ID"),
            knowledge_alert_chat_id=get_value("KNOWLEDGE_ALERT_CHAT_ID") or get_value("TELEGRAM_REPORT_CHAT_ID"),
            meeting_digest_shared_secret=get_value("MEETING_DIGEST_SHARED_SECRET"),
            matching_task_limit=int(str(get_value("MEETING_DIGEST_MATCHING_TASK_LIMIT", "50"))),
            matching_score_threshold=_parse_float(get_value("MEETING_DIGEST_MATCHING_SCORE_THRESHOLD"), 0.42),
            weekly_llm_enabled=_parse_bool(get_value("MEETING_DIGEST_WEEKLY_LLM_ENABLED"), False),
            llm_api_key=get_value("LLM_API_KEY") or get_value("OPENAI_API_KEY"),
            llm_base_url=str(get_value("LLM_BASE_URL") or get_value("OPENAI_BASE_URL") or "https://api.openai.com/v1"),
            llm_model=str(get_value("LLM_MODEL") or get_value("OPENAI_MODEL") or "gpt-4.1-mini"),
            llm_timeout_seconds=int(str(get_value("LLM_TIMEOUT_SECONDS", "120"))),
            debug=_parse_bool(get_value("MEETING_DIGEST_BOT_DEBUG"), False),
        )
        settings.state_db_path.parent.mkdir(parents=True, exist_ok=True)
        return settings
