from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import MeetingRecord, PublicationRecord, TaskDraft
from .task_drafts import build_meeting_task_draft


KNOWLEDGE_TAGS = {"#task_discussion", "#task_demo"}
DISCUSSION_TAG = "#task_discussion"
DEMO_TAG = "#task_demo"
EXCLUDED_TAGS = {"#daily"}
PROMPT_WORKSPACE_FILES = {
    "README.md": """# Prompt Workspace

Use these prompts with the sibling `source_bundle` and `machine_bundle`.
External AI tools should propose changes with source references; they should
not directly mutate the source of truth.
""",
    "revise_knowledge_object.md": """Use the attached knowledge object as grounded context.

Task:
Revise the accumulated knowledge object according to the user's correction.

Rules:
- Use only sources from this bundle.
- If discussion and demo conflict, prefer demo.
- Do not delete older requirements; mark them as superseded in the proposal.
- Preserve source event IDs for every material change.
- Return a change proposal, not a final write.

User correction:
{{user_correction}}
""",
    "generate_user_instruction.md": """Use the attached knowledge object as grounded context.

Task:
Generate a user-facing instruction for the functionality.

Rules:
- Write for an operator or product user, not a developer.
- Include prerequisites, step-by-step flow, expected result, edge cases, and source references.
- Do not include implementation speculation.
- If sources are insufficient, list missing questions instead of inventing details.
""",
    "generate_technical_spec.md": """Use the attached knowledge object as grounded context.

Task:
Generate a technical specification for implementation.

Rules:
- Include context, scope, functional requirements, acceptance criteria, integrations, data/contracts, risks, and open questions.
- Separate confirmed demo feedback from discussion assumptions.
- Cite source event IDs next to requirements and decisions.
""",
    "detect_conflicts.md": """Use the attached knowledge object as grounded context.

Task:
Find conflicts, outdated requirements, and unresolved questions.

Rules:
- Group conflicts by feature area.
- Prefer newer demo events over older discussion events when they disagree.
- Return each conflict with affected source event IDs and a recommended resolution.
""",
}


class KnowledgeSourceEvent(BaseModel):
    event_id: str
    event_type: str
    title: str
    recorded_at: str | None = None
    loom_video_id: str
    loom_url: str = ""
    telegram_post_url: str = ""
    google_doc_url: str | None = None
    transcript_doc_url: str | None = None
    summary: str = ""
    decisions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    raw_tags: list[str] = Field(default_factory=list)


class KnowledgeObject(BaseModel):
    object_id: str
    object_type: str = "task_case"
    title: str
    system: str = "unknown"
    subsystem: str = ""
    feature_area: str = ""
    status: str = "draft"
    source_tags: list[str] = Field(default_factory=list)
    linked_bitrix_tasks: list[int] = Field(default_factory=list)
    linked_loom_ids: list[str] = Field(default_factory=list)
    linked_telegram_posts: list[str] = Field(default_factory=list)
    current_summary: str = ""
    current_requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    demo_feedback: list[str] = Field(default_factory=list)
    source_events: list[KnowledgeSourceEvent] = Field(default_factory=list)


class KnowledgeExportResult(BaseModel):
    output_dir: str
    objects_count: int
    object_ids: list[str]


class KnowledgeBackfillResult(BaseModel):
    scanned: int
    updated: int
    candidates: int
    post_urls: list[str] = Field(default_factory=list)


class KnowledgeIntake:
    """Build accumulated knowledge objects from registered Loom task publications.

    This is intentionally read-only. It turns the existing MeetingDigestBot
    state into exportable knowledge packages without writing to CRM, Notion, or
    the future Git knowledge repository.
    """

    def __init__(self, service) -> None:
        self.service = service

    def collect(
        self,
        *,
        post_url: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
    ) -> list[KnowledgeObject]:
        publications = (
            [self.service.state.get_publication_by_post_url(post_url)]
            if post_url
            else self.service.state.list_publications(digest_type="meeting", limit=limit)
        )
        objects: dict[str, KnowledgeObject] = {}
        for publication in publications:
            if publication is None or not publication.loom_video_id:
                continue
            meeting = self.service.aicallorder.get_meeting(publication.loom_video_id)
            if meeting is None or not self._is_in_date_window(meeting, date_from=date_from, date_to=date_to):
                continue
            if not self.is_knowledge_candidate(meeting=meeting, publication=publication):
                continue
            draft = build_meeting_task_draft(
                meeting=meeting,
                publication=publication,
                default_tags=self.service.settings.bitrix_tags,
            )
            binding = self.service.state.get_task_binding(source_type="meeting", source_key=meeting.loom_video_id)
            object_id = self._object_id(meeting=meeting, draft=draft, binding=binding)
            if object_id not in objects:
                objects[object_id] = self._new_object(object_id=object_id, meeting=meeting, draft=draft, binding=binding)
            self._append_event(objects[object_id], meeting=meeting, publication=publication, draft=draft, binding=binding)
        return list(objects.values())

    def export(
        self,
        *,
        output_dir: Path,
        bundle: str = "all",
        post_url: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int | None = None,
    ) -> KnowledgeExportResult:
        objects = self.collect(post_url=post_url, date_from=date_from, date_to=date_to, limit=limit)
        output_dir.mkdir(parents=True, exist_ok=True)
        for item in objects:
            self._write_object_bundle(output_dir=output_dir, item=item, bundle=bundle)
        return KnowledgeExportResult(
            output_dir=str(output_dir),
            objects_count=len(objects),
            object_ids=[item.object_id for item in objects],
        )

    def backfill_source_tags(self, *, limit: int | None = None) -> KnowledgeBackfillResult:
        publications = self.service.state.list_publications(digest_type="meeting", limit=limit)
        scanned = 0
        updated = 0
        candidate_urls: list[str] = []
        for publication in publications:
            scanned += 1
            meeting = self.service.aicallorder.get_meeting(publication.loom_video_id) if publication.loom_video_id else None
            if meeting is None:
                continue
            tags = sorted(self._all_tags(meeting=meeting, publication=publication))
            if not tags:
                continue
            existing = publication.payload_json.get("source_tags") or []
            existing_norm = {str(tag).casefold() for tag in existing if str(tag).strip()}
            if set(tags) != existing_norm:
                payload = dict(publication.payload_json or {})
                payload["source_tags"] = tags
                publication = self.service.state.update_publication_payload(post_url=publication.post_url, payload=payload) or publication
                updated += 1
            if self.is_knowledge_candidate(meeting=meeting, publication=publication):
                candidate_urls.append(publication.post_url)
        return KnowledgeBackfillResult(
            scanned=scanned,
            updated=updated,
            candidates=len(candidate_urls),
            post_urls=candidate_urls,
        )

    @classmethod
    def is_knowledge_candidate(cls, *, meeting: MeetingRecord, publication: PublicationRecord) -> bool:
        tags = cls._all_tags(meeting=meeting, publication=publication)
        if tags & EXCLUDED_TAGS:
            return False
        return bool(tags & KNOWLEDGE_TAGS)

    @staticmethod
    def _is_in_date_window(meeting: MeetingRecord, *, date_from: date | None, date_to: date | None) -> bool:
        if not date_from and not date_to:
            return True
        raw = (meeting.recorded_at or "")[:10]
        if not raw:
            return True
        try:
            recorded = date.fromisoformat(raw)
        except ValueError:
            return True
        if date_from and recorded < date_from:
            return False
        if date_to and recorded > date_to:
            return False
        return True

    @classmethod
    def _all_tags(cls, *, meeting: MeetingRecord, publication: PublicationRecord) -> set[str]:
        values: list[str] = [meeting.title, publication.meeting_title or ""]
        artifacts = meeting.artifacts or {}
        for key in ("tags", "hashtags", "source_tags"):
            raw = artifacts.get(key)
            if isinstance(raw, list):
                values.extend(str(item) for item in raw)
            elif raw:
                values.append(str(raw))
        values.append(json.dumps(publication.payload_json or {}, ensure_ascii=False))
        tags: set[str] = set()
        for value in values:
            for match in re.findall(r"#[\wА-Яа-яІіЇїЄєҐґ-]+", value, flags=re.UNICODE):
                tags.add(match.casefold())
        return tags

    def _new_object(
        self,
        *,
        object_id: str,
        meeting: MeetingRecord,
        draft: TaskDraft,
        binding: dict[str, Any] | None,
    ) -> KnowledgeObject:
        artifacts = meeting.artifacts or {}
        tech_spec = artifacts.get("technical_spec_draft") if isinstance(artifacts.get("technical_spec_draft"), dict) else {}
        system, subsystem, feature_area = self._classify_area(meeting=meeting, draft=draft, tech_spec=tech_spec)
        linked_tasks = self._linked_tasks(binding)
        return KnowledgeObject(
            object_id=object_id,
            title=draft.title,
            system=system,
            subsystem=subsystem,
            feature_area=feature_area,
            linked_bitrix_tasks=linked_tasks,
            current_summary=str(artifacts.get("summary") or "").strip(),
        )

    def _append_event(
        self,
        item: KnowledgeObject,
        *,
        meeting: MeetingRecord,
        publication: PublicationRecord,
        draft: TaskDraft,
        binding: dict[str, Any] | None,
    ) -> None:
        artifacts = meeting.artifacts or {}
        tech_spec = artifacts.get("technical_spec_draft") if isinstance(artifacts.get("technical_spec_draft"), dict) else {}
        tags = sorted(self._all_tags(meeting=meeting, publication=publication))
        event_type = "demo" if DEMO_TAG in tags else "discussion"
        event = KnowledgeSourceEvent(
            event_id=f"{event_type}__{meeting.loom_video_id}",
            event_type=event_type,
            title=meeting.title,
            recorded_at=meeting.recorded_at,
            loom_video_id=meeting.loom_video_id,
            loom_url=meeting.source_url,
            telegram_post_url=publication.post_url,
            google_doc_url=publication.google_doc_url,
            transcript_doc_url=publication.transcript_doc_url,
            summary=str(artifacts.get("summary") or "").strip(),
            decisions=self._clean_list(artifacts.get("decisions")),
            action_items=self._action_titles(artifacts.get("action_items")),
            blockers=self._clean_list(artifacts.get("blockers")),
            open_questions=self._clean_list(tech_spec.get("open_questions")),
            acceptance_criteria=self._clean_list(tech_spec.get("acceptance_criteria")),
            raw_tags=tags,
        )
        item.source_events.append(event)
        item.source_tags = sorted(set(item.source_tags) | set(tags))
        item.linked_loom_ids = sorted(set(item.linked_loom_ids) | {meeting.loom_video_id})
        item.linked_telegram_posts = sorted(set(item.linked_telegram_posts) | {publication.post_url})
        item.linked_bitrix_tasks = sorted(set(item.linked_bitrix_tasks) | set(self._linked_tasks(binding)))
        item.decisions = self._merge_unique(item.decisions, event.decisions)
        item.open_questions = self._merge_unique(item.open_questions, event.open_questions)
        item.acceptance_criteria = self._merge_unique(item.acceptance_criteria, event.acceptance_criteria)
        item.current_requirements = self._merge_unique(
            item.current_requirements,
            self._clean_list(tech_spec.get("functional_requirements")) or event.action_items,
        )
        if event.event_type == "demo":
            item.demo_feedback = self._merge_unique(
                item.demo_feedback,
                event.open_questions + event.blockers + event.action_items,
            )

    @staticmethod
    def _object_id(*, meeting: MeetingRecord, draft: TaskDraft, binding: dict[str, Any] | None) -> str:
        linked_tasks = KnowledgeIntake._linked_tasks(binding)
        if linked_tasks:
            return f"task_case__bitrix_{linked_tasks[0]}"
        title_key = KnowledgeIntake._slug(draft.title or meeting.title)
        digest = hashlib.sha1(meeting.loom_video_id.encode("utf-8")).hexdigest()[:8]
        return f"task_case__{title_key}_{digest}"

    @staticmethod
    def _linked_tasks(binding: dict[str, Any] | None) -> list[int]:
        if not binding:
            return []
        try:
            return [int(binding["bitrix_task_id"])]
        except (KeyError, TypeError, ValueError):
            return []

    @staticmethod
    def _classify_area(*, meeting: MeetingRecord, draft: TaskDraft, tech_spec: dict[str, Any]) -> tuple[str, str, str]:
        text = " ".join(
            [
                meeting.title,
                draft.title,
                draft.description,
                json.dumps(tech_spec, ensure_ascii=False),
            ]
        ).casefold()
        if "bitrix" in text or "битрикс" in text or "crm" in text or "crm-зада" in text:
            system = "bitrix"
        elif "aicallorder" in text or "loom" in text:
            system = "aicallorder"
        elif "meetingdigestbot" in text or "llmeets" in text:
            system = "meeting_digest_bot"
        else:
            system = "unknown"

        if "чеклист" in text or "checklist" in text:
            feature_area = "checklists"
        elif "коммент" in text or "comment" in text:
            feature_area = "comments"
        elif "telegram" in text or "телеграм" in text:
            feature_area = "telegram_publication"
        elif "notion" in text or "knowledge" in text or "база знаний" in text:
            feature_area = "knowledge_base"
        else:
            feature_area = ""
        return system, "", feature_area

    @staticmethod
    def _write_object_bundle(*, output_dir: Path, item: KnowledgeObject, bundle: str = "all") -> None:
        object_dir = output_dir / item.object_id
        object_dir.mkdir(parents=True, exist_ok=True)
        if bundle in {"all", "source"}:
            KnowledgeIntake._write_source_bundle(object_dir=object_dir, item=item)
        if bundle in {"all", "machine"}:
            KnowledgeIntake._write_machine_bundle(object_dir=object_dir, item=item)
        if bundle in {"all", "prompts"}:
            KnowledgeIntake._write_prompt_workspace(object_dir=object_dir, item=item)

        # Compatibility files for simple manual use and older exports.
        if bundle == "all":
            (object_dir / "00_manifest.json").write_text(
                json.dumps(item.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (object_dir / "01_overview.md").write_text(KnowledgeIntake._overview_markdown(item), encoding="utf-8")
            (object_dir / "02_functional_spec.md").write_text(KnowledgeIntake._spec_markdown(item), encoding="utf-8")
            (object_dir / "03_source_events.md").write_text(KnowledgeIntake._events_markdown(item), encoding="utf-8")
            (object_dir / "04_ai_bundle.json").write_text(
                json.dumps(KnowledgeIntake._machine_payload(item), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def _write_source_bundle(*, object_dir: Path, item: KnowledgeObject) -> None:
        source_dir = object_dir / "source_bundle"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "00_readme.md").write_text(KnowledgeIntake._source_readme_markdown(item), encoding="utf-8")
        (source_dir / "01_overview.md").write_text(KnowledgeIntake._overview_markdown(item), encoding="utf-8")
        (source_dir / "02_functional_spec.md").write_text(KnowledgeIntake._spec_markdown(item), encoding="utf-8")
        (source_dir / "03_decisions.md").write_text(KnowledgeIntake._list_markdown(item, "Decisions", item.decisions), encoding="utf-8")
        (source_dir / "04_acceptance_criteria.md").write_text(
            KnowledgeIntake._list_markdown(item, "Acceptance Criteria", item.acceptance_criteria),
            encoding="utf-8",
        )
        (source_dir / "05_demo_feedback.md").write_text(
            KnowledgeIntake._list_markdown(item, "Demo Feedback", item.demo_feedback),
            encoding="utf-8",
        )
        (source_dir / "06_source_events.md").write_text(KnowledgeIntake._events_markdown(item), encoding="utf-8")
        (source_dir / "07_sources.md").write_text(KnowledgeIntake._sources_markdown(item), encoding="utf-8")

    @staticmethod
    def _write_machine_bundle(*, object_dir: Path, item: KnowledgeObject) -> None:
        machine_dir = object_dir / "machine_bundle"
        machine_dir.mkdir(parents=True, exist_ok=True)
        (machine_dir / "knowledge_object.json").write_text(
            json.dumps(item.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (machine_dir / "ai_context.json").write_text(
            json.dumps(KnowledgeIntake._machine_payload(item), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (machine_dir / "retrieval_manifest.json").write_text(
            json.dumps(
                {
                    "object_id": item.object_id,
                    "title": item.title,
                    "system": item.system,
                    "feature_area": item.feature_area,
                    "source_tags": item.source_tags,
                    "source_files": [
                        "source_bundle/01_overview.md",
                        "source_bundle/02_functional_spec.md",
                        "source_bundle/06_source_events.md",
                        "source_bundle/07_sources.md",
                    ],
                    "machine_files": [
                        "machine_bundle/knowledge_object.json",
                        "machine_bundle/ai_context.json",
                    ],
                    "prompt_workspace": "prompt_workspace",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_prompt_workspace(*, object_dir: Path, item: KnowledgeObject) -> None:
        prompt_dir = object_dir / "prompt_workspace"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        for filename, template in PROMPT_WORKSPACE_FILES.items():
            (prompt_dir / filename).write_text(template.strip() + "\n", encoding="utf-8")
        (prompt_dir / "object_context.md").write_text(KnowledgeIntake._prompt_context_markdown(item), encoding="utf-8")

    @staticmethod
    def _machine_payload(item: KnowledgeObject) -> dict[str, Any]:
        return {
            "instruction": (
                "Use this accumulated knowledge object as grounded context. "
                "Preserve source references. If discussion and demo conflict, prefer demo "
                "and mark older points as superseded instead of deleting them."
            ),
            "contracts": {
                "source_of_truth": "future Git knowledge repository",
                "external_ai_role": "analysis_and_patch_proposal",
                "direct_mutation_allowed": False,
                "excluded_tags": sorted(EXCLUDED_TAGS),
                "included_tags": sorted(KNOWLEDGE_TAGS),
                "conflict_priority": ["demo", "discussion"],
            },
            "knowledge_object": item.model_dump(),
        }

    @staticmethod
    def _overview_markdown(item: KnowledgeObject) -> str:
        lines = [
            f"# {item.title}",
            "",
            f"- Object ID: `{item.object_id}`",
            f"- Type: `{item.object_type}`",
            f"- System: `{item.system}`",
            f"- Feature area: `{item.feature_area or 'unknown'}`",
            f"- Status: `{item.status}`",
            f"- Source tags: {', '.join(item.source_tags) or '-'}",
            f"- Bitrix tasks: {', '.join(str(task) for task in item.linked_bitrix_tasks) or '-'}",
            "",
            "## Current Summary",
            "",
            item.current_summary or "No summary yet.",
            "",
            "## Source Links",
            "",
        ]
        for url in item.linked_telegram_posts:
            lines.append(f"- Telegram: {url}")
        for event in item.source_events:
            lines.append(f"- Loom: {event.loom_url}")
            if event.google_doc_url:
                lines.append(f"- Google Doc: {event.google_doc_url}")
            if event.transcript_doc_url:
                lines.append(f"- Transcript Doc: {event.transcript_doc_url}")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _source_readme_markdown(item: KnowledgeObject) -> str:
        return "\n".join(
            [
                f"# Source Bundle: {item.title}",
                "",
                "This bundle is intended for NotebookLM and other RAG tools.",
                "Load the Markdown files as sources. Keep `07_sources.md` attached so answers can cite provenance.",
                "",
                "Recommended reading order:",
                "",
                "1. `01_overview.md`",
                "2. `02_functional_spec.md`",
                "3. `06_source_events.md`",
                "4. `07_sources.md`",
            ]
        ).strip() + "\n"

    @staticmethod
    def _spec_markdown(item: KnowledgeObject) -> str:
        lines = [f"# Functional Spec: {item.title}", ""]
        KnowledgeIntake._extend_section(lines, "Requirements", item.current_requirements)
        KnowledgeIntake._extend_section(lines, "Acceptance Criteria", item.acceptance_criteria)
        KnowledgeIntake._extend_section(lines, "Decisions", item.decisions)
        KnowledgeIntake._extend_section(lines, "Demo Feedback", item.demo_feedback)
        KnowledgeIntake._extend_section(lines, "Open Questions", item.open_questions)
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _list_markdown(item: KnowledgeObject, title: str, values: list[str]) -> str:
        lines = [f"# {title}: {item.title}", ""]
        if values:
            lines.extend(f"- {value}" for value in values)
        else:
            lines.append("No confirmed items yet.")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _events_markdown(item: KnowledgeObject) -> str:
        lines = [f"# Source Events: {item.title}", ""]
        for event in item.source_events:
            lines.extend(
                [
                    f"## {event.event_type.title()}: {event.title}",
                    "",
                    f"- Event ID: `{event.event_id}`",
                    f"- Recorded at: {event.recorded_at or '-'}",
                    f"- Loom ID: `{event.loom_video_id}`",
                    f"- Telegram: {event.telegram_post_url}",
                    f"- Loom: {event.loom_url}",
                    "",
                    event.summary or "No event summary.",
                    "",
                ]
            )
            KnowledgeIntake._extend_section(lines, "Decisions", event.decisions)
            KnowledgeIntake._extend_section(lines, "Action Items", event.action_items)
            KnowledgeIntake._extend_section(lines, "Acceptance Criteria", event.acceptance_criteria)
            KnowledgeIntake._extend_section(lines, "Open Questions", event.open_questions)
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _sources_markdown(item: KnowledgeObject) -> str:
        lines = [f"# Sources: {item.title}", ""]
        for event in item.source_events:
            lines.extend(
                [
                    f"## {event.event_id}",
                    "",
                    f"- Type: `{event.event_type}`",
                    f"- Title: {event.title}",
                    f"- Recorded at: {event.recorded_at or '-'}",
                    f"- Loom video ID: `{event.loom_video_id}`",
                    f"- Loom URL: {event.loom_url or '-'}",
                    f"- Telegram post: {event.telegram_post_url or '-'}",
                    f"- Google Doc: {event.google_doc_url or '-'}",
                    f"- Transcript Doc: {event.transcript_doc_url or '-'}",
                    f"- Tags: {', '.join(event.raw_tags) or '-'}",
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _prompt_context_markdown(item: KnowledgeObject) -> str:
        lines = [
            f"# Prompt Context: {item.title}",
            "",
            f"Object ID: `{item.object_id}`",
            f"System: `{item.system}`",
            f"Feature area: `{item.feature_area or 'unknown'}`",
            "",
            "Use `../machine_bundle/ai_context.json` when a tool supports JSON context.",
            "Use `../source_bundle/*.md` when a tool works better with Markdown sources.",
            "",
            "Important constraints:",
            "",
            "- Do not use `#daily` materials.",
            "- Prefer `demo` source events over older `discussion` source events in conflicts.",
            "- Return proposed patches with source event IDs.",
        ]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _extend_section(lines: list[str], title: str, values: list[str]) -> None:
        if not values:
            return
        lines.extend([f"## {title}", ""])
        lines.extend(f"- {value}" for value in values)
        lines.append("")

    @staticmethod
    def _clean_list(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if isinstance(value, dict):
                text = str(value.get("title") or value.get("text") or "").strip()
            else:
                text = str(value or "").strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _action_titles(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if isinstance(value, dict):
                title = str(value.get("title") or "").strip()
                owner = str(value.get("owner") or value.get("responsible") or "").strip()
                due = str(value.get("due") or value.get("due_date") or "").strip()
                parts = [title]
                if owner:
                    parts.append(f"Ответственный: {owner}")
                if due:
                    parts.append(f"Срок: {due}")
                text = "\n  ".join(part for part in parts if part)
            else:
                text = str(value or "").strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _merge_unique(existing: list[str], additions: list[str]) -> list[str]:
        result = list(existing)
        seen = {KnowledgeIntake._normalize(item) for item in existing}
        for item in additions:
            key = KnowledgeIntake._normalize(item)
            if key and key not in seen:
                result.append(item)
                seen.add(key)
        return result

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold()).strip()

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        return slug[:60] or "task"
