from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable
import zipfile
from urllib.parse import parse_qs, quote_plus, urlparse
from zoneinfo import ZoneInfo

import requests

from .knowledge_alerts import read_knowledge_alert_chat_id, write_knowledge_alert_chat_id
from .knowledge_rag import KnowledgeVectorStore, client_from_env
from .knowledge_repo import KnowledgeRepository
from .notebooklm_agent import NotebookLMAgent
from .models import (
    DailyPlanSyncRequest,
    DailyReportRequest,
    DigestType,
    DaySyncRequest,
    PostSyncRequest,
    PublicationRegistrationRequest,
    SyncAction,
    TaskExtractorAction,
    TaskExtractorRequest,
    TelegramCommand,
    TelegramResponse,
    WeeklyReportRequest,
)
from .service import MeetingDigestService
from .telegram_links import extract_post_link, extract_task_id


BOT_MENTION_RE = re.compile(r"@LLMeets_bot\b", re.IGNORECASE)
TASK_EXTRACTOR_MENTION_RE = re.compile(r"@Task_?Extractor_?Bot\b", re.IGNORECASE)
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
TRUSTED_SOURCE_URL_RE = re.compile(r"https?://[^\s<>)]+", re.IGNORECASE)


@dataclass(slots=True)
class TelegramBotFacade:
    service: MeetingDigestService
    token: str
    task_extractor_mode: bool = False
    _knowledge_sessions: dict[str, dict] = field(default_factory=dict, init=False, repr=False)
    _proposal_refs: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/"

    @staticmethod
    def _voice_transcription_failed_response() -> TelegramResponse:
        return TelegramResponse(
            ok=False,
            text="Не удалось распознать голосовое сообщение. NotebookLM не запускал. Отправьте voice еще раз или напишите запрос текстом.",
            payload={"intent": "voice_transcription_failed", "notebooklm_queued": False},
        )

    def process_update(self, update: dict) -> TelegramResponse:
        callback_response = self._process_callback_query(update.get("callback_query") or {})
        if callback_response:
            return callback_response

        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = self._normalize_text(message.get("text") or message.get("caption") or "")
        direct_audio_message = bool(message.get("voice") or message.get("audio")) and not text
        if not text and (message.get("voice") or message.get("audio")):
            text = self._transcribe_telegram_audio(message)
            if not text:
                response = self._voice_transcription_failed_response()
                if chat_id:
                    self.send_message(chat_id, response.text, reply_to_message_id=message.get("message_id"))
                return response
        if BOT_MENTION_RE.search(text):
            reply = message.get("reply_to_message") or {}
            if reply.get("voice") or reply.get("audio"):
                cleaned = self._strip_bot_mention(text).strip()
                voice_action = self._voice_reply_action(cleaned)
                if voice_action:
                    transcribed = self._transcribe_telegram_audio(reply)
                    if transcribed:
                        text = f"@LLMeets_bot {voice_action} {transcribed}".strip()
                    else:
                        response = self._voice_transcription_failed_response()
                        if chat_id:
                            self.send_message(chat_id, response.text, reply_to_message_id=message.get("message_id"))
                        return response
        if BOT_MENTION_RE.search(text) and self._is_mention_only(text):
            reply = message.get("reply_to_message") or {}
            if reply.get("voice") or reply.get("audio"):
                transcribed = self._transcribe_telegram_audio(reply)
                if transcribed:
                    text = transcribed
                else:
                    response = self._voice_transcription_failed_response()
                    if chat_id:
                        self.send_message(chat_id, response.text, reply_to_message_id=message.get("message_id"))
                    return response
        if not text:
            if direct_audio_message:
                response = self._voice_transcription_failed_response()
                if chat_id:
                    self.send_message(chat_id, response.text, reply_to_message_id=message.get("message_id"))
                return response
            return TelegramResponse(
                ok=False,
                text="Пришлите ссылку на пост Telegram и, при необходимости, номер задачи.",
            )

        task_extractor_response = self._process_task_extractor_request(text, message=message)
        if task_extractor_response:
            if chat_id and task_extractor_response.text:
                attachment = task_extractor_response.payload.get("attachment_path")
                if attachment:
                    self.send_document(chat_id, str(attachment), caption=task_extractor_response.text[:1000])
                else:
                    self.send_message(chat_id, task_extractor_response.text, reply_to_message_id=message.get("message_id"))
            return task_extractor_response

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

        kb_response = self._process_kb_command(text) if self._chat_allowed("KNOWLEDGE_ALLOWED_CHAT_IDS", chat_id) else None
        if kb_response:
            if chat_id:
                user_id = (message.get("from") or {}).get("id")
                self._remember_knowledge_context(chat_id, user_id, kb_response)
                attachment = kb_response.payload.get("attachment_path")
                if attachment:
                    self.send_document(chat_id, str(attachment), caption=kb_response.text[:1000])
                else:
                    self.send_message(chat_id, kb_response.text, reply_markup=self._keyboard_for_response(kb_response, chat_id=chat_id, user_id=user_id))
            return kb_response

        kb_ai_response = self._process_knowledge_ai_request(text, message=message) if self._chat_allowed("KNOWLEDGE_ALLOWED_CHAT_IDS", chat_id) else None
        if kb_ai_response:
            if chat_id:
                user_id = (message.get("from") or {}).get("id")
                self._remember_knowledge_context(chat_id, user_id, kb_ai_response)
                attachment = kb_ai_response.payload.get("attachment_path")
                if attachment:
                    self.send_document(chat_id, str(attachment), caption=kb_ai_response.text[:1000])
                else:
                    self.send_message(
                        chat_id,
                        kb_ai_response.text,
                        reply_to_message_id=message.get("message_id"),
                        reply_markup=self._keyboard_for_response(kb_ai_response, chat_id=chat_id, user_id=user_id),
                    )
            return kb_ai_response

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

        if not self._chat_allowed("MEETING_ALLOWED_CHAT_IDS", chat_id):
            response = TelegramResponse(ok=False, text="Команда не включена для этого чата.")
            if chat_id:
                self.send_message(chat_id, response.text)
            return response

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
            result = self.service.run_weekly_report(
                WeeklyReportRequest(
                    week_from=command.week_from,
                    week_to=command.week_to,
                    team_name=command.team_name or "Bitrix Develop Team",
                    force=True,
                    send_telegram=False,
                )
            )
            response = TelegramResponse(
                ok=True,
                text=str((result.details or {}).get("telegram_text") or self._format_sync_result("итогов недели", result)),
                payload=result.model_dump(),
            )
        elif command.post_url:
            result = self._sync_post_command(command, action=action, message=message)
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

    def _process_task_extractor_request(self, text: str, *, message: dict) -> TelegramResponse | None:
        action = self._task_extractor_action(text)
        mentioned = TASK_EXTRACTOR_MENTION_RE.search(text) is not None
        if not mentioned and not self.task_extractor_mode:
            return None
        if not action and self.task_extractor_mode:
            if not self._looks_like_task_extractor_source(text):
                return None
            action = TaskExtractorAction.add
        if not action:
            action = TaskExtractorAction.status

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        reply = message.get("reply_to_message") or {}
        reply_text = self._normalize_text(reply.get("text") or reply.get("caption") or "")
        target_task_id = extract_task_id(text)
        try:
            result = self.service.task_extractor.handle(
                TaskExtractorRequest(
                    action=action,
                    chat_id=str(chat.get("id") or ""),
                    message_id=str(message.get("message_id") or ""),
                    user_id=str(sender.get("id") or ""),
                    text=text,
                    reply_text=reply_text,
                    target_task_id=target_task_id,
                )
            )
        except Exception as exc:
            return TelegramResponse(
                ok=False,
                text=f"Task Extractor failed: {exc}",
                payload={"intent": "task_extractor", "error": str(exc)},
            )
        payload = result.model_dump()
        if result.zip_path:
            payload["attachment_path"] = result.zip_path
        return TelegramResponse(ok=True, text=result.text, payload=payload)

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

    def _process_callback_query(self, callback: dict) -> TelegramResponse | None:
        if not callback:
            return None
        data = str(callback.get("data") or "")
        if not data.startswith("kb:"):
            return None
        callback_id = callback.get("id")
        if callback_id:
            self._answer_callback_query(str(callback_id))
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user_id = (callback.get("from") or {}).get("id")
        session_key = self._knowledge_session_key(chat_id, user_id)
        action = data.split(":", 1)[1]
        if not self._chat_allowed("KNOWLEDGE_ALLOWED_CHAT_IDS", chat_id):
            response = TelegramResponse(ok=False, text="База знаний не включена для этого чата.", payload={"intent": action})
            if chat_id:
                self.send_message(chat_id, response.text)
            return response
        if action == "menu":
            response = TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        elif action == "ask":
            response = TelegramResponse(
                ok=True,
                text="Напишите вопрос текстом или отправьте voice и ответьте на него сообщением `@LLMeets_bot ask`.",
                payload={"intent": "ask_prompt"},
            )
        elif action == "notebooklm":
            response = self._process_notebooklm_callback(
                session_key=session_key,
                chat_id=chat_id,
                reply_to_message_id=(message.get("reply_to_message") or {}).get("message_id") or message.get("message_id"),
            )
        elif action.startswith("proposal:"):
            response = self._process_proposal_callback(action, session_key=session_key)
        else:
            original = message.get("reply_to_message") or {}
            query = self._normalize_text(original.get("text") or original.get("caption") or "")
            if not query:
                query = self._normalize_text(message.get("text") or message.get("caption") or "")
            query = self._strip_bot_mention(query)
            query = self._query_from_session(session_key=session_key, action=action, fallback=query)
            if not query:
                response = TelegramResponse(
                    ok=False,
                    text="Не вижу исходный запрос. Напишите или наговорите его еще раз и выберите действие.",
                    payload={"intent": action},
                )
            else:
                response = self._run_knowledge_intent(action, query)
                self._remember_knowledge_context(chat_id, user_id, response)
        if chat_id:
            attachment = response.payload.get("attachment_path")
            if attachment:
                self.send_document(chat_id, str(attachment), caption=response.text[:1000])
            else:
                self.send_message(chat_id, response.text, reply_markup=self._keyboard_for_response(response, chat_id=chat_id, user_id=user_id))
        return response

    def _process_kb_command(self, text: str) -> TelegramResponse | None:
        command_text = self._strip_bot_mention(text).strip()
        match = re.match(r"^(?:/)?kb(?:\s+|$)(.*)$", command_text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        args = match.group(1).strip()
        parts = args.split()
        action = (parts[0].lower() if parts else "help").strip()
        token = parts[1] if len(parts) > 1 else ""
        repo = KnowledgeRepository(Path(os.environ.get("KNOWLEDGE_REPO_PATH", "company-knowledge")))
        try:
            if action in {"proposals", "proposal", "list"}:
                return self._run_knowledge_intent("proposals", "")
            if action in {"diff", "show"} and token:
                metadata_path = repo.resolve_revision_metadata(token)
                if not metadata_path:
                    return TelegramResponse(ok=False, text=f"KB proposal not found: {token}")
                return TelegramResponse(ok=True, text=self._revision_impact_preview(repo, metadata_path=metadata_path))
            if action in {"approve", "reject"} and token:
                metadata_path = repo.resolve_revision_metadata(token)
                if not metadata_path:
                    return TelegramResponse(ok=False, text=f"KB proposal not found: {token}")
                proposal = repo.set_revision_status(metadata_path=metadata_path, status="approved" if action == "approve" else "rejected")
                return TelegramResponse(ok=True, text=f"KB proposal {proposal.object_id}: {proposal.status}", payload=proposal.model_dump())
            if action == "apply" and token:
                metadata_path = repo.resolve_revision_metadata(token)
                if not metadata_path:
                    return TelegramResponse(ok=False, text=f"KB proposal not found: {token}")
                proposal = repo.apply_resolved_revision(metadata_path=metadata_path)
                return TelegramResponse(ok=True, text=f"KB proposal {proposal.object_id}: applied", payload=proposal.model_dump())
            if action == "health":
                pending = len(repo.list_revision_metadata(status="draft"))
                rag = KnowledgeVectorStore(repo.root).stats()
                quality = repo.quality_report()
                lines = [
                    "KB health:",
                    f"- repo: {repo.root}",
                    f"- pending proposals: {pending}",
                    f"- rag chunks: {rag.get('chunks_embedded', 0)}",
                    f"- rag usage tokens: {(rag.get('usage') or {}).get('estimated_tokens', 0)}",
                    f"- quality issues: {len(quality.issues)}",
                    f"- alert chat: {os.environ.get('KNOWLEDGE_ALERT_CHAT_ID') or read_knowledge_alert_chat_id() or '-'}",
                ]
                return TelegramResponse(ok=True, text="\n".join(lines), payload={"pending_proposals": pending, "rag": rag})
            if action == "ask":
                question = args[len(parts[0]) :].strip() if parts else ""
                if not question:
                    return TelegramResponse(ok=False, text="Usage: kb ask <question>")
                return self._run_knowledge_intent("ask", question)
            if action in {"instruction", "guide"}:
                query = args[len(parts[0]) :].strip() if parts else ""
                if not query:
                    return TelegramResponse(ok=False, text="Usage: kb instruction <request>")
                return self._run_knowledge_intent("instruction", query)
            if action in {"spec", "tz", "тз"}:
                query = args[len(parts[0]) :].strip() if parts else ""
                if not query:
                    return TelegramResponse(ok=False, text="Usage: kb spec <request>")
                return self._run_knowledge_intent("spec", query)
            if action == "export":
                query = args[len(parts[0]) :].strip() if parts else ""
                return self._run_knowledge_intent("export", query or "notebooklm")
        except Exception as exc:
            return TelegramResponse(ok=False, text=f"KB command failed: {exc}")
        return TelegramResponse(
            ok=True,
            text=self._knowledge_menu_text(),
            payload={"intent": "menu"},
        )

    def _process_knowledge_ai_request(self, text: str, *, message: dict) -> TelegramResponse | None:
        if not self._should_handle_knowledge_ai(text, message=message):
            return None
        query = self._strip_bot_mention(text).strip()
        if not query:
            return TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        query = self._strip_leading_kb_action(query)
        intent = self._classify_knowledge_intent(query)
        if intent == "menu":
            return TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        chat = message.get("chat") or {}
        trusted_sources = self._trusted_sources_from_message(message)
        return self._run_knowledge_intent(
            intent,
            query,
            telegram_chat_id=chat.get("id"),
            telegram_reply_to_message_id=message.get("message_id"),
            trusted_sources=trusted_sources,
        )

    def _run_knowledge_intent(
        self,
        intent: str,
        query: str,
        *,
        telegram_chat_id: int | str | None = None,
        telegram_reply_to_message_id: int | str | None = None,
        trusted_sources: list[dict] | None = None,
    ) -> TelegramResponse:
        repo = KnowledgeRepository(Path(os.environ.get("KNOWLEDGE_REPO_PATH", "company-knowledge")))
        if intent in {"health", "status"}:
            pending = len(repo.list_revision_metadata(status="draft"))
            rag = KnowledgeVectorStore(repo.root).stats()
            quality = repo.quality_report()
            return TelegramResponse(
                ok=True,
                text="\n".join(
                    [
                        "Статус базы знаний:",
                        f"- правок на проверке: {pending}",
                        f"- RAG chunks: {rag.get('chunks_embedded', 0)}",
                        f"- токены индекса: {(rag.get('usage') or {}).get('estimated_tokens', 0)}",
                        f"- замечания качества: {len(quality.issues)}",
                    ]
                ),
                payload={"intent": "health", "pending_proposals": pending, "rag": rag},
            )
        if intent in {"proposals", "review_proposals"}:
            items = repo.list_revision_metadata(status="draft")
            if not items:
                return TelegramResponse(ok=True, text="Черновиков правок нет.", payload={"intent": "proposals", "count": 0})
            lines = [f"Правки на проверке: {len(items)}"]
            for item in items[:10]:
                lines.append(f"- {item.get('object_id')} [{item.get('source') or 'revision'}] статус={item.get('status')}")
            return TelegramResponse(ok=True, text="\n".join(lines), payload={"intent": "proposals", "count": len(items), "proposals": items[:10]})
        if intent in {"export", "export_bundle"}:
            target = "agents" if any(marker in query.casefold() for marker in ["agent", "api", "machine"]) else "notebooklm"
            result = repo.export_external_bundle(target=target)
            zip_paths = [path for path in result.written_files if str(path).endswith(".zip")]
            zip_path = zip_paths[-1] if zip_paths else ""
            return TelegramResponse(
                ok=True,
                text=f"Экспорт готов: {target}. Объектов: {result.objects_count}.",
                payload={"intent": "export_bundle", "query": query, "attachment_path": zip_path, "result": result.model_dump()},
            )
        if intent in {"revise", "revise_knowledge"}:
            return self._create_knowledge_revision_from_query(repo, query, trusted_sources=trusted_sources or [])

        answer_mode = {
            "instruction": "user_instruction",
            "generate_instruction": "user_instruction",
            "spec": "technical_spec",
            "generate_spec": "technical_spec",
            "support": "support_answer",
        }.get(intent, "general")
        result = self._answer_from_knowledge(repo, query, answer_mode=answer_mode)
        sources = result.get("sources") or []
        source_lines = []
        for item in sources[:3]:
            source_lines.append(f"- {item.get('object_id')} / {item.get('chunk_id') or item.get('score')}")
        text = str(result.get("answer") or "")
        if source_lines:
            text = text.strip() + "\n\nИсточники:\n" + "\n".join(source_lines)
        notebook_prompt_path = self._queue_notebooklm_followup(
            query=query,
            answer=text,
            sources=sources,
            answer_mode=answer_mode,
            telegram_chat_id=telegram_chat_id,
            telegram_reply_to_message_id=telegram_reply_to_message_id,
        )
        if notebook_prompt_path:
            text = text.strip() + "\n\nNotebookLM: проверка отправлена в фоновую очередь."
        return TelegramResponse(
            ok=True,
            text=text[:3900],
            payload={
                "intent": intent,
                "query": query,
                "answer_mode": answer_mode,
                "sources_count": len(sources),
                "notebooklm_prompt_path": notebook_prompt_path,
            },
        )

    def _answer_from_knowledge(self, repo: KnowledgeRepository, query: str, *, answer_mode: str) -> dict:
        client = client_from_env(dict(os.environ), require_llm=True)
        if client:
            store = KnowledgeVectorStore(
                repo.root,
                db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
                embeddings_model=client.embeddings_model,
            )
            retrieval_query = self._knowledge_retrieval_query(query)
            return store.answer(
                query,
                embedding_client=client,
                chat_client=client,
                retrieval_query=retrieval_query,
                limit=int(os.environ.get("KNOWLEDGE_RAG_CONTEXT_LIMIT", "10")),
                threshold=float(os.environ.get("KNOWLEDGE_RAG_SEARCH_THRESHOLD", "-1.0")),
                min_score=float(os.environ.get("KNOWLEDGE_RAG_MIN_SCORE", "0.12")),
                answer_mode=answer_mode,
            )
        return repo.ask(query, limit=5)

    @staticmethod
    def _knowledge_retrieval_query(query: str) -> str:
        lowered = query.casefold()
        hints: list[str] = []
        if any(marker in lowered for marker in ["реквиз", "реквіз", "единые", "єдині"]):
            hints.append(
                "единый реквизит единые реквизиты реквизиты ФОП реквізити ФОП "
                "объединение подзаказов групповое замовлення консолидированное замовлення "
                "общий платеж Payments Pro"
            )
        if any(marker in lowered for marker in ["заказ", "замов", "подзаказ"]):
            hints.append("заказ заказы замовлення подзаказ подзаказы групповой заказ консолидированный заказ")
        if any(marker in lowered for marker in ["платеж", "платіж", "payment"]):
            hints.append("платеж оплата payment Payments Pro AssetPayments общий платеж")
        if not hints:
            return query
        return query + "\n\nПоисковые синонимы и связанные термины:\n" + "\n".join(f"- {hint}" for hint in hints)

    def _create_knowledge_revision_from_query(self, repo: KnowledgeRepository, query: str, *, trusted_sources: list[dict] | None = None) -> TelegramResponse:
        result = self._answer_from_knowledge(repo, query, answer_mode="general")
        normalized = self._normalize_revision_query(repo, query=query, answer_result=result, extra_trusted_sources=trusted_sources or [])
        cleaned_query = str(normalized.get("cleaned_query") or query).strip() or query
        replacements = self._filter_revision_replacements(
            (str(item.get("old") or ""), str(item.get("new") or ""))
            for item in normalized.get("replacements") or []
            if isinstance(item, dict) and str(item.get("old") or "").strip() and str(item.get("new") or "").strip()
        )
        instruction_summary = str(normalized.get("instruction_summary") or "").strip()
        source_ids: list[str] = []
        for item in result.get("sources") or []:
            object_id = str(item.get("object_id") or "")
            if object_id and object_id not in source_ids:
                source_ids.append(object_id)
        object_ids = self._affected_revision_object_ids(repo, query=cleaned_query, source_ids=source_ids, replacements=replacements)
        if not object_ids:
            return TelegramResponse(
                ok=False,
                text="Не нашел подходящие объекты базы знаний для правки. Уточните систему, функциональность или object_id.",
                payload={"intent": "revise_knowledge"},
            )
        proposals = [
            repo.create_revision_proposal(
                object_id=object_id,
                correction=cleaned_query,
                replacements=[{"old": old, "new": new} for old, new in replacements],
                trusted_sources=normalized.get("trusted_sources") or [],
                instruction_summary=instruction_summary,
            ).model_dump()
            for object_id in object_ids
        ]
        lines = [f"Создал правки на проверку: {len(proposals)}."]
        if instruction_summary:
            lines.extend(["", "Как я понял инструкцию:", instruction_summary])
        if normalized.get("used_ai") and cleaned_query != query:
            lines.extend(["", "Очищенная формулировка правки:", cleaned_query])
        if replacements:
            lines.extend(["", "Что реально изменится в ответах и инструкциях:"])
            lines.extend(f"- `{old}` будет трактоваться и записываться как `{new}`." for old, new in replacements)
        else:
            lines.extend(["", "Что реально изменится:"])
            lines.append("- База зафиксирует вашу корректировку как правило для связанных объектов и последующего применения.")
        lines.extend(["", "Связанные объекты:"])
        for object_id in object_ids[:8]:
            lines.append(f"- {self._knowledge_object_label(repo, object_id)}")
        if len(proposals) > 1:
            lines.extend(["", "Нажмите `Применить все`, если сводка верная, или `Отклонить все`, если правку нужно переформулировать."])
        else:
            lines.extend(["", "Нажмите кнопку ниже: можно посмотреть эффект, применить или отклонить."])
        return TelegramResponse(
            ok=True,
            text=self._revision_batch_impact_preview(repo, proposals=proposals) if len(proposals) > 1 else "\n".join(lines)[:3900],
            payload={
                "intent": "revise_knowledge",
                "query": cleaned_query,
                "raw_query": query,
                "normalization": normalized,
                "instruction_summary": instruction_summary,
                "proposal": proposals[0] if len(proposals) == 1 else {},
                "proposals": proposals,
                "replacements": [{"old": old, "new": new} for old, new in replacements],
            },
        )

    def _normalize_revision_query(self, repo: KnowledgeRepository, *, query: str, answer_result: dict, extra_trusted_sources: list[dict] | None = None) -> dict:
        trusted_sources = [*(extra_trusted_sources or []), *self._extract_trusted_revision_sources(query)]
        fallback_replacements = [{"old": old, "new": new} for old, new in self._extract_term_replacements(query)]
        fallback = {
            "cleaned_query": query,
            "replacements": fallback_replacements,
            "instruction_summary": self._fallback_instruction_summary(query, trusted_sources),
            "notes": [],
            "confidence": "low" if not fallback_replacements else "medium",
            "used_ai": False,
            "trusted_sources": trusted_sources,
        }
        client = client_from_env(dict(os.environ), require_llm=True)
        if not client or not hasattr(client, "complete_messages"):
            return fallback
        source_lines = []
        for item in (answer_result.get("sources") or [])[:6]:
            source_lines.append(
                "\n".join(
                    [
                        f"- object_id: {item.get('object_id')}",
                        f"  title: {item.get('title')}",
                        f"  snippets: {' | '.join(str(snippet) for snippet in (item.get('snippets') or [])[:3])}",
                    ]
                )
            )
        trusted_source_lines = []
        for idx, item in enumerate(trusted_sources, start=1):
            trusted_source_lines.append(
                "\n".join(
                    [
                        f"[trusted_source_{idx}]",
                        f"type: {item.get('type')}",
                        f"url: {item.get('url')}",
                        f"title: {item.get('title') or '-'}",
                        f"status: {item.get('status')}",
                        "text:",
                        str(item.get("text") or item.get("message") or "")[:12000],
                    ]
                )
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "You clean noisy speech-to-text corrections for a company knowledge base. "
                    "Do not invent product behavior. Preserve the user's intent. "
                    "If trusted source text is provided, use it as the strongest evidence for the correction. "
                    "If a trusted source is unavailable, mention that in notes and do not invent its content. "
                    "Normalize obvious transcription mistakes only when the source context or the user wording supports it. "
                    "Extract explicit terminology replacements such as 'old term should be new term'. "
                    "Do not create replacements from Telegram command words such as правка, исправь, обнови знание, знание, инструкция, replace, correction. "
                    "Replacements are only for domain terms, abbreviations, product names, or transcription artifacts. "
                    "If the user provides a trusted full instruction, summarize what knowledge rule should change and leave replacements empty unless explicit terminology replacements are present. "
                    "Return strict JSON with keys: cleaned_query, instruction_summary, replacements, notes, confidence. "
                    "instruction_summary must be 2-5 short Russian sentences explaining how you understood the requested knowledge update. "
                    "replacements must be an array of {old,new,reason}. "
                    "If unsure, keep the original wording and put a note."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Raw correction transcript:\n"
                    f"{query}\n\n"
                    "Trusted correction sources from user URLs:\n"
                    f"{chr(10).join(trusted_source_lines) or '-'}\n\n"
                    "Relevant knowledge sources:\n"
                    f"{chr(10).join(source_lines) or '-'}\n\n"
                    "Return only JSON."
                ),
            },
        ]
        try:
            content = client.complete_messages(messages, temperature=0.0)
            parsed = self._parse_json_object(content)
        except Exception:
            return fallback
        if not isinstance(parsed, dict):
            return fallback
        cleaned = str(parsed.get("cleaned_query") or query).strip() or query
        replacements = []
        seen: set[tuple[str, str]] = set()
        for item in parsed.get("replacements") or []:
            if not isinstance(item, dict):
                continue
            old = str(item.get("old") or "").strip()
            new = str(item.get("new") or "").strip()
            if not old or not new or old.casefold() == new.casefold():
                continue
            key = (old.casefold(), new.casefold())
            if key in seen:
                continue
            seen.add(key)
            replacements.append({"old": old, "new": new, "reason": str(item.get("reason") or "").strip()})
        for item in fallback_replacements:
            key = (item["old"].casefold(), item["new"].casefold())
            if key not in seen:
                replacements.append(item)
                seen.add(key)
        filtered_pairs = self._filter_revision_replacements(
            (str(item.get("old") or ""), str(item.get("new") or "")) for item in replacements if isinstance(item, dict)
        )
        replacements = [
            {
                "old": old,
                "new": new,
                "reason": next(
                    (
                        str(item.get("reason") or "")
                        for item in replacements
                        if isinstance(item, dict)
                        and str(item.get("old") or "").casefold() == old.casefold()
                        and str(item.get("new") or "").casefold() == new.casefold()
                    ),
                    "",
                ),
            }
            for old, new in filtered_pairs
        ]
        notes = parsed.get("notes") if isinstance(parsed.get("notes"), list) else []
        instruction_summary = str(parsed.get("instruction_summary") or "").strip()
        if not instruction_summary:
            instruction_summary = self._fallback_instruction_summary(cleaned, trusted_sources)
        return {
            "cleaned_query": cleaned,
            "instruction_summary": instruction_summary,
            "replacements": replacements,
            "notes": [str(item) for item in notes if str(item).strip()][:5],
            "confidence": str(parsed.get("confidence") or "medium"),
            "used_ai": True,
            "trusted_sources": trusted_sources,
        }

    def _extract_trusted_revision_sources(self, query: str) -> list[dict]:
        sources: list[dict] = []
        seen: set[str] = set()
        for match in TRUSTED_SOURCE_URL_RE.finditer(query):
            url = match.group(0).rstrip(".,;:)]}")
            if url in seen:
                continue
            seen.add(url)
            source_type = self._trusted_source_type(url)
            if source_type not in {"google_doc", "notion", "youtube"}:
                continue
            sources.append(self._fetch_trusted_revision_source(url, source_type=source_type))
        return sources[:5]

    def _trusted_sources_from_message(self, message: dict) -> list[dict]:
        document = message.get("document") or {}
        if not document:
            return []
        file_name = str(document.get("file_name") or "telegram_document").strip() or "telegram_document"
        file_id = str(document.get("file_id") or "").strip()
        if not file_id:
            return []
        try:
            content = self._download_telegram_file(file_id)
            if not content:
                return []
            source = self._trusted_source_from_file_bytes(content, file_name=file_name)
            return [source] if source else []
        except Exception as exc:
            return [
                {
                    "url": f"telegram:{file_name}",
                    "type": "telegram_document",
                    "status": "unavailable",
                    "title": file_name,
                    "text": "",
                    "message": f"Telegram document fetch failed: {exc}",
                }
            ]

    def _download_telegram_file(self, file_id: str) -> bytes:
        file_info = requests.get(self.api_url + "getFile", params={"file_id": file_id}, timeout=30)
        file_info.raise_for_status()
        file_path = ((file_info.json().get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            return b""
        download = requests.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=120)
        download.raise_for_status()
        return download.content

    def _trusted_source_from_file_bytes(self, content: bytes, *, file_name: str) -> dict:
        suffix = Path(file_name).suffix.casefold()
        base = {"url": f"telegram:{file_name}", "type": "telegram_document", "status": "unavailable", "title": file_name, "text": "", "message": ""}
        if suffix == ".zip":
            text = self._extract_text_from_zip_bundle(content)
            return {
                **base,
                "type": "notebooklm_bundle",
                "status": "fetched" if text else "unavailable",
                "text": text,
                "message": "" if text else "No supported .md/.txt/.json files found in bundle.",
            }
        if suffix in {".md", ".markdown", ".txt", ".json"}:
            text = self._decode_text_bytes(content)
            return {**base, "type": "trusted_file", "status": "fetched" if text else "unavailable", "text": text}
        return {**base, "message": "Unsupported trusted source file type. Use .md, .txt, .json, or .zip bundle."}

    @staticmethod
    def _decode_text_bytes(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                return TelegramBotFacade._clean_source_text(content.decode(encoding))
            except UnicodeDecodeError:
                continue
        return ""

    @staticmethod
    def _extract_text_from_zip_bundle(content: bytes) -> str:
        chunks: list[str] = []
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.file_size > 2_000_000:
                    continue
                suffix = Path(info.filename).suffix.casefold()
                if suffix not in {".md", ".markdown", ".txt", ".json"}:
                    continue
                with archive.open(info) as file:
                    text = TelegramBotFacade._decode_text_bytes(file.read())
                if text:
                    chunks.append(f"# {info.filename}\n{text[:12000]}")
                if sum(len(item) for item in chunks) > 30000:
                    break
        return "\n\n".join(chunks)[:30000]

    @staticmethod
    def _trusted_source_type(url: str) -> str:
        host = (urlparse(url).netloc or "").casefold()
        if "docs.google.com" in host:
            return "google_doc"
        if "notion.so" in host or "notion.site" in host:
            return "notion"
        if "youtube.com" in host or "youtu.be" in host:
            return "youtube"
        return "web"

    def _fetch_trusted_revision_source(self, url: str, *, source_type: str) -> dict:
        base = {"url": url, "type": source_type, "status": "unavailable", "title": "", "text": "", "message": ""}
        try:
            if source_type == "google_doc":
                text = self._fetch_google_doc_text(url)
                return {**base, "status": "fetched" if text else "unavailable", "text": text, "message": "" if text else "Google Doc text is not publicly readable from the server."}
            if source_type == "youtube":
                return {**base, **self._fetch_youtube_source(url)}
            text = self._fetch_public_page_text(url)
            return {**base, "status": "fetched" if text else "unavailable", "text": text, "message": "" if text else "Page text is not publicly readable from the server."}
        except Exception as exc:
            return {**base, "message": f"Source fetch failed: {exc}"}

    @staticmethod
    def _fetch_google_doc_text(url: str) -> str:
        match = re.search(r"/document/d/([^/]+)", url)
        if not match:
            return ""
        export_url = f"https://docs.google.com/document/d/{match.group(1)}/export?format=txt"
        response = requests.get(export_url, timeout=30)
        if response.status_code >= 400:
            return ""
        return TelegramBotFacade._clean_source_text(response.text)

    @staticmethod
    def _fetch_youtube_source(url: str) -> dict:
        video_id = TelegramBotFacade._youtube_video_id(url)
        title = ""
        try:
            oembed = requests.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
                timeout=20,
            )
            if oembed.status_code < 400:
                title = str((oembed.json() or {}).get("title") or "")
        except Exception:
            title = ""
        if not video_id:
            return {"status": "unavailable", "title": title, "text": "", "message": "Could not parse YouTube video id."}
        text = TelegramBotFacade._fetch_youtube_captions(video_id)
        if text:
            return {"status": "fetched", "title": title, "text": text, "message": ""}
        return {
            "status": "metadata_only",
            "title": title,
            "text": f"YouTube video: {title or video_id}",
            "message": "Captions/transcript are not publicly available to the server.",
        }

    @staticmethod
    def _youtube_video_id(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.netloc or "").casefold()
        if "youtu.be" in host:
            return parsed.path.strip("/").split("/")[0]
        query_id = (parse_qs(parsed.query).get("v") or [""])[0]
        if query_id:
            return query_id
        match = re.search(r"/(?:embed|shorts)/([^/?#]+)", parsed.path)
        return match.group(1) if match else ""

    @staticmethod
    def _fetch_youtube_captions(video_id: str) -> str:
        languages = ["ru", "uk", "en"]
        for lang in languages:
            response = requests.get(
                "https://www.youtube.com/api/timedtext",
                params={"v": video_id, "lang": lang, "fmt": "vtt"},
                timeout=20,
            )
            if response.status_code < 400 and response.text.strip():
                return TelegramBotFacade._clean_youtube_caption_text(response.text)
        return ""

    @staticmethod
    def _fetch_public_page_text(url: str) -> str:
        response = requests.get(url, timeout=30, headers={"User-Agent": "meeting-digest-bot/1.0"})
        if response.status_code >= 400:
            return ""
        return TelegramBotFacade._clean_source_text(response.text)

    @staticmethod
    def _clean_source_text(text: str) -> str:
        text = html.unescape(text or "")
        text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:30000]

    @staticmethod
    def _clean_youtube_caption_text(text: str) -> str:
        lines = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped == "WEBVTT" or "-->" in stripped or stripped.isdigit():
                continue
            lines.append(re.sub(r"<[^>]+>", "", html.unescape(stripped)))
        return TelegramBotFacade._clean_source_text(" ".join(lines))

    @staticmethod
    def _parse_json_object(content: str) -> dict | None:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                parsed = json.loads(text[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
        return None

    @staticmethod
    def _fallback_instruction_summary(query: str, trusted_sources: list[dict] | None = None) -> str:
        fetched_sources = [item for item in (trusted_sources or []) if isinstance(item, dict) and item.get("status") == "fetched"]
        if fetched_sources:
            source = fetched_sources[0]
            title = str(source.get("title") or source.get("url") or "проверенного источника").strip()
            text = re.sub(r"\s+", " ", str(source.get("text") or "")).strip()
            if text:
                return f"Нужно обновить знание по проверенному источнику: {title}. Ключевое содержание источника будет учтено как правило для связанных объектов базы знаний."
        cleaned = re.sub(r"\s+", " ", query).strip()
        return cleaned[:700] if cleaned else "Не удалось надежно извлечь смысл правки; лучше переформулировать запрос."

    @staticmethod
    def _filter_revision_replacements(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        command_terms = {
            "kb",
            "ask",
            "правка",
            "правки",
            "исправь",
            "исправить",
            "исправление",
            "обнови",
            "обновить",
            "обнови знание",
            "знание",
            "знания",
            "база",
            "база знаний",
            "инструкция",
            "инструкцию",
            "проверенная инструкция",
            "по проверенной инструкции",
            "корректировка",
            "замени",
            "заменить",
            "учти",
            "добавь",
            "создай",
            "прими",
            "replace",
            "correction",
            "update knowledge",
        }
        seen: set[tuple[str, str]] = set()
        filtered: list[tuple[str, str]] = []

        def normalize(value: str) -> str:
            return re.sub(r"\s+", " ", value.strip(" `\"'«».,;:()[]{}")).casefold()

        for old, new in items:
            old = str(old or "").strip(" `\"'«».,;:()[]{}")
            new = str(new or "").strip(" `\"'«».,;:()[]{}")
            old_norm = normalize(old)
            new_norm = normalize(new)
            if not old_norm or not new_norm or old_norm == new_norm:
                continue
            if old_norm in command_terms or new_norm in command_terms:
                continue
            if len(old_norm) < 2 or len(new_norm) < 2:
                continue
            if len(old_norm.split()) > 8 or len(new_norm.split()) > 8:
                continue
            key = (old_norm, new_norm)
            if key in seen:
                continue
            seen.add(key)
            filtered.append((old, new))
        return filtered

    @staticmethod
    def _extract_term_replacements(query: str) -> list[tuple[str, str]]:
        replacements: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(old: str, new: str) -> None:
            old = old.strip(" `\"'«»")
            new = new.strip(" `\"'«».,;:")
            if not old or not new or old.casefold() == new.casefold():
                return
            key = (old.casefold(), new.casefold())
            if key not in seen:
                seen.add(key)
                replacements.append((old, new))

        for match in re.finditer(r"(?P<new>[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_.-]+)\s*\([^)]*вместо\s+[\"«](?P<old>[^\"»]+)[\"»][^)]*\)", query, flags=re.IGNORECASE):
            add(match.group("old"), match.group("new"))
        for match in re.finditer(r"[\"«](?P<old>[^\"»]+)[\"»]\s*(?:->|=>|на)\s*[\"«]?(?P<new>[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_.-]+)[\"»]?", query, flags=re.IGNORECASE):
            add(match.group("old"), match.group("new"))
        for match in re.finditer(r"замени(?:ть)?\s+[\"«]?(?P<old>[^\"»]+?)[\"»]?\s+на\s+[\"«]?(?P<new>[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_.-]+)[\"»]?(?:[\s,.;)]|$)", query, flags=re.IGNORECASE):
            add(match.group("old"), match.group("new"))
        return replacements

    def _affected_revision_object_ids(
        self,
        repo: KnowledgeRepository,
        *,
        query: str,
        source_ids: list[str],
        replacements: list[tuple[str, str]],
    ) -> list[str]:
        limit = int(os.environ.get("KNOWLEDGE_REVISION_BATCH_LIMIT", "8"))
        scores: dict[str, int] = {}
        old_terms = [old for old, _new in replacements]
        query_terms = [term.casefold() for term in re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]{3,}", query)]
        for path in repo._knowledge_json_paths():  # Repository-local review helper; keeps Telegram UX source-grounded.
            data = repo._read_json(path)
            object_id = str(data.get("object_id") or path.stem)
            text = repo._index_text(data)
            folded = text.casefold()
            score = 0
            for old in old_terms:
                if old.casefold() in folded:
                    score += 100
            if object_id in source_ids:
                score += 20
            score += sum(1 for term in query_terms if term in folded)
            if score > 0:
                scores[object_id] = score
        for object_id in source_ids:
            scores.setdefault(object_id, 10)
        return [object_id for object_id, _score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]]

    @staticmethod
    def _knowledge_object_label(repo: KnowledgeRepository, object_id: str) -> str:
        path = repo._canonical_object_path(object_id)
        if not path:
            return f"`{object_id}`"
        data = repo._read_json(path)
        title = str(data.get("title") or object_id)
        object_type = str(data.get("object_type") or path.parent.name)
        return f"{title} (`{object_id}`, {object_type})"

    def _revision_impact_preview(self, repo: KnowledgeRepository, *, metadata_path: Path) -> str:
        data = repo._read_json(metadata_path)
        object_id = str(data.get("object_id") or "")
        correction = str(data.get("correction") or "")
        instruction_summary = str(data.get("instruction_summary") or "").strip()
        replacements = [item for item in data.get("replacements") or [] if isinstance(item, dict)]
        lines = [
            "Эффект правки:",
            f"- Объект: {self._knowledge_object_label(repo, object_id)}",
            f"- Статус: {data.get('status') or 'draft'}",
            "",
            "Как я понял инструкцию:",
            instruction_summary or self._fallback_instruction_summary(correction, data.get("trusted_sources") or []),
            "",
            "Как изменятся ответы:",
        ]
        if replacements:
            for item in replacements:
                old = str(item.get("old") or "")
                new = str(item.get("new") or "")
                if old and new:
                    lines.append(f"- В ответах и инструкциях `{old}` будет заменено на `{new}`.")
            lines.append("- После применения индекс RAG пересоберется, поэтому поиск начнет находить нормализованные термины.")
        else:
            lines.append("- Корректировка будет добавлена в историю объекта и учтена при следующем пересборе связанных инструкций.")
        related = self._affected_revision_object_ids(
            repo,
            query=correction,
            source_ids=[object_id],
            replacements=[(str(item.get("old") or ""), str(item.get("new") or "")) for item in replacements],
        )
        instruction_ids = [item for item in related if item.startswith("instruction__")]
        if instruction_ids:
            lines.extend(["", "Связанные инструкции:"])
            lines.extend(f"- {self._knowledge_object_label(repo, item)}" for item in instruction_ids[:5])
        lines.extend(["", "Исходная корректировка:", correction])
        return "\n".join(lines)[:3900]

    def _revision_batch_impact_preview(self, repo: KnowledgeRepository, *, proposals: list[dict]) -> str:
        metadata_paths = [Path(str(item.get("_metadata_path") or item.get("metadata_path") or "")) for item in proposals]
        loaded = [repo._read_json(path) for path in metadata_paths if str(path) and path.exists()]
        loaded = [item for item in loaded if item]
        replacements: list[dict] = []
        seen_replacements: set[tuple[str, str]] = set()
        object_ids: list[str] = []
        correction = ""
        instruction_summary = ""
        trusted_sources: list[dict] = []
        seen_source_urls: set[str] = set()
        for item in loaded:
            object_id = str(item.get("object_id") or "")
            if object_id:
                object_ids.append(object_id)
            correction = correction or str(item.get("correction") or "")
            instruction_summary = instruction_summary or str(item.get("instruction_summary") or "").strip()
            for source in item.get("trusted_sources") or []:
                if not isinstance(source, dict):
                    continue
                source_key = str(source.get("url") or source.get("title") or "")
                if source_key and source_key not in seen_source_urls:
                    seen_source_urls.add(source_key)
                    trusted_sources.append(source)
            for replacement in item.get("replacements") or []:
                if not isinstance(replacement, dict):
                    continue
                old = str(replacement.get("old") or "")
                new = str(replacement.get("new") or "")
                key = (old.casefold(), new.casefold())
                if old and new and key not in seen_replacements:
                    seen_replacements.add(key)
                    replacements.append({"old": old, "new": new})
        lines = [f"Эффект правки: будет затронуто объектов: {len(object_ids)}."]
        if trusted_sources:
            lines.extend(["", "Проверенные источники учтены:"])
            for source in trusted_sources[:5]:
                label = source.get("title") or source.get("url") or source.get("type")
                lines.append(f"- {label} ({source.get('type')}, {source.get('status')})")
        lines.extend(
            [
                "",
                "Как я понял инструкцию:",
                instruction_summary or self._fallback_instruction_summary(correction, trusted_sources),
            ]
        )
        if replacements:
            lines.extend(["", "Что изменится в ответах и инструкциях:"])
            lines.extend(f"- `{item['old']}` будет заменено на `{item['new']}`." for item in replacements)
            lines.append("- После применения база пересоберет markdown, chunk index и RAG index.")
        else:
            lines.extend(["", "Что изменится:"])
            lines.append("- Корректировка будет добавлена в связанные knowledge objects и учтена при пересборке базы.")
        instruction_ids = [object_id for object_id in object_ids if object_id.startswith("instruction__")]
        task_case_count = len([object_id for object_id in object_ids if object_id.startswith("task_case__")])
        feature_count = len([object_id for object_id in object_ids if object_id.startswith("feature__")])
        system_count = len([object_id for object_id in object_ids if object_id.startswith("system__")])
        lines.extend(
            [
                "",
                "Где будет применено:",
                f"- task cases: {task_case_count}",
                f"- features: {feature_count}",
                f"- systems: {system_count}",
                f"- instructions: {len(instruction_ids)}",
            ]
        )
        if instruction_ids:
            lines.extend(["", "Связанные инструкции:"])
            lines.extend(f"- {self._knowledge_object_label(repo, object_id)}" for object_id in instruction_ids[:5])
        if correction:
            lines.extend(["", "Исходная корректировка:", correction])
        return "\n".join(lines)[:3900]

    @staticmethod
    def _rebuild_knowledge_indexes(repo: KnowledgeRepository) -> None:
        repo.build_index()
        repo.build_chunk_index()
        rag_client = client_from_env(dict(os.environ))
        if rag_client:
            KnowledgeVectorStore(
                repo.root,
                db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
                embeddings_model=rag_client.embeddings_model,
            ).build(client=rag_client, force=True)

    @classmethod
    def _should_handle_knowledge_ai(cls, text: str, *, message: dict) -> bool:
        cleaned = cls._strip_bot_mention(text).strip()
        lowered = cleaned.casefold()
        if BOT_MENTION_RE.search(text) and not cleaned:
            return True
        if cls._should_defer_to_meeting_sync(text, message=message):
            return False
        if BOT_MENTION_RE.search(text) and lowered.startswith(("ask ", "спросить ", "вопрос ")):
            return True
        if message.get("voice") or message.get("audio"):
            return True
        reply = message.get("reply_to_message") or {}
        if BOT_MENTION_RE.search(text) and (reply.get("voice") or reply.get("audio")):
            return True
        if BOT_MENTION_RE.search(text) and any(
            marker in lowered
            for marker in [
                "ask",
                "health",
                "status",
                "spec",
                "instruction",
                "база",
                "знани",
                "инструкц",
                "спецификац",
                "notebook",
                "rag",
                "proposal",
                "предлож",
                "как работает",
                "сформируй",
                "экспорт",
                "export",
            ]
        ):
            return True
        if BOT_MENTION_RE.search(text):
            operational_command = (
                extract_post_link(text)
                or DAY_COMMAND_RE.search(text)
                or WEEK_COMMAND_RE.search(text)
                or REPORT_COMMAND_RE.search(text)
                or WEEKLY_REPORT_COMMAND_RE.search(text)
                or cls._is_register_command(text)
            )
            return not bool(operational_command)
        return cleaned.startswith(("?", "kb?"))

    @classmethod
    def _should_defer_to_meeting_sync(cls, text: str, *, message: dict) -> bool:
        if not BOT_MENTION_RE.search(text):
            return False
        action = cls._task_extractor_action(text)
        if action not in {
            TaskExtractorAction.preview,
            TaskExtractorAction.create,
            TaskExtractorAction.update,
            TaskExtractorAction.comment,
            TaskExtractorAction.checklist,
        }:
            return False
        cleaned = cls._strip_bot_mention(text).strip()
        first_token = cleaned.casefold().lstrip("/").split(maxsplit=1)[0] if cleaned.split() else ""
        reply = message.get("reply_to_message") or {}
        reply_text = cls._normalize_text(reply.get("text") or reply.get("caption") or "")
        return bool(
            extract_post_link(text)
            or extract_task_id(text)
            or reply_text
            or first_token
            in {
                "preview",
                "create",
                "new",
                "update",
                "replace",
                "comment",
                "checklist",
                "предпросмотр",
                "показать",
                "проверить",
                "создать",
                "новая",
                "новую",
                "обновить",
                "заменить",
                "коммент",
                "комментарий",
                "чеклист",
                "чек-лист",
            }
        )

    @classmethod
    def _is_mention_only(cls, text: str) -> bool:
        return not cls._strip_bot_mention(text).strip()

    @staticmethod
    def _classify_knowledge_intent(query: str) -> str:
        lowered = query.casefold()
        if not lowered or lowered in {"kb", "база знаний", "knowledge"}:
            return "menu"
        if any(marker in lowered for marker in ["health", "статус", "здоров", "состояние"]):
            return "health"
        if any(marker in lowered for marker in ["proposal", "предлож", "правк", "изменен"]):
            if any(marker in lowered for marker in ["невер", "исправ", "обнов", "скоррект", "работает не", "wrong"]):
                return "revise_knowledge"
            return "proposals"
        if any(marker in lowered for marker in ["notebook", "bundle", "архив", "zip", "external ai", "экспорт", "export"]):
            return "export_bundle"
        if any(marker in lowered for marker in ["инструкц", "гайд", "guide", "manual", "как пользоваться", "пошаг"]):
            return "generate_instruction"
        if any(marker in lowered for marker in ["тз", "spec", "спецификац", "acceptance", "критери", "implementation", "техничес"]):
            return "generate_spec"
        if any(marker in lowered for marker in ["невер", "исправ", "скоррект", "обнови знание", "wrong"]):
            return "revise_knowledge"
        return "ask"

    @staticmethod
    def _voice_reply_action(cleaned_text: str) -> str:
        lowered = cleaned_text.casefold().strip()
        if lowered in {"", "ask", "спросить", "вопрос", "задать вопрос"}:
            return "ask"
        if lowered in {"instruction", "инструкция", "сделай инструкцию"}:
            return "сформируй инструкцию по:"
        if lowered in {"spec", "tz", "тз", "техзадание"}:
            return "сформируй ТЗ по:"
        if lowered in {"исправь", "исправь знание", "правка", "скорректируй"}:
            return "исправь знание:"
        if lowered in {"export", "экспорт"}:
            return "собери экспорт:"
        return cleaned_text if lowered.startswith(("ask ", "спроси ", "вопрос ")) else ""

    @staticmethod
    def _strip_leading_kb_action(query: str) -> str:
        cleaned = query.strip()
        lowered = cleaned.casefold()
        for prefix in ("ask ", "спросить ", "вопрос "):
            if lowered.startswith(prefix):
                return cleaned[len(prefix) :].strip()
        return cleaned

    @staticmethod
    def _knowledge_session_key(chat_id: int | str | None, user_id: int | str | None) -> str:
        return f"{chat_id or '-'}:{user_id or '-'}"

    @staticmethod
    def _chat_allowed(env_name: str, chat_id: int | str | None) -> bool:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            return True
        if chat_id is None:
            return False
        allowed = {item.strip() for item in re.split(r"[,;\s]+", raw) if item.strip()}
        return str(chat_id).strip() in allowed

    def _remember_knowledge_context(self, chat_id: int | str | None, user_id: int | str | None, response: TelegramResponse) -> None:
        if not chat_id:
            return
        key = self._knowledge_session_key(chat_id, user_id)
        session = self._knowledge_sessions.setdefault(key, {})
        payload = response.payload or {}
        if payload.get("intent") == "export_bundle":
            return
        if payload.get("query"):
            session["query"] = str(payload["query"])
        if payload.get("answer_mode"):
            session["answer_mode"] = str(payload["answer_mode"])
        if payload.get("notebooklm_prompt_path"):
            session["notebooklm_prompt_path"] = str(payload["notebooklm_prompt_path"])
        if payload.get("intent") in {"ask", "instruction", "generate_instruction", "spec", "generate_spec", "support"}:
            session["last_answer"] = response.text
        proposal = payload.get("proposal") or {}
        if proposal.get("metadata_path"):
            self._proposal_refs[key] = [str(proposal["metadata_path"])]
            session["last_proposal"] = str(proposal["metadata_path"])
            session["query"] = str(payload.get("query") or proposal.get("correction") or session.get("query") or "")
        proposals = payload.get("proposals") or []
        if proposals:
            self._proposal_refs[key] = [str(item.get("_metadata_path") or item.get("metadata_path") or "") for item in proposals]

    def _query_from_session(self, *, session_key: str, action: str, fallback: str) -> str:
        session = self._knowledge_sessions.get(session_key) or {}
        if action in {"instruction", "spec", "export"} and session.get("query"):
            return str(session["query"])
        return fallback.strip()

    def _keyboard_for_response(self, response: TelegramResponse, *, chat_id: int | str | None, user_id: int | str | None) -> dict:
        payload = response.payload or {}
        key = self._knowledge_session_key(chat_id, user_id)
        if payload.get("intent") == "export_bundle":
            return {}
        if payload.get("proposals"):
            return self._proposal_review_keyboard(payload.get("proposals") or [], session_key=key)
        if payload.get("intent") == "revise_knowledge" and (payload.get("proposal") or {}).get("metadata_path"):
            return self._single_proposal_keyboard()
        if payload.get("intent") == "proposal_action" and payload.get("metadata_path"):
            return self._single_proposal_keyboard(int(payload.get("proposal_index") or 0))
        return self._knowledge_action_keyboard()

    def _proposal_review_keyboard(self, proposals: list[dict], *, session_key: str) -> dict:
        refs: list[str] = []
        rows: list[list[dict[str, str]]] = []
        for item in proposals:
            metadata_path = str(item.get("_metadata_path") or item.get("metadata_path") or "")
            if metadata_path:
                refs.append(metadata_path)
        self._proposal_refs[session_key] = refs
        if len(refs) > 1:
            rows.append(
                [
                    {"text": "Применить все", "callback_data": "kb:proposal:apply_all:0"},
                    {"text": "Отклонить все", "callback_data": "kb:proposal:reject_all:0"},
                ]
            )
        elif refs:
            rows.extend(
                [
                    [
                        {"text": "Показать эффект", "callback_data": "kb:proposal:show:0"},
                        {"text": "Применить", "callback_data": "kb:proposal:apply:0"},
                    ],
                    [{"text": "Отклонить", "callback_data": "kb:proposal:reject:0"}],
                ]
            )
        rows.append([{"text": "Назад", "callback_data": "kb:menu"}])
        return {"inline_keyboard": rows}

    @staticmethod
    def _single_proposal_keyboard(index: int = 0) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "Показать эффект", "callback_data": f"kb:proposal:show:{index}"},
                    {"text": "Принять", "callback_data": f"kb:proposal:approve:{index}"},
                ],
                [
                    {"text": "Применить", "callback_data": f"kb:proposal:apply:{index}"},
                    {"text": "Отклонить", "callback_data": f"kb:proposal:reject:{index}"},
                ],
                [{"text": "Назад", "callback_data": "kb:menu"}],
            ]
        }

    def _process_proposal_callback(self, action: str, *, session_key: str) -> TelegramResponse:
        parts = action.split(":")
        operation = parts[1] if len(parts) > 1 else "list"
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        repo = KnowledgeRepository(Path(os.environ.get("KNOWLEDGE_REPO_PATH", "company-knowledge")))
        refs = self._proposal_refs.get(session_key) or []
        if operation in {"apply_all", "reject_all"}:
            if not refs:
                return self._run_knowledge_intent("proposals", "")
            if operation == "reject_all":
                rejected = []
                for ref in refs:
                    proposal = repo.set_revision_status(metadata_path=Path(ref), status="rejected")
                    rejected.append(proposal.object_id)
                return TelegramResponse(
                    ok=True,
                    text="Отклонил правки:\n" + "\n".join(f"- `{object_id}`" for object_id in rejected[:10]),
                    payload={"intent": "proposal_action", "query": str((self._knowledge_sessions.get(session_key) or {}).get("query") or "")},
                )
            applied = []
            query = str((self._knowledge_sessions.get(session_key) or {}).get("query") or "")
            for ref in refs:
                metadata_path = Path(ref)
                data = repo._read_json(metadata_path)
                if data.get("status") != "approved":
                    repo.set_revision_status(metadata_path=metadata_path, status="approved")
                proposal = repo.apply_resolved_revision(metadata_path=metadata_path)
                applied.append(proposal.object_id)
                query = query or proposal.correction or proposal.object_id
            self._rebuild_knowledge_indexes(repo)
            answer = self._answer_from_knowledge(repo, query or "что изменилось после правки", answer_mode="general")
            text = "Применил правки ко связанным объектам:\n" + "\n".join(f"- `{object_id}`" for object_id in applied[:10])
            text += "\n\nКак теперь будет отвечать база:\n" + str(answer.get("answer") or "").strip()
            return TelegramResponse(ok=True, text=text[:3900], payload={"intent": "proposal_action", "query": query, "applied_count": len(applied)})
        metadata_path = Path(refs[index]) if index < len(refs) and refs[index] else None
        if not metadata_path:
            return self._run_knowledge_intent("proposals", "")
        if operation == "show":
            preview = self._revision_impact_preview(repo, metadata_path=metadata_path)
            return TelegramResponse(
                ok=True,
                text=preview,
                payload={"intent": "proposal_action", "metadata_path": str(metadata_path), "proposal_index": index},
            )
        if operation in {"approve", "reject"}:
            status = "approved" if operation == "approve" else "rejected"
            proposal = repo.set_revision_status(metadata_path=metadata_path, status=status)
            label = "принята" if status == "approved" else "отклонена"
            return TelegramResponse(
                ok=True,
                text=f"Правка `{proposal.object_id}` {label}.",
                payload={
                    "intent": "proposal_action",
                    "query": proposal.correction,
                    "metadata_path": str(metadata_path),
                    "proposal_index": index,
                    "proposal": proposal.model_dump(),
                },
            )
        if operation == "apply":
            data = repo._read_json(metadata_path)  # Reuse repository metadata format for a short Telegram action.
            if data.get("status") != "approved":
                repo.set_revision_status(metadata_path=metadata_path, status="approved")
            proposal = repo.apply_resolved_revision(metadata_path=metadata_path)
            self._rebuild_knowledge_indexes(repo)
            query = proposal.correction or str((self._knowledge_sessions.get(session_key) or {}).get("query") or proposal.object_id)
            answer = self._answer_from_knowledge(repo, query, answer_mode="general")
            text = "Правка применена.\n\nКак теперь работает функционал:\n" + str(answer.get("answer") or "").strip()
            return TelegramResponse(
                ok=True,
                text=text[:3900],
                payload={
                    "intent": "proposal_action",
                    "query": query,
                    "metadata_path": str(metadata_path),
                    "proposal_index": index,
                    "proposal": proposal.model_dump(),
                },
            )
        return self._run_knowledge_intent("proposals", "")

    def _process_notebooklm_callback(
        self,
        *,
        session_key: str,
        chat_id: int | str | None = None,
        reply_to_message_id: int | str | None = None,
    ) -> TelegramResponse:
        session = self._knowledge_sessions.get(session_key) or {}
        query = str(session.get("query") or "").strip()
        answer = str(session.get("last_answer") or "").strip()
        answer_mode = str(session.get("answer_mode") or "general")
        if not query:
            return TelegramResponse(
                ok=False,
                text="Не вижу последнего вопроса для NotebookLM. Сначала задайте вопрос по базе знаний.",
                payload={"intent": "notebooklm_check"},
            )
        prompt_path = self._queue_notebooklm_followup(
            query=query,
            answer=answer,
            sources=[],
            answer_mode=answer_mode,
            telegram_chat_id=chat_id,
            telegram_reply_to_message_id=reply_to_message_id,
        )
        if not prompt_path:
            return TelegramResponse(
                ok=False,
                text="NotebookLM очередь не настроена или проект недоступен. Проверьте серверный watcher.",
                payload={"intent": "notebooklm_check", "query": query},
            )
        return TelegramResponse(
            ok=True,
            text="NotebookLM проверка отправлена в фоновую очередь. Агент откроет блокнот и задаст уточняющий промт.",
            payload={"intent": "notebooklm_check", "query": query, "notebooklm_prompt_path": prompt_path},
        )

    @staticmethod
    def _queue_notebooklm_followup(
        *,
        query: str,
        answer: str,
        sources: list[dict],
        answer_mode: str,
        telegram_chat_id: int | str | None = None,
        telegram_reply_to_message_id: int | str | None = None,
    ) -> str:
        query = str(query or "").strip()
        answer = str(answer or "").strip()
        if not query or len(query) < 3:
            return ""
        exports_root = os.environ.get("KNOWLEDGE_NOTEBOOKLM_EXPORTS_ROOT")
        if not exports_root:
            return ""
        session_id = os.environ.get("KNOWLEDGE_NOTEBOOKLM_SESSION_ID") or "company-knowledge"
        source_lines = [
            f"- {item.get('object_id')} / {item.get('chunk_id') or item.get('score')}"
            for item in sources[:8]
        ]
        prompt = "\n".join(
            [
                "Проверь и дополни ответ по базе знаний как внешний исследовательский слой NotebookLM.",
                "",
                f"Режим ответа: {answer_mode}",
                "",
                "Вопрос пользователя:",
                query,
                "",
                "Ответ RAG:",
                answer,
                "",
                "Источники RAG:",
                "\n".join(source_lines) if source_lines else "- нет источников",
                "",
                "Верни:",
                "1. что подтверждается источниками;",
                "2. какие детали стоит добавить;",
                "3. есть ли противоречия;",
                "4. какие canonical objects нужно обновить, если вопрос содержит корректировку.",
            ]
        )
        try:
            path = NotebookLMAgent(exports_root=Path(exports_root)).queue_prompt(
                session_id=session_id,
                prompt=prompt,
                kind="rag_followup",
                metadata={
                    "query": query,
                    "rag_answer": answer,
                    "answer_mode": answer_mode,
                    "sources": sources[:8],
                    "telegram_chat_id": telegram_chat_id,
                    "telegram_reply_to_message_id": telegram_reply_to_message_id,
                    "delivery": "telegram_synthesis" if telegram_chat_id else "store_only",
                },
            )
            return str(path)
        except Exception as exc:
            print(f"NotebookLM prompt queue failed: {exc}")
            return ""

    @staticmethod
    def _knowledge_menu_text() -> str:
        return (
            "База знаний: выберите действие кнопкой или напишите вопрос обычным текстом.\n\n"
            "Лучший сценарий: сначала спросите, как работает функциональность, затем нажмите «Инструкция» или «ТЗ» под ответом."
        )

    @staticmethod
    def _knowledge_action_keyboard() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "Спросить", "callback_data": "kb:ask"},
                    {"text": "Инструкция", "callback_data": "kb:instruction"},
                    {"text": "ТЗ", "callback_data": "kb:spec"},
                ],
                [
                    {"text": "Экспорт", "callback_data": "kb:export"},
                    {"text": "Правки", "callback_data": "kb:proposals"},
                    {"text": "Статус", "callback_data": "kb:health"},
                ],
                [
                    {"text": "NotebookLM проверка", "callback_data": "kb:notebooklm"},
                ],
            ]
        }

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = requests.post(
            self.api_url + "sendMessage",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def send_document(self, chat_id: int | str, path: str, *, caption: str = "") -> dict:
        with open(path, "rb") as handle:
            response = requests.post(
                self.api_url + "sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"document": (Path(path).name, handle)},
                timeout=120,
            )
        response.raise_for_status()
        return response.json()

    def _answer_callback_query(self, callback_query_id: str) -> None:
        try:
            requests.post(self.api_url + "answerCallbackQuery", json={"callback_query_id": callback_query_id}, timeout=10)
        except Exception:
            return

    def _transcribe_telegram_audio(self, message: dict) -> str:
        media = message.get("voice") or message.get("audio") or {}
        file_id = media.get("file_id")
        if not file_id:
            return ""
        try:
            file_info = requests.get(self.api_url + "getFile", params={"file_id": file_id}, timeout=30)
            file_info.raise_for_status()
            file_path = ((file_info.json().get("result") or {}).get("file_path") or "").strip()
            if not file_path:
                return ""
            download = requests.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=120)
            download.raise_for_status()
            return self._transcribe_audio_bytes(download.content, filename=Path(file_path).name)
        except Exception as exc:
            print(f"Telegram voice transcription failed: {exc}")
            return ""

    @staticmethod
    def _transcribe_audio_bytes(content: bytes, *, filename: str) -> str:
        api_key = os.environ.get("KNOWLEDGE_RAG_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
        if not api_key or not content:
            return ""
        base_url = os.environ.get("KNOWLEDGE_RAG_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1"
        model = os.environ.get("KNOWLEDGE_TRANSCRIPTION_MODEL") or os.environ.get("OPENAI_TRANSCRIPTION_MODEL") or "whisper-1"
        suffix = Path(filename or "voice.ogg").suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(content)
            handle.flush()
            with open(handle.name, "rb") as audio:
                response = requests.post(
                    base_url.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": model, "response_format": "json"},
                    files={"file": (Path(filename or handle.name).name, audio)},
                    timeout=180,
                )
        response.raise_for_status()
        data = response.json()
        return str(data.get("text") or "").strip()

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

    def _sync_post_command(self, command: TelegramCommand, *, action: SyncAction, message: dict) -> object:
        request = PostSyncRequest(
            post_url=command.post_url or "",
            action=action,
            task_id=command.task_id,
        )
        try:
            return self.service.sync_post(request)
        except ValueError as exc:
            error_message = str(exc)
            if "Публикация не зарегистрирована" not in error_message and "register endpoint" not in error_message:
                raise
            record, error = self._register_publication_record_from_reply(message, post_url=command.post_url)
            if not record:
                raise ValueError(f"{exc} Авто-регистрация из reply не удалась: {error}") from exc
            result = self.service.sync_post(request)
            result.details["auto_registered_publication"] = True
            result.details["registered_post_url"] = record.post_url
            result.details["registered_loom_video_id"] = record.loom_video_id
            return result

    def _register_publication_from_reply(self, message: dict) -> TelegramResponse:
        record, error = self._register_publication_record_from_reply(message)
        if not record:
            return TelegramResponse(
                ok=False,
                text=error or (
                    "Для регистрации старого поста ответьте командой "
                    "`@LLMeets_bot зарегистрировать` именно на сообщение с Loom-дайджестом."
                ),
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

    def _register_publication_record_from_reply(self, message: dict, *, post_url: str | None = None):
        reply = message.get("reply_to_message") or {}
        reply_text = self._normalize_text(reply.get("text") or reply.get("caption") or "")
        if not reply_text:
            return None, (
                "Для регистрации старого поста ответьте командой "
                "`@LLMeets_bot зарегистрировать` именно на сообщение с Loom-дайджестом."
            )

        resolved_post_url = post_url or self._post_url_from_reply(message)
        metadata = self._extract_publication_metadata(reply_text)
        if not resolved_post_url:
            return None, "Не удалось определить ссылку на пост из reply_to_message."
        if not metadata.get("loom_video_id"):
            return None, (
                "Не нашел Loom-ссылку или Loom video ID в тексте старого поста. "
                "Ответьте командой на сам digest-пост, где есть строка Loom."
            )

        record = self.service.register_publication(
            PublicationRegistrationRequest(
                post_url=resolved_post_url,
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
        return record, None

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

    @staticmethod
    def _task_extractor_action(text: str) -> TaskExtractorAction | None:
        cleaned = TASK_EXTRACTOR_MENTION_RE.sub(" ", text or "")
        cleaned = BOT_MENTION_RE.sub(" ", cleaned)
        cleaned = f" {cleaned.strip().lower()} "
        mapping = [
            (TaskExtractorAction.collect, [" collect ", " /collect ", " собрать ", " /собрать "]),
            (TaskExtractorAction.add, [" add ", " /add ", " добавить ", " /добавить "]),
            (TaskExtractorAction.preview, [" preview ", " /preview ", " предпросмотр ", " /предпросмотр ", " показать ", " /показать ", " проверить ", " /проверить "]),
            (TaskExtractorAction.export, [" export ", " /export ", " выгрузка ", " /выгрузка "]),
            (TaskExtractorAction.create, [" create ", " /create ", " new ", " /new ", " создать ", " /создать ", " новая ", " /новая ", " новую ", " /новую "]),
            (TaskExtractorAction.update, [" update ", " /update ", " replace ", " /replace ", " обновить ", " /обновить ", " заменить ", " /заменить "]),
            (TaskExtractorAction.comment, [" comment ", " /comment ", " коммент ", " /коммент ", " комментарий ", " /комментарий ", " комментарии ", " /комментарии "]),
            (TaskExtractorAction.checklist, [" checklist ", " /checklist ", " чеклист ", " /чеклист ", " чек-лист ", " /чек-лист "]),
            (TaskExtractorAction.clear, [" clear ", " /clear ", " очистить ", " /очистить "]),
            (TaskExtractorAction.status, [" status ", " /status ", " статус ", " /статус "]),
        ]
        for action, markers in mapping:
            if any(marker in cleaned for marker in markers):
                return action
        return None

    @classmethod
    def _is_task_extractor_context(cls, text: str, *, message: dict) -> bool:
        if extract_post_link(text) or extract_task_id(text) or cls._looks_like_task_extractor_source(text):
            return True
        reply = message.get("reply_to_message") or {}
        reply_text = cls._normalize_text(reply.get("text") or reply.get("caption") or "")
        if not reply_text:
            return False
        return bool(
            extract_post_link(reply_text)
            or cls._looks_like_task_extractor_source(reply_text)
            or re.search(r"\b(?:loom|встреча|meeting|#daily|#task_discussion|#task_demo)\b", reply_text, re.IGNORECASE)
        )

    @staticmethod
    def _looks_like_task_extractor_source(text: str) -> bool:
        cleaned = text or ""
        return bool(
            re.search(r"https?://", cleaned, re.IGNORECASE)
            or re.search(r"\b(?:task|задач[аиеуы]?)\s*#?\d{3,}\b", cleaned, re.IGNORECASE)
            or len(cleaned.strip()) > 80
        )

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
