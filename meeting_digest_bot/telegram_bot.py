from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

import requests

from .knowledge_alerts import read_knowledge_alert_chat_id, write_knowledge_alert_chat_id
from .knowledge_rag import KnowledgeVectorStore, client_from_env
from .knowledge_repo import KnowledgeRepository
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
        callback_response = self._process_callback_query(update.get("callback_query") or {})
        if callback_response:
            return callback_response

        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = self._normalize_text(message.get("text") or message.get("caption") or "")
        if not text and (message.get("voice") or message.get("audio")):
            text = self._transcribe_telegram_audio(message)
        if BOT_MENTION_RE.search(text) and self._is_mention_only(text):
            reply = message.get("reply_to_message") or {}
            if reply.get("voice") or reply.get("audio"):
                transcribed = self._transcribe_telegram_audio(reply)
                if transcribed:
                    text = transcribed
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

        kb_response = self._process_kb_command(text)
        if kb_response:
            if chat_id:
                self.send_message(chat_id, kb_response.text)
            return kb_response

        kb_ai_response = self._process_knowledge_ai_request(text, message=message)
        if kb_ai_response:
            if chat_id:
                attachment = kb_ai_response.payload.get("attachment_path")
                if attachment:
                    self.send_document(chat_id, str(attachment), caption=kb_ai_response.text[:1000])
                else:
                    self.send_message(
                        chat_id,
                        kb_ai_response.text,
                        reply_to_message_id=message.get("message_id"),
                        reply_markup=self._knowledge_action_keyboard(),
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
        action = data.split(":", 1)[1]
        if action == "menu":
            response = TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        else:
            original = message.get("reply_to_message") or {}
            query = self._normalize_text(original.get("text") or original.get("caption") or "")
            if not query:
                query = self._normalize_text(message.get("text") or message.get("caption") or "")
            query = self._strip_bot_mention(query)
            if not query:
                response = TelegramResponse(
                    ok=False,
                    text="Не вижу исходный запрос. Напишите или наговорите его еще раз и выберите действие.",
                    payload={"intent": action},
                )
            else:
                response = self._run_knowledge_intent(action, query)
        if chat_id:
            attachment = response.payload.get("attachment_path")
            if attachment:
                self.send_document(chat_id, str(attachment), caption=response.text[:1000])
            else:
                self.send_message(chat_id, response.text, reply_markup=self._knowledge_action_keyboard())
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
                items = repo.list_revision_metadata(status="draft")
                if not items:
                    return TelegramResponse(ok=True, text="KB proposals: no draft proposals.")
                lines = [f"KB proposals: {len(items)} draft"]
                for item in items[:10]:
                    lines.append(f"- {item.get('object_id')} [{item.get('source') or 'revision'}] status={item.get('status')}")
                if len(items) > 10:
                    lines.append(f"...and {len(items) - 10} more")
                return TelegramResponse(ok=True, text="\n".join(lines), payload={"count": len(items)})
            if action in {"diff", "show"} and token:
                metadata_path = repo.resolve_revision_metadata(token)
                if not metadata_path:
                    return TelegramResponse(ok=False, text=f"KB proposal not found: {token}")
                return TelegramResponse(ok=True, text=f"KB diff for {token}:\n{repo.revision_diff_text(metadata_path=metadata_path)}")
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
            text=(
                "KB commands: kb health | kb ask <question> | kb proposals | kb diff <id> | "
                "kb approve <id> | kb reject <id> | kb apply <id>"
            ),
        )

    def _process_knowledge_ai_request(self, text: str, *, message: dict) -> TelegramResponse | None:
        if not self._should_handle_knowledge_ai(text, message=message):
            return None
        query = self._strip_bot_mention(text).strip()
        if not query:
            return TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        intent = self._classify_knowledge_intent(query)
        if intent == "menu":
            return TelegramResponse(ok=True, text=self._knowledge_menu_text(), payload={"intent": "menu"})
        return self._run_knowledge_intent(intent, query)

    def _run_knowledge_intent(self, intent: str, query: str) -> TelegramResponse:
        repo = KnowledgeRepository(Path(os.environ.get("KNOWLEDGE_REPO_PATH", "company-knowledge")))
        if intent in {"health", "status"}:
            pending = len(repo.list_revision_metadata(status="draft"))
            rag = KnowledgeVectorStore(repo.root).stats()
            quality = repo.quality_report()
            return TelegramResponse(
                ok=True,
                text="\n".join(
                    [
                        "KB health:",
                        f"- pending proposals: {pending}",
                        f"- rag chunks: {rag.get('chunks_embedded', 0)}",
                        f"- rag usage tokens: {(rag.get('usage') or {}).get('estimated_tokens', 0)}",
                        f"- quality issues: {len(quality.issues)}",
                    ]
                ),
                payload={"intent": "health", "pending_proposals": pending, "rag": rag},
            )
        if intent in {"proposals", "review_proposals"}:
            items = repo.list_revision_metadata(status="draft")
            if not items:
                return TelegramResponse(ok=True, text="KB proposals: no draft proposals.", payload={"intent": "proposals", "count": 0})
            lines = [f"KB proposals: {len(items)} draft"]
            for item in items[:10]:
                lines.append(f"- {item.get('object_id')} [{item.get('source') or 'revision'}] status={item.get('status')}")
            return TelegramResponse(ok=True, text="\n".join(lines), payload={"intent": "proposals", "count": len(items)})
        if intent in {"export", "export_bundle"}:
            target = "agents" if any(marker in query.casefold() for marker in ["agent", "api", "machine"]) else "notebooklm"
            result = repo.export_external_bundle(target=target)
            zip_paths = [path for path in result.written_files if str(path).endswith(".zip")]
            zip_path = zip_paths[-1] if zip_paths else ""
            return TelegramResponse(
                ok=True,
                text=f"KB export готов: {target}. Objects: {result.objects_count}.",
                payload={"intent": "export_bundle", "attachment_path": zip_path, "result": result.model_dump()},
            )
        if intent in {"revise", "revise_knowledge"}:
            return self._create_knowledge_revision_from_query(repo, query)

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
            text = text.strip() + "\n\nSources:\n" + "\n".join(source_lines)
        return TelegramResponse(
            ok=True,
            text=text[:3900],
            payload={"intent": intent, "answer_mode": answer_mode, "sources_count": len(sources)},
        )

    def _answer_from_knowledge(self, repo: KnowledgeRepository, query: str, *, answer_mode: str) -> dict:
        client = client_from_env(dict(os.environ), require_llm=True)
        if client:
            store = KnowledgeVectorStore(
                repo.root,
                db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
                embeddings_model=client.embeddings_model,
            )
            return store.answer(query, embedding_client=client, chat_client=client, limit=5, min_score=0.18, answer_mode=answer_mode)
        return repo.ask(query, limit=5)

    def _create_knowledge_revision_from_query(self, repo: KnowledgeRepository, query: str) -> TelegramResponse:
        result = self._answer_from_knowledge(repo, query, answer_mode="general")
        task_case_id = ""
        for item in result.get("sources") or []:
            object_id = str(item.get("object_id") or "")
            if object_id.startswith("task_case__"):
                task_case_id = object_id
                break
        if not task_case_id:
            return TelegramResponse(
                ok=False,
                text="Не нашел task_case для корректировки. Уточните систему, функциональность или object_id.",
                payload={"intent": "revise_knowledge"},
            )
        proposal = repo.create_revision_proposal(object_id=task_case_id, correction=query)
        text = "\n".join(
            [
                f"Создал draft proposal для `{proposal.object_id}`.",
                "",
                "Проверьте:",
                f"kb diff {proposal.object_id}",
                "",
                "Дальше:",
                f"kb approve {proposal.object_id}",
                f"kb apply {proposal.object_id}",
                f"kb reject {proposal.object_id}",
            ]
        )
        return TelegramResponse(ok=True, text=text, payload={"intent": "revise_knowledge", "proposal": proposal.model_dump()})

    @classmethod
    def _should_handle_knowledge_ai(cls, text: str, *, message: dict) -> bool:
        cleaned = cls._strip_bot_mention(text).strip()
        lowered = cleaned.casefold()
        if BOT_MENTION_RE.search(text) and not cleaned:
            return True
        if message.get("voice") or message.get("audio"):
            return True
        reply = message.get("reply_to_message") or {}
        if BOT_MENTION_RE.search(text) and (reply.get("voice") or reply.get("audio")):
            return True
        if BOT_MENTION_RE.search(text) and any(
            marker in lowered
            for marker in [
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
        return cleaned.startswith(("?", "kb?"))

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
    def _knowledge_menu_text() -> str:
        return (
            "KB AI: выберите действие кнопкой или напишите запрос обычным текстом.\n\n"
            "Можно спросить, как работает функциональность, попросить инструкцию, ТЗ, export bundle или посмотреть proposals."
        )

    @staticmethod
    def _knowledge_action_keyboard() -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "Ask", "callback_data": "kb:ask"},
                    {"text": "Instruction", "callback_data": "kb:instruction"},
                    {"text": "Spec", "callback_data": "kb:spec"},
                ],
                [
                    {"text": "Export", "callback_data": "kb:export"},
                    {"text": "Proposals", "callback_data": "kb:proposals"},
                    {"text": "Health", "callback_data": "kb:health"},
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
        except Exception:
            return ""

    @staticmethod
    def _transcribe_audio_bytes(content: bytes, *, filename: str) -> str:
        api_key = os.environ.get("KNOWLEDGE_RAG_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
        if not api_key or not content:
            return ""
        base_url = os.environ.get("KNOWLEDGE_RAG_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1"
        model = os.environ.get("KNOWLEDGE_TRANSCRIPTION_MODEL") or os.environ.get("OPENAI_TRANSCRIPTION_MODEL") or "gpt-4o-mini-transcribe"
        suffix = Path(filename or "voice.ogg").suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(content)
            handle.flush()
            with open(handle.name, "rb") as audio:
                response = requests.post(
                    base_url.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": model},
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
