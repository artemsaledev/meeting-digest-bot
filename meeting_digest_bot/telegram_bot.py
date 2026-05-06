from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

import requests

from .knowledge_alerts import write_knowledge_alert_chat_id
from .models import (
    DailyPlanSyncRequest,
    DailyReportRequest,
    DigestType,
    DaySyncRequest,
    PostSyncRequest,
    PublicationRegistrationRequest,
    SyncAction,
    TelegramCommand,
    TelegramResponse,
    WeekSyncRequest,
    WeeklyReportRequest,
)
from .service import MeetingDigestService
from .telegram_links import extract_post_link, extract_task_id


BOT_MENTION_RE = re.compile(r"@LLMeets_bot\b", re.IGNORECASE)
DAY_COMMAND_RE = re.compile(r"/day(?:@[A-Za-z0-9_]+)?\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
PLAN_COMMAND_RE = re.compile(
    r"(?:/plan(?:@[A-Za-z0-9_]+)?|план|daily[-_\s]?plan)\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
WEEK_COMMAND_RE = re.compile(
    r"/week(?:@[A-Za-z0-9_]+)?\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
REPORT_COMMAND_RE = re.compile(
    r"(?:/report(?:@[A-Za-z0-9_]+)?|итоги|результаты)\s*(вчера|\d{4}-\d{2}-\d{2})?",
    re.IGNORECASE,
)
WEEKLY_REPORT_COMMAND_RE = re.compile(
    r"(?:/weekly_report(?:@[A-Za-z0-9_]+)?|итоги\s+недели|результаты\s+недели)\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
LOOM_URL_RE = re.compile(r"https?://(?:www\.)?loom\.com/share/([A-Za-z0-9]+)[^\s]*", re.IGNORECASE)
GOOGLE_DOC_RE = re.compile(r"https?://docs\.google\.com/document/d/[^\s)]+", re.IGNORECASE)


@dataclass(slots=True)
class TelegramBotFacade:
    service: MeetingDigestService
    token: str

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/"

    def process_update(self, update: dict) -> TelegramResponse:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = self._normalize_text(message.get("text") or message.get("caption") or "")
        if not text:
            return TelegramResponse(
                ok=False,
                text="Пришлите ссылку на пост Telegram и, при необходимости, номер задачи.",
            )

        if self._is_help_command(text):
            response = TelegramResponse(ok=True, text=self._help_text())
            if chat_id:
                self.send_message(chat_id, response.text)
            return response

        if self._is_knowledge_alert_here_command(text):
            saved = write_knowledge_alert_chat_id(chat_id)
            response = TelegramResponse(
                ok=True,
                text=f"Knowledge Base alerts enabled for this chat: {saved}",
                payload={"knowledge_alert_chat_id": saved},
            )
            if chat_id:
                self.send_message(chat_id, response.text)
            return response

        if self._is_register_command(text):
            response = self._register_publication_from_reply(message)
            if chat_id:
                self.send_message(chat_id, response.text)
            return response

        report_response = self._process_report_command(text)
        if report_response:
            if chat_id:
                self.send_message(chat_id, report_response.text)
            return report_response

        command = self._parse_command(text, message=message)
        action = SyncAction.preview if command.action == SyncAction.auto else command.action
        if command.daily_plan_date:
            result = self.service.sync_daily_plan(
                DailyPlanSyncRequest(
                    report_date=command.daily_plan_date,
                    action=action,
                    task_id=command.task_id,
                    team_name=command.team_name or "Bitrix Develop Team",
                )
            )
            response = TelegramResponse(
                ok=True,
                text=self._format_sync_result("плана дня", result),
                payload=result.model_dump(),
            )
        elif command.report_date:
            result = self.service.sync_day(
                DaySyncRequest(
                    report_date=command.report_date,
                    action=action,
                    task_id=command.task_id,
                )
            )
            response = TelegramResponse(
                ok=True,
                text=self._format_sync_result("дня", result),
                payload=result.model_dump(),
            )
        elif command.week_from and command.week_to:
            result = self.service.sync_week(
                WeekSyncRequest(
                    week_from=command.week_from,
                    week_to=command.week_to,
                    action=action,
                    task_id=command.task_id,
                )
            )
            response = TelegramResponse(
                ok=True,
                text=self._format_sync_result("недели", result),
                payload=result.model_dump(),
            )
        elif command.post_url:
            result = self.service.sync_post(
                PostSyncRequest(
                    post_url=command.post_url,
                    action=action,
                    task_id=command.task_id,
                )
            )
            response = TelegramResponse(
                ok=True,
                text=self._format_sync_result("поста", result),
                payload=result.model_dump(),
            )
        else:
            response = TelegramResponse(
                ok=False,
                text="Не удалось распознать ссылку на пост или диапазон недели.",
            )

        if chat_id:
            self.send_message(chat_id, response.text)
        return response

    def _process_report_command(self, text: str) -> TelegramResponse | None:
        if not BOT_MENTION_RE.search(text) and not text.strip().startswith(("/report", "/weekly_report")):
            return None
        command_text = self._strip_bot_mention(text)
        weekly_match = WEEKLY_REPORT_COMMAND_RE.search(command_text)
        if weekly_match:
            result = self.service.run_weekly_report(
                WeeklyReportRequest(
                    week_from=date.fromisoformat(weekly_match.group(1)),
                    week_to=date.fromisoformat(weekly_match.group(2)),
                    force=True,
                    send_telegram=False,
                )
            )
            return TelegramResponse(
                ok=True,
                text=str((result.details or {}).get("telegram_text") or self._format_sync_result("итогов недели", result)),
                payload=result.model_dump(),
            )

        match = REPORT_COMMAND_RE.search(command_text)
        if not match:
            return None
        raw_date = (match.group(1) or "").strip().lower()
        if raw_date == "вчера" or not raw_date:
            report_date = datetime.now(ZoneInfo("Europe/Kyiv")).date() - timedelta(days=1)
        else:
            report_date = date.fromisoformat(raw_date)
        result = self.service.run_daily_report(
            DailyReportRequest(
                report_date=report_date,
                force=True,
                send_telegram=False,
            )
        )
        return TelegramResponse(
            ok=True,
            text=str((result.details or {}).get("telegram_text") or self._format_sync_result("итогов плана дня", result)),
            payload=result.model_dump(),
        )

    def send_message(self, chat_id: int | str, text: str) -> dict:
        response = requests.post(
            self.api_url + "sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _parse_command(self, text: str, message: dict | None = None) -> TelegramCommand:
        command_text = self._strip_bot_mention(text)
        lowered = command_text.lower()
        action = SyncAction.auto
        normalized = f" {lowered} "
        if any(marker in normalized for marker in [" preview ", " предпросмотр ", " показать ", " проверить "]):
            action = SyncAction.preview
        elif any(marker in normalized for marker in [" weekly ", " week ", " неделя ", " неделю "]):
            action = SyncAction.append_to_weekly
        elif any(marker in normalized for marker in [" new ", " create ", " создать ", " новая ", " новую "]):
            action = SyncAction.create
        elif any(marker in normalized for marker in [" checklist ", " чеклист ", " чек-лист "]):
            action = SyncAction.append_checklists
        elif any(marker in normalized for marker in [" comment ", " комментарий ", " коммент "]):
            action = SyncAction.append_comment
        elif any(marker in normalized for marker in [" replace ", " update ", " обновить ", " заменить "]):
            action = SyncAction.update_description

        post_link = extract_post_link(text)
        reply_text_post_link = self._post_link_from_reply_text(message) if not post_link else None
        reply_post_url = self._post_url_from_reply(message) if not post_link and not reply_text_post_link else None
        task_id = extract_task_id(lowered)
        plan_match = PLAN_COMMAND_RE.search(lowered)
        day_match = DAY_COMMAND_RE.search(lowered)
        week_match = WEEK_COMMAND_RE.search(lowered)
        return TelegramCommand(
            post_url=post_link.raw_url if post_link else reply_text_post_link or reply_post_url,
            task_id=task_id,
            action=action,
            daily_plan_date=date.fromisoformat(plan_match.group(1)) if plan_match else None,
            report_date=date.fromisoformat(day_match.group(1)) if day_match else None,
            week_from=date.fromisoformat(week_match.group(1)) if week_match else None,
            week_to=date.fromisoformat(week_match.group(2)) if week_match else None,
            team_name=self._extract_team_name(command_text),
        )

    def _register_publication_from_reply(self, message: dict) -> TelegramResponse:
        reply = message.get("reply_to_message") or {}
        reply_text = self._normalize_text(reply.get("text") or reply.get("caption") or "")
        if not reply_text:
            return TelegramResponse(
                ok=False,
                text=(
                    "Для регистрации старого поста ответьте командой "
                    "`@LLMeets_bot зарегистрировать` именно на сообщение с Loom-дайджестом."
                ),
            )

        post_url = self._post_url_from_reply(message)
        metadata = self._extract_publication_metadata(reply_text)
        if not post_url:
            return TelegramResponse(ok=False, text="Не удалось определить ссылку на пост из reply_to_message.")
        if not metadata.get("loom_video_id"):
            return TelegramResponse(
                ok=False,
                text=(
                    "Не нашел Loom-ссылку или Loom video ID в тексте старого поста. "
                    "Ответьте командой на сам digest-пост, где есть строка Loom."
                ),
            )

        record = self.service.register_publication(
            PublicationRegistrationRequest(
                post_url=post_url,
                telegram_chat_id=str((message.get("chat") or {}).get("id") or ""),
                telegram_message_id=str(reply.get("message_id") or ""),
                digest_type=DigestType.meeting,
                loom_video_id=metadata.get("loom_video_id"),
                meeting_title=metadata.get("meeting_title"),
                source_url=metadata.get("source_url"),
                google_doc_url=metadata.get("google_doc_url"),
                transcript_doc_url=metadata.get("transcript_doc_url"),
                source_tags=list(metadata.get("source_tags") or []),
                payload={
                    "registered_from": "telegram_reply_command",
                    "source_tags": list(metadata.get("source_tags") or []),
                    "doc_section_title": metadata.get("doc_section_title"),
                    "transcript_section_title": metadata.get("transcript_section_title"),
                },
            )
        )
        return TelegramResponse(
            ok=True,
            text=(
                "Старый пост зарегистрирован.\n"
                f"Источник: {record.post_url}\n"
                f"Loom video ID: {record.loom_video_id}\n"
                f"Заголовок: {record.meeting_title or '-'}\n\n"
                "Теперь можно ответить на этот же пост или на это сообщение:\n"
                "@LLMeets_bot preview\n"
                "@LLMeets_bot создать\n"
                "@LLMeets_bot обновить 168334"
            ),
            payload=record.model_dump(),
        )

    @staticmethod
    def _extract_publication_metadata(text: str) -> dict[str, object]:
        loom_match = LOOM_URL_RE.search(text)
        source_url = loom_match.group(0).rstrip(".,;") if loom_match else None
        loom_video_id = loom_match.group(1) if loom_match else None
        source_tags = sorted(set(re.findall(r"#[\wА-Яа-яІіЇїЄєҐґ-]+", text, flags=re.UNICODE)))

        explicit_id = re.search(r"Loom video ID:\s*([A-Za-z0-9]+)", text, re.IGNORECASE)
        if explicit_id:
            loom_video_id = explicit_id.group(1)
            source_url = source_url or f"https://www.loom.com/share/{loom_video_id}"

        title = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for prefix in ("Meeting:", "Встреча:", "Meeting Note:", "Transcript:"):
                if line.lower().startswith(prefix.lower()):
                    title = line.split(":", 1)[1].strip()
                    break
            if title:
                break
        if not title:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            title = first_line[:180] if first_line else None

        google_doc_url = None
        transcript_doc_url = None
        doc_section_title = None
        transcript_section_title = None
        doc_urls = GOOGLE_DOC_RE.findall(text)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            lowered = line.lower()
            if lowered.startswith("doc section:"):
                doc_section_title = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("transcript section:"):
                transcript_section_title = line.split(":", 1)[1].strip()
                continue
            doc_match = GOOGLE_DOC_RE.search(line)
            if not doc_match:
                continue
            url = doc_match.group(0).rstrip(".,;")
            if "transcript" in lowered or "транскрип" in lowered:
                transcript_doc_url = transcript_doc_url or url
            else:
                google_doc_url = google_doc_url or url

        if doc_urls:
            google_doc_url = google_doc_url or doc_urls[0].rstrip(".,;")
            if len(doc_urls) > 1:
                transcript_doc_url = transcript_doc_url or doc_urls[1].rstrip(".,;")

        return {
            "loom_video_id": loom_video_id,
            "source_url": source_url,
            "meeting_title": title,
            "google_doc_url": google_doc_url,
            "transcript_doc_url": transcript_doc_url,
            "doc_section_title": doc_section_title,
            "transcript_section_title": transcript_section_title,
            "source_tags": source_tags,
        }

    @staticmethod
    def _post_link_from_reply_text(message: dict | None) -> str | None:
        if not message:
            return None
        reply = message.get("reply_to_message") or {}
        reply_text = reply.get("text") or reply.get("caption") or ""
        post_link = extract_post_link(reply_text)
        return post_link.raw_url if post_link else None

    @staticmethod
    def _post_url_from_reply(message: dict | None) -> str | None:
        if not message:
            return None
        reply = message.get("reply_to_message") or {}
        reply_message_id = reply.get("message_id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        username = chat.get("username")
        if not reply_message_id or not chat_id:
            return None
        if username:
            return f"https://t.me/{str(username).lstrip('@')}/{reply_message_id}"
        raw_chat_id = str(chat_id).strip()
        if raw_chat_id.startswith("-100"):
            internal_chat_id = raw_chat_id[4:]
        elif raw_chat_id.startswith("-"):
            internal_chat_id = raw_chat_id[1:]
        else:
            internal_chat_id = raw_chat_id
        return f"https://t.me/c/{internal_chat_id}/{reply_message_id}"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.replace("\u00a0", " ").strip()

    @staticmethod
    def _strip_bot_mention(text: str) -> str:
        return BOT_MENTION_RE.sub(" ", text).strip()

    @classmethod
    def _is_help_command(cls, text: str) -> bool:
        cleaned = cls._strip_bot_mention(text).strip().lower()
        return cleaned in {"/start", "/start@llmeets_bot", "/help", "/help@llmeets_bot", "help", "помощь"}

    @classmethod
    def _is_register_command(cls, text: str) -> bool:
        cleaned = f" {cls._strip_bot_mention(text).strip().lower()} "
        return any(
            marker in cleaned
            for marker in [
                " register ",
                " /register ",
                " зарегистрировать ",
                " /зарегистрировать ",
                " зарегистрируй ",
                " регистрация ",
                " зареєструвати ",
                " зареєструй ",
            ]
        )

    @classmethod
    def _is_knowledge_alert_here_command(cls, text: str) -> bool:
        cleaned = f" {cls._strip_bot_mention(text).strip().lower()} "
        return any(marker in cleaned for marker in [" kb_alert_here ", " knowledge_alert_here ", " alerts_here "])

    @staticmethod
    def _help_text() -> str:
        return (
            "Пришлите ссылку на Telegram-пост с дайджестом Loom.\n\n"
            "Примеры:\n"
            "@LLMeets_bot https://t.me/c/5147878786/120 preview\n"
            "@LLMeets_bot https://t.me/c/5147878786/120 создать\n"
            "@LLMeets_bot https://t.me/c/5147878786/120 коммент 168334\n"
            "@LLMeets_bot https://t.me/c/5147878786/120 чеклист 168334\n"
            "@LLMeets_bot https://t.me/c/5147878786/120 обновить 168334\n"
            "Ответом на старый пост: @LLMeets_bot зарегистрировать\n"
            "@LLMeets_bot план 2026-05-04 preview\n"
            "@LLMeets_bot план 2026-05-04 создать\n"
            "@LLMeets_bot итоги вчера\n"
            "@LLMeets_bot итоги 2026-05-04\n"
            "@LLMeets_bot итоги недели 2026-05-04 2026-05-08\n"
            "/day@LLMeets_bot 2026-04-14 week 168336\n"
            "/week@LLMeets_bot 2026-04-27 2026-05-03\n\n"
            "Если задача уже привязана к этой встрече, режим auto добавит комментарий в существующую задачу."
        )

    @staticmethod
    def _format_sync_result(scope: str, result) -> str:
        if result.action == "preview":
            details = result.details or {}
            lines = [
                f"Предпросмотр {scope}: запись в CRM не выполнялась.",
                f"Действие в auto: {details.get('would_action_if_auto')}",
                f"Заголовок: {result.title}",
            ]
            if details.get("stale_binding_reset"):
                lines.append(
                    f"Старая привязка к задаче #{details.get('stale_binding_task_id')} сброшена: задача не найдена в CRM."
                )
            if result.task_id:
                lines.append(f"Целевая задача: #{result.task_id}")
            if result.task_url:
                lines.append(str(result.task_url))
            post_url = details.get("post_url")
            if post_url:
                lines.append(f"Источник: {post_url}")
            checklists = details.get("checklists") or []
            if checklists:
                lines.append("Чеклисты:")
                for item in checklists[:5]:
                    suffix = ""
                    if "would_add" in item:
                        if item.get("dedupe_unavailable"):
                            suffix = f", добавится до {item.get('would_add')}, дедупликация недоступна"
                        else:
                            suffix = f", добавится {item.get('would_add')}, пропустится {item.get('would_skip')}"
                    lines.append(f"- {item.get('title')}: {item.get('items_count')} пунктов{suffix}")
            matches = details.get("task_matches") or []
            if matches:
                lines.append("")
                lines.append("Похожие задачи:")
                for match in matches[:5]:
                    lines.append(f"- #{match.get('task_id')} ({match.get('score')}): {match.get('title')}")
            lines.append("")
            lines.append("Для записи в CRM добавьте команду: создать, коммент, чеклист или обновить.")
            return "\n".join(lines)
        if result.action == "merged_update":
            details = result.details or {}
            lines = [
                "Задача обновлена в merge-режиме.",
                f"Задача #{result.task_id}",
            ]
            if result.task_url:
                lines.append(str(result.task_url))
            if details.get("point_number"):
                status = "заменен" if details.get("point_replaced") else "добавлен"
                lines.append(f"Point {details.get('point_number')}: {status}.")
            checklist = details.get("checklist") or {}
            if checklist:
                lines.append(
                    f"Чеклист: {checklist.get('group')} "
                    f"(добавлено {checklist.get('added')}, пропущено {checklist.get('skipped')})."
                )
            return "\n".join(lines)
        return f"Синхронизация {scope} выполнена: {result.action}. Задача #{result.task_id}\n{result.task_url}"

    @staticmethod
    def _extract_team_name(text: str) -> str | None:
        match = re.search(r"(?:команда|team)\s+(.+)$", text, flags=re.IGNORECASE)
        if not match:
            return None
        team = match.group(1).strip()
        team = re.sub(r"\b(?:preview|создать|create|обновить|update|коммент|comment|чеклист|checklist)\b", "", team, flags=re.IGNORECASE).strip()
        return team or None
