from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from .models import (
    ChecklistGroup,
    SyncAction,
    TaskDraft,
    TaskExtractorAction,
    TaskExtractorRequest,
    TaskExtractorResult,
    TaskExtractorSession,
    TaskExtractorSource,
)
from .telegram_links import POST_URL_PATTERN, TASK_URL_PATTERN


LOOM_URL_PATTERN = re.compile(r"https?://(?:www\.)?loom\.com/share/([A-Za-z0-9]+)[^\s]*", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s)]+", re.IGNORECASE)


class TaskExtractorService:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.state = service.state
        self.aicallorder = service.aicallorder
        self.bitrix = service.bitrix
        self.settings = service.settings

    def handle(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        if request.action in {TaskExtractorAction.collect, TaskExtractorAction.add}:
            return self.collect(request)
        if request.action == TaskExtractorAction.preview:
            return self.preview(request)
        if request.action == TaskExtractorAction.export:
            return self.export(request)
        if request.action == TaskExtractorAction.create:
            return self.publish(request, action=SyncAction.create)
        if request.action == TaskExtractorAction.update:
            return self.publish(request, action=SyncAction.update_description)
        if request.action == TaskExtractorAction.comment:
            return self.publish(request, action=SyncAction.append_comment)
        if request.action == TaskExtractorAction.checklist:
            return self.publish(request, action=SyncAction.append_checklists)
        if request.action == TaskExtractorAction.clear:
            return self.clear(request)
        return self.status(request)

    def collect(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        session = self._get_or_create_session(request)
        collected: list[TaskExtractorSource] = []
        for text, source_hint in self._request_texts(request):
            collected.extend(self._collect_text_sources(session=session, request=request, text=text, source_hint=source_hint))
        if request.target_task_id:
            collected.append(self._collect_bitrix_task(session=session, request=request, task_id=request.target_task_id))
        sources = self.state.list_task_extractor_sources(session_id=session.session_id)
        session = self.state.update_task_extractor_session(
            session_id=session.session_id,
            status="collecting",
            title=self._session_title(sources),
            target_task_id=request.target_task_id,
        )
        return TaskExtractorResult(
            action="collected",
            session=session,
            sources=sources,
            text=self._format_preview(session=session, sources=sources, added_count=len(collected)),
            details={"added": len(collected)},
        )

    def preview(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        session = self._active_session_or_error(request.chat_id)
        sources = self._enrich_sources(session)
        session = self.state.update_task_extractor_session(
            session_id=session.session_id,
            status="ready",
            title=self._session_title(sources),
            target_task_id=request.target_task_id or session.target_task_id,
        )
        return TaskExtractorResult(
            action="preview",
            session=session,
            sources=sources,
            text=self._format_preview(session=session, sources=sources),
            details=self._source_counts(sources),
        )

    def export(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        session = self._active_session_or_error(request.chat_id)
        sources = self._enrich_sources(session)
        session = self.state.update_task_extractor_session(
            session_id=session.session_id,
            status="exported",
            title=self._session_title(sources),
            target_task_id=request.target_task_id or session.target_task_id,
            exported=True,
        )
        export_dir = Path("exports") / "task_extractor" / session.session_id
        manifest, prompt = self._write_export_files(session=session, sources=sources, export_dir=export_dir)
        zip_path = self._zip_export(export_dir=export_dir, session=session)
        self.state.write_task_extractor_export(
            session_id=session.session_id,
            export_type="notebooklm",
            output_dir=str(export_dir),
            zip_path=str(zip_path),
            manifest=manifest,
            llm_prompt=prompt,
        )
        return TaskExtractorResult(
            action="exported",
            session=session,
            sources=sources,
            export_dir=str(export_dir),
            zip_path=str(zip_path),
            text=self._format_handoff_message(session=session, zip_path=zip_path),
            details={"manifest": manifest},
        )

    def publish(self, request: TaskExtractorRequest, *, action: SyncAction) -> TaskExtractorResult:
        session = self._active_session_or_error(request.chat_id)
        sources = self._enrich_sources(session)
        missing = [source for source in sources if source.status == "missing"]
        if missing:
            missing_text = ", ".join(f"{source.source_type}:{source.source_key}" for source in missing[:5])
            raise ValueError(f"Task Extractor has missing source data. Run preview/export and fix these sources first: {missing_text}")
        draft = self._build_task_draft(session=session, sources=sources)
        target_task_id = request.target_task_id or session.target_task_id
        result = self.service._apply_task_draft(
            draft=draft,
            source_type="task_extractor",
            source_key=session.session_id,
            action=action,
            explicit_task_id=target_task_id,
        )
        session = self.state.update_task_extractor_session(
            session_id=session.session_id,
            status="exported" if result.task_id else session.status,
            title=draft.title,
            target_task_id=result.task_id or target_task_id,
        )
        export_result = self.export(
            TaskExtractorRequest(
                action=TaskExtractorAction.export,
                chat_id=request.chat_id,
                message_id=request.message_id,
                user_id=request.user_id,
            )
        )
        if result.task_id:
            session = self.state.update_task_extractor_session(
                session_id=session.session_id,
                status="exported",
                target_task_id=result.task_id,
                published=True,
            )
            self.state.write_task_extractor_publication(
                session_id=session.session_id,
                bitrix_task_id=result.task_id,
                action=result.action,
                result=result.model_dump(),
            )
        text = "\n".join(
            [
                f"Task Extractor {result.action}",
                f"Session: {session.session_id}",
                f"Bitrix task: #{result.task_id}" if result.task_id else "Bitrix task: preview only",
                result.task_url or "",
                "",
                export_result.text,
            ]
        ).strip()
        return TaskExtractorResult(
            action=result.action,
            session=session,
            sources=sources,
            task_id=result.task_id,
            task_url=result.task_url,
            export_dir=export_result.export_dir,
            zip_path=export_result.zip_path,
            text=text,
            details=result.details,
        )

    def clear(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        session = self.state.clear_task_extractor_session(chat_id=request.chat_id)
        if session is None:
            return TaskExtractorResult(action="cleared", text="Task Extractor: no active session to clear.")
        return TaskExtractorResult(
            action="cleared",
            session=session,
            text=f"Task Extractor session cleared: {session.session_id}",
        )

    def status(self, request: TaskExtractorRequest) -> TaskExtractorResult:
        session = self.state.get_active_task_extractor_session(chat_id=request.chat_id)
        if session is None:
            return TaskExtractorResult(action="status", text="Task Extractor: no active session.")
        sources = self.state.list_task_extractor_sources(session_id=session.session_id)
        return TaskExtractorResult(
            action="status",
            session=session,
            sources=sources,
            text=self._format_preview(session=session, sources=sources),
            details=self._source_counts(sources),
        )

    def _get_or_create_session(self, request: TaskExtractorRequest) -> TaskExtractorSession:
        session = self.state.get_active_task_extractor_session(chat_id=request.chat_id)
        if session:
            return session
        session_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
        return self.state.create_task_extractor_session(
            session_id=session_id,
            chat_id=request.chat_id,
            root_message_id=request.message_id,
            created_by_user_id=request.user_id,
        )

    def _active_session_or_error(self, chat_id: str) -> TaskExtractorSession:
        session = self.state.get_active_task_extractor_session(chat_id=chat_id)
        if session is None:
            raise ValueError("Task Extractor has no active session. Send collect/add first.")
        return session

    def _request_texts(self, request: TaskExtractorRequest) -> list[tuple[str, str]]:
        items = [
            (request.reply_text, "reply"),
            (request.forward_text, "forward"),
            (request.text, "message"),
        ]
        result = []
        seen = set()
        for text, hint in items:
            cleaned = self._strip_command(text)
            key = cleaned.strip()
            if key and key not in seen:
                seen.add(key)
                result.append((cleaned, hint))
        return result

    def _collect_text_sources(
        self,
        *,
        session: TaskExtractorSession,
        request: TaskExtractorRequest,
        text: str,
        source_hint: str,
    ) -> list[TaskExtractorSource]:
        collected: list[TaskExtractorSource] = []
        for task_id in self._extract_task_ids(text):
            collected.append(self._collect_bitrix_task(session=session, request=request, task_id=task_id, context_text=text))
        for loom_video_id, loom_url in self._extract_loom_urls(text):
            source = TaskExtractorSource(
                session_id=session.session_id,
                source_type="loom",
                source_key=loom_video_id,
                source_url=loom_url,
                telegram_chat_id=request.chat_id,
                telegram_message_id=request.message_id,
                loom_video_id=loom_video_id,
                title=f"Loom {loom_video_id}",
                raw_text=text,
                normalized={"source_hint": source_hint},
            )
            collected.append(self.state.upsert_task_extractor_source(source))
        for post_url in self._extract_telegram_post_urls(text):
            source = TaskExtractorSource(
                session_id=session.session_id,
                source_type="telegram_post",
                source_key=post_url,
                source_url=post_url,
                telegram_chat_id=request.chat_id,
                telegram_message_id=request.message_id,
                title="Telegram post",
                raw_text=text,
                normalized={"source_hint": source_hint},
            )
            collected.append(self.state.upsert_task_extractor_source(source))
        if text.strip():
            source_type = "task_context_post" if self._extract_task_ids(text) else "manual_note"
            source_key = f"{request.chat_id}:{request.message_id}:{source_hint}:{abs(hash(text))}"
            source = TaskExtractorSource(
                session_id=session.session_id,
                source_type=source_type,
                source_key=source_key,
                telegram_chat_id=request.chat_id,
                telegram_message_id=request.message_id,
                title=self._first_line(text) or source_type,
                raw_text=text,
                normalized={"source_hint": source_hint, "urls": URL_PATTERN.findall(text)},
            )
            collected.append(self.state.upsert_task_extractor_source(source))
        return collected

    def _collect_bitrix_task(
        self,
        *,
        session: TaskExtractorSession,
        request: TaskExtractorRequest,
        task_id: int,
        context_text: str = "",
    ) -> TaskExtractorSource:
        task = self._read_bitrix_task(task_id)
        source = TaskExtractorSource(
            session_id=session.session_id,
            source_type="bitrix_task",
            source_key=str(task_id),
            source_url=self.service._task_url(task_id),
            telegram_chat_id=request.chat_id,
            telegram_message_id=request.message_id,
            bitrix_task_id=task_id,
            title=str(task.get("title") or task.get("TITLE") or f"Bitrix task {task_id}"),
            raw_text=context_text,
            normalized={"task": task, "manual_context": context_text},
            status="collected" if task else "missing",
        )
        return self.state.upsert_task_extractor_source(source)

    def _enrich_sources(self, session: TaskExtractorSession) -> list[TaskExtractorSource]:
        sources = self.state.list_task_extractor_sources(session_id=session.session_id)
        enriched: list[TaskExtractorSource] = []
        for source in sources:
            if source.loom_video_id:
                meeting = self.aicallorder.get_meeting(source.loom_video_id)
                normalized = dict(source.normalized or {})
                if meeting:
                    normalized["meeting"] = meeting.model_dump()
                    source.title = meeting.title or source.title
                    source.status = "enriched"
                else:
                    normalized["missing"] = "aicallorder_meeting"
                    source.status = "missing"
                source.normalized = normalized
                source = self.state.upsert_task_extractor_source(source)
            if source.bitrix_task_id:
                normalized = dict(source.normalized or {})
                normalized["task"] = self._read_bitrix_task(source.bitrix_task_id)
                source.normalized = normalized
                source = self.state.upsert_task_extractor_source(source)
            enriched.append(source)
        return enriched

    def _read_bitrix_task(self, task_id: int) -> dict[str, Any]:
        data: dict[str, Any] = {"id": task_id, "url": self.service._task_url(task_id)}
        task = self.service._get_task_payload(
            task_id,
            select=["ID", "TITLE", "DESCRIPTION", "STATUS", "RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS", "GROUP_ID"],
        )
        if task:
            data.update(task)
        try:
            data["comments"] = self.bitrix.list_task_comments(task_id)[:30]
        except Exception as exc:
            data["comments_error"] = str(exc)
        try:
            data["checklist"] = self.bitrix.list_checklist_items(task_id)
        except Exception as exc:
            data["checklist_error"] = str(exc)
        return data

    def _build_task_draft(self, *, session: TaskExtractorSession, sources: list[TaskExtractorSource]) -> TaskDraft:
        title = self._session_title(sources)
        description = "\n\n".join(
            [
                f"# {title}",
                self._source_summary_markdown(sources),
                self._requirements_markdown(sources),
                self._open_questions_markdown(sources),
                self._source_links_markdown(sources),
                self._task_extractor_marker(session),
            ]
        ).strip()
        checklist_items = [
            "Confirm scope and out-of-scope items from the NotebookLM analysis.",
            "Confirm acceptance criteria with PM/product owner.",
            "Validate implementation dependencies and affected teams.",
            "Attach or link the NotebookLM project after it is created.",
        ]
        return TaskDraft(
            title=title,
            description=description,
            comment=self._publication_comment(session=session, sources=sources),
            checklist_groups=[ChecklistGroup(title="Task Extractor PM review", items=checklist_items)],
            tags=list(dict.fromkeys([*self.settings.bitrix_tags, "task-extractor", "notebooklm-export", "llm-context"])),
            meta={
                "task_extractor": True,
                "session_id": session.session_id,
                "source_count": len(sources),
                "loom_video_ids": [item.loom_video_id for item in sources if item.loom_video_id],
                "source_task_ids": [item.bitrix_task_id for item in sources if item.bitrix_task_id],
            },
        )

    def _write_export_files(
        self,
        *,
        session: TaskExtractorSession,
        sources: list[TaskExtractorSource],
        export_dir: Path,
    ) -> tuple[dict[str, Any], str]:
        source_dir = export_dir / "source_bundle"
        prompt_dir = export_dir / "prompt_workspace"
        machine_dir = export_dir / "machine_bundle"
        for directory in (source_dir, prompt_dir, machine_dir):
            directory.mkdir(parents=True, exist_ok=True)

        files = {
            source_dir / "00_readme.md": self._readme_markdown(session, sources),
            source_dir / "01_task_context.md": self._source_summary_markdown(sources),
            source_dir / "02_meetings_digest.md": self._meetings_markdown(sources),
            source_dir / "03_transcripts.md": self._transcripts_markdown(sources),
            source_dir / "04_existing_tasks.md": self._existing_tasks_markdown(sources),
            source_dir / "05_comments_and_manual_notes.md": self._manual_notes_markdown(sources),
            source_dir / "06_decisions_and_requirements.md": self._requirements_markdown(sources),
            source_dir / "07_open_questions.md": self._open_questions_markdown(sources),
            source_dir / "08_source_links.md": self._source_links_markdown(sources),
        }
        prompt = self._notebook_prompt(session, sources)
        files[prompt_dir / "prompt_for_notebooklm.md"] = prompt
        files[prompt_dir / "generate_functional_spec.md"] = self._functional_spec_prompt()
        files[prompt_dir / "generate_estimation_questions.md"] = self._estimation_prompt()
        files[prompt_dir / "generate_acceptance_criteria.md"] = self._acceptance_prompt()

        manifest = self._handoff_manifest(session=session, sources=sources, export_dir=export_dir)
        files[machine_dir / "handoff_manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
        files[machine_dir / "source_manifest.json"] = json.dumps([item.model_dump() for item in sources], ensure_ascii=False, indent=2)
        files[machine_dir / "bitrix_manifest.json"] = json.dumps(
            [item.normalized.get("task") for item in sources if item.bitrix_task_id],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        for path, content in files.items():
            path.write_text(content.strip() + "\n", encoding="utf-8")
        return manifest, prompt

    def _zip_export(self, *, export_dir: Path, session: TaskExtractorSession) -> Path:
        task_part = f"task_{session.target_task_id}" if session.target_task_id else "draft"
        zip_path = export_dir.parent / f"task_extractor_{session.session_id}__{task_part}__notebooklm.zip"
        with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
            for path in export_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(export_dir))
        return zip_path

    def _handoff_manifest(
        self,
        *,
        session: TaskExtractorSession,
        sources: list[TaskExtractorSource],
        export_dir: Path,
    ) -> dict[str, Any]:
        title = self._session_title(sources)
        task_label = f"Task {session.target_task_id}" if session.target_task_id else f"Draft {session.session_id}"
        return {
            "schema_version": 1,
            "session_id": session.session_id,
            "telegram_chat_id": session.chat_id,
            "telegram_root_message_id": session.root_message_id,
            "telegram_export_message_id": "",
            "created_by_user_id": session.created_by_user_id,
            "status": session.status,
            "title": title,
            "bitrix_task_id": session.target_task_id,
            "bitrix_task_url": self.service._task_url(session.target_task_id) if session.target_task_id else "",
            "notebooklm_project_title": f"{task_label} - {title}",
            "notebooklm_project_url": "",
            "zip_path": "",
            "source_bundle_files": [
                "source_bundle/00_readme.md",
                "source_bundle/01_task_context.md",
                "source_bundle/02_meetings_digest.md",
                "source_bundle/03_transcripts.md",
                "source_bundle/04_existing_tasks.md",
                "source_bundle/05_comments_and_manual_notes.md",
                "source_bundle/06_decisions_and_requirements.md",
                "source_bundle/07_open_questions.md",
                "source_bundle/08_source_links.md",
            ],
            "source_count": len(sources),
            "loom_video_ids": [item.loom_video_id for item in sources if item.loom_video_id],
            "source_task_ids": [item.bitrix_task_id for item in sources if item.bitrix_task_id],
            "created_at": session.created_at,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _format_preview(self, *, session: TaskExtractorSession, sources: list[TaskExtractorSource], added_count: int = 0) -> str:
        counts = self._source_counts(sources)
        lines = [
            "Task Extractor session",
            f"Session: {session.session_id}",
            f"Status: {session.status}",
            f"Title: {self._session_title(sources)}",
            f"Sources: {len(sources)}",
        ]
        if added_count:
            lines.append(f"Added now: {added_count}")
        for key, value in counts.items():
            lines.append(f"- {key}: {value}")
        missing = [item for item in sources if item.status == "missing"]
        if missing:
            lines.append("")
            lines.append("Missing:")
            lines.extend(f"- {item.source_type}: {item.source_key}" for item in missing[:10])
        lines.append("")
        lines.append("Next: export / create / update <task_id> / clear")
        return "\n".join(lines)

    def _format_handoff_message(self, *, session: TaskExtractorSession, zip_path: Path) -> str:
        title = session.title or "Task Extractor draft"
        notebook_title = f"Task {session.target_task_id} - {title}" if session.target_task_id else f"Draft {session.session_id} - {title}"
        return "\n".join(
            [
                "Task Extractor export ready",
                f"Session: {session.session_id}",
                f"Bitrix task: #{session.target_task_id}" if session.target_task_id else "Bitrix task: draft",
                f"NotebookLM title: {notebook_title}",
                f"Package: {zip_path.name}",
                "Status: exported",
            ]
        )

    @staticmethod
    def _source_counts(sources: list[TaskExtractorSource]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in sources:
            counts[source.source_type] = counts.get(source.source_type, 0) + 1
        return counts

    def _session_title(self, sources: list[TaskExtractorSource]) -> str:
        for source in sources:
            if source.source_type in {"task_context_post", "manual_note"} and source.title:
                return self._short_title(source.title)
        for source in sources:
            if source.title:
                return self._short_title(source.title)
        return "Task Extractor functional analysis"

    @staticmethod
    def _short_title(value: str) -> str:
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            return "Task Extractor functional analysis"
        return cleaned[:117].rstrip() + "..." if len(cleaned) > 120 else cleaned

    @staticmethod
    def _first_line(text: str) -> str:
        for line in text.splitlines():
            if line.strip() and not line.strip().startswith("http"):
                return line.strip()
        return ""

    def _source_summary_markdown(self, sources: list[TaskExtractorSource]) -> str:
        lines = ["# Task context", ""]
        for index, source in enumerate(sources, start=1):
            lines.append(f"## Source {index}. {source.source_type}: {source.title or source.source_key}")
            if source.source_url:
                lines.append(f"URL: {source.source_url}")
            if source.loom_video_id:
                lines.append(f"Loom video ID: {source.loom_video_id}")
            if source.bitrix_task_id:
                lines.append(f"Bitrix task: {source.bitrix_task_id}")
            if source.raw_text:
                lines.extend(["", source.raw_text.strip()[:6000]])
            lines.append("")
        return "\n".join(lines).strip()

    def _meetings_markdown(self, sources: list[TaskExtractorSource]) -> str:
        lines = ["# Meetings digest", ""]
        for source in sources:
            meeting = source.normalized.get("meeting") if isinstance(source.normalized, dict) else None
            if not meeting:
                continue
            lines.extend(
                [
                    f"## {meeting.get('title') or source.title}",
                    f"Loom: {meeting.get('source_url') or source.source_url}",
                    f"Recorded at: {meeting.get('recorded_at') or ''}",
                    "",
                    json.dumps(meeting.get("artifacts") or {}, ensure_ascii=False, indent=2)[:8000],
                    "",
                ]
            )
        return "\n".join(lines).strip() or "# Meetings digest\n\nNo meetings found."

    def _transcripts_markdown(self, sources: list[TaskExtractorSource]) -> str:
        lines = ["# Transcripts", ""]
        for source in sources:
            meeting = source.normalized.get("meeting") if isinstance(source.normalized, dict) else None
            transcript = str((meeting or {}).get("transcript_text") or "").strip()
            if transcript:
                lines.extend([f"## {meeting.get('title') or source.title}", transcript[:50000], ""])
        return "\n".join(lines).strip() or "# Transcripts\n\nNo transcripts found."

    def _existing_tasks_markdown(self, sources: list[TaskExtractorSource]) -> str:
        lines = ["# Existing Bitrix tasks", ""]
        for source in sources:
            task = source.normalized.get("task") if isinstance(source.normalized, dict) else None
            if not task:
                continue
            title = task.get("title") or task.get("TITLE") or source.title
            description = task.get("description") or task.get("DESCRIPTION") or ""
            lines.extend([f"## Task {source.bitrix_task_id}: {title}", f"URL: {source.source_url}", "", str(description)[:12000], ""])
            comments = task.get("comments") or []
            if comments:
                lines.append("### Comments")
                for comment in comments[:20]:
                    lines.append(f"- {self._comment_text(comment)[:1000]}")
                lines.append("")
            checklist = task.get("checklist") or []
            if checklist:
                lines.append("### Checklist")
                for item in checklist[:100]:
                    lines.append(f"- {item.get('TITLE') or item.get('title')}")
                lines.append("")
        return "\n".join(lines).strip() or "# Existing Bitrix tasks\n\nNo Bitrix tasks found."

    def _manual_notes_markdown(self, sources: list[TaskExtractorSource]) -> str:
        lines = ["# Comments and manual notes", ""]
        for source in sources:
            if source.source_type not in {"manual_note", "task_context_post", "telegram_post"}:
                continue
            lines.extend([f"## {source.title or source.source_type}", source.raw_text.strip(), ""])
        return "\n".join(lines).strip() or "# Comments and manual notes\n\nNo manual notes found."

    def _requirements_markdown(self, sources: list[TaskExtractorSource]) -> str:
        return "\n".join(
            [
                "# Decisions and requirements",
                "",
                "Use this section as a working area for NotebookLM.",
                "Extract requirements only from attached sources.",
                "Prefer manual user notes over AI summaries when they conflict.",
                "Mark uncertain items as open questions.",
                "",
                f"Source count: {len(sources)}",
            ]
        )

    @staticmethod
    def _open_questions_markdown(sources: list[TaskExtractorSource]) -> str:
        missing = [source for source in sources if source.status == "missing"]
        lines = ["# Open questions", ""]
        if missing:
            lines.append("Missing source data:")
            lines.extend(f"- {source.source_type}: {source.source_key}" for source in missing)
        else:
            lines.append("No automatic gaps detected. Ask NotebookLM to identify specification gaps.")
        return "\n".join(lines)

    @staticmethod
    def _source_links_markdown(sources: list[TaskExtractorSource]) -> str:
        lines = ["# Source links", ""]
        for source in sources:
            if source.source_url:
                lines.append(f"- {source.source_type}: {source.source_url}")
        return "\n".join(lines).strip() or "# Source links\n\nNo links found."

    def _readme_markdown(self, session: TaskExtractorSession, sources: list[TaskExtractorSource]) -> str:
        return "\n".join(
            [
                f"# Task Extractor package: {self._session_title(sources)}",
                "",
                f"Session: {session.session_id}",
                f"Status: {session.status}",
                f"Sources: {len(sources)}",
                "",
                "Upload `source_bundle/*.md` to NotebookLM.",
                "Use `prompt_workspace/prompt_for_notebooklm.md` as the first prompt.",
            ]
        )

    def _notebook_prompt(self, session: TaskExtractorSession, sources: list[TaskExtractorSource]) -> str:
        return "\n".join(
            [
                "You are helping prepare a functional specification for a new feature.",
                "Use only the uploaded Task Extractor sources.",
                "Manual notes from the user have priority over AI-generated summaries.",
                "When sources conflict, call out the conflict and ask for confirmation.",
                "",
                "Produce:",
                "1. Functional summary.",
                "2. Scope and out of scope.",
                "3. Requirements.",
                "4. Integration/data notes.",
                "5. Acceptance criteria.",
                "6. Estimation questions.",
                "7. Risks and blockers.",
                "8. Source references.",
                "",
                f"Task Extractor session: {session.session_id}",
                f"Source count: {len(sources)}",
            ]
        )

    @staticmethod
    def _functional_spec_prompt() -> str:
        return "Generate a technical/functional spec from the uploaded sources with source references."

    @staticmethod
    def _estimation_prompt() -> str:
        return "Generate estimation questions grouped by product, backend, frontend, QA, data, and risks."

    @staticmethod
    def _acceptance_prompt() -> str:
        return "Generate concise PM and QA acceptance criteria. Mark assumptions clearly."

    def _publication_comment(self, *, session: TaskExtractorSession, sources: list[TaskExtractorSource]) -> str:
        return "\n".join(
            [
                "Task Extractor context package prepared.",
                f"Session: {session.session_id}",
                f"Sources: {len(sources)}",
                "NotebookLM handoff package is attached in Telegram/export storage.",
            ]
        )

    @staticmethod
    def _task_extractor_marker(session: TaskExtractorSession) -> str:
        return "\n".join(
            [
                f"=== TASK_EXTRACTOR_CONTEXT START session_id={session.session_id} ===",
                "Generated from Task Extractor source pool.",
                f"=== TASK_EXTRACTOR_CONTEXT END session_id={session.session_id} ===",
            ]
        )

    @staticmethod
    def _extract_task_ids(text: str) -> list[int]:
        result: list[int] = []
        for match in TASK_URL_PATTERN.finditer(text or ""):
            result.append(int(match.group("task_id")))
        for match in re.finditer(r"(?<![-/\d#])(?:task\s*#?|задач[аиеуы]?\s*#?|#)(\d{3,})(?![-/\d])", text or "", re.IGNORECASE):
            result.append(int(match.group(1)))
        return list(dict.fromkeys(result))

    @staticmethod
    def _extract_loom_urls(text: str) -> list[tuple[str, str]]:
        result = []
        for match in LOOM_URL_PATTERN.finditer(text or ""):
            result.append((match.group(1), match.group(0).rstrip(".,;")))
        return list(dict.fromkeys(result))

    @staticmethod
    def _extract_telegram_post_urls(text: str) -> list[str]:
        return list(dict.fromkeys(match.group(1) for match in POST_URL_PATTERN.finditer(text or "")))

    @staticmethod
    def _comment_text(comment: dict[str, Any]) -> str:
        for key in ("POST_MESSAGE", "POST_MESSAGE_TEXT", "COMMENTTEXT", "text", "message"):
            value = comment.get(key)
            if value:
                return " ".join(str(value).split())
        return json.dumps(comment, ensure_ascii=False, default=str)

    @staticmethod
    def _strip_command(text: str) -> str:
        cleaned = re.sub(r"@Task_?Extractor_?Bot\b", "", text or "", flags=re.IGNORECASE)
        cleaned = re.sub(
            r"\b(?:collect|add|preview|export|create|update|comment|checklist|clear|status|собрать|добавить|выгрузка|создать|обновить|коммент|чеклист|очистить|статус)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()
