from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .kb_intake import DEMO_TAG, DISCUSSION_TAG, KnowledgeObject, KnowledgeSourceEvent
from .knowledge_repo import KnowledgeRepoResult, KnowledgeRepository


ARCHIVE_BLOCK_RE = re.compile(
    r"\[\[LOOM_VIDEO_ID:(?P<loom_id>[A-Za-z0-9_-]+)\]\](?P<body>.*?)(?:\[\[/LOOM_VIDEO_ID:(?P=loom_id)\]\]|\Z)",
    flags=re.DOTALL,
)

SECTION_TITLES = {
    "Metadata",
    "Summary",
    "Decisions",
    "Completed Today",
    "Action Items",
    "Remaining Tech Debt",
    "Business Requests For Estimation",
    "Blockers",
    "Technical Spec Draft",
    "Scope",
    "Functional Requirements",
    "Non-Functional Requirements",
    "Dependencies",
    "Acceptance Criteria",
    "Open Questions",
    "Telegram Digest",
}


class AicallorderArchiveImportResult(BaseModel):
    source_file: str
    source_url: str = ""
    dry_run: bool = False
    blocks_count: int = 0
    objects_count: int = 0
    object_ids: list[str] = Field(default_factory=list)
    skipped_blocks: list[dict[str, str]] = Field(default_factory=list)
    upsert: dict[str, Any] | None = None
    derive_catalogs: dict[str, Any] | None = None
    index: dict[str, Any] | None = None
    chunk_index: dict[str, Any] | None = None
    rag_index: dict[str, Any] | None = None
    external_export: dict[str, Any] | None = None
    notion_sync: dict[str, Any] | None = None


@dataclass(slots=True)
class ArchiveBlock:
    loom_id: str
    title: str
    metadata: dict[str, str]
    sections: dict[str, list[str]]
    raw_text: str


def import_aicallorder_archive(
    *,
    source_file: Path,
    knowledge_dir: Path,
    source_url: str = "",
    dry_run: bool = False,
    limit: int | None = None,
    include_untagged: bool = False,
    build_rag: bool = False,
    export_target: str = "none",
    sync_notion: bool = False,
    env: dict[str, str] | None = None,
) -> AicallorderArchiveImportResult:
    text = source_file.read_text(encoding="utf-8-sig")
    blocks = parse_aicallorder_archive(text)
    skipped_blocks: list[dict[str, str]] = []
    if not include_untagged:
        filtered: list[ArchiveBlock] = []
        for block in blocks:
            tags = {tag.casefold() for tag in _tags(block.title)}
            if {DEMO_TAG, DISCUSSION_TAG} & tags:
                filtered.append(block)
            else:
                skipped_blocks.append({"loom_id": block.loom_id, "title": block.title, "reason": "missing task tag"})
        blocks = filtered
    if limit is not None:
        blocks = blocks[:limit]
    objects = [archive_block_to_knowledge_object(block, source_url=source_url) for block in blocks]
    result = AicallorderArchiveImportResult(
        source_file=str(source_file),
        source_url=source_url,
        dry_run=dry_run,
        blocks_count=len(blocks),
        objects_count=len(objects),
        object_ids=[item.object_id for item in objects],
        skipped_blocks=skipped_blocks,
    )
    if dry_run:
        return result

    repo = KnowledgeRepository(knowledge_dir)
    result.upsert = repo.upsert_objects(objects).model_dump()
    result.derive_catalogs = repo.derive_catalogs().model_dump()
    result.index = repo.build_index().model_dump()
    result.chunk_index = repo.build_chunk_index().model_dump()
    if build_rag:
        from .knowledge_rag import KnowledgeVectorStore, client_from_env

        result.rag_index = KnowledgeVectorStore(knowledge_dir).build_from_chunk_index(
            client=client_from_env(env or {}),
            force=False,
        )
    if export_target != "none":
        result.external_export = repo.export_external_bundle(target=export_target).model_dump()
    if sync_notion:
        result.notion_sync = repo.notion_sync_plan(apply=True, env=env or {}).model_dump()
    return result


def parse_aicallorder_archive(text: str) -> list[ArchiveBlock]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks: list[ArchiveBlock] = []
    for match in ARCHIVE_BLOCK_RE.finditer(normalized):
        body = match.group("body").strip()
        if not body:
            continue
        blocks.append(_parse_block(loom_id=match.group("loom_id"), body=body))
    return blocks


def archive_block_to_knowledge_object(block: ArchiveBlock, *, source_url: str = "") -> KnowledgeObject:
    tags = _tags(block.title)
    if not tags:
        tags = [DEMO_TAG if "demo" in block.title.casefold() else DISCUSSION_TAG]
    event_type = "demo" if DEMO_TAG in {tag.casefold() for tag in tags} else "discussion"
    metadata = block.metadata
    title = _clean_title(block.title, block.loom_id)
    summary = _section_text(block.sections, "Summary")
    functional_requirements = _merge_unique(
        _section_bullets(block.sections, "Functional Requirements")
        + _section_bullets(block.sections, "Scope")
        + _section_bullets(block.sections, "Business Requests For Estimation")
    )
    action_items = _merge_unique(
        _section_bullets(block.sections, "Action Items")
        + _section_bullets(block.sections, "Completed Today")
        + _section_bullets(block.sections, "Remaining Tech Debt")
    )
    decisions = _section_bullets(block.sections, "Decisions")
    acceptance = _section_bullets(block.sections, "Acceptance Criteria")
    open_questions = _section_bullets(block.sections, "Open Questions")
    blockers = _section_bullets(block.sections, "Blockers")
    system, feature_area = _classify(title=title, text=block.raw_text)
    event = KnowledgeSourceEvent(
        event_id=f"{event_type}__{block.loom_id}",
        event_type=event_type,
        title=title,
        recorded_at=metadata.get("Recorded at"),
        loom_video_id=block.loom_id,
        loom_url=metadata.get("Source URL") or f"https://www.loom.com/share/{block.loom_id}",
        telegram_post_url="",
        google_doc_url=metadata.get("Summary Doc") or source_url or None,
        transcript_doc_url=metadata.get("Transcript Doc"),
        summary=summary,
        decisions=decisions,
        action_items=action_items,
        blockers=blockers,
        open_questions=open_questions,
        acceptance_criteria=acceptance,
        raw_tags=tags,
    )
    return KnowledgeObject(
        object_id=f"task_case__aicallorder_archive__{_slug(title)}__{block.loom_id[:8]}",
        title=title,
        system=system,
        feature_area=feature_area,
        source_tags=sorted({tag.casefold() for tag in tags}),
        linked_loom_ids=[block.loom_id],
        current_summary=summary,
        current_requirements=functional_requirements or action_items,
        acceptance_criteria=acceptance,
        decisions=decisions,
        open_questions=open_questions,
        demo_feedback=_merge_unique(blockers + open_questions + action_items) if event_type == "demo" else [],
        source_events=[event],
    )


def _parse_block(*, loom_id: str, body: str) -> ArchiveBlock:
    lines = [line.rstrip() for line in body.splitlines()]
    title = ""
    while lines and not title:
        title = lines.pop(0).strip()
    if title.casefold().startswith("meeting note:"):
        title = title.split(":", 1)[1].strip()
    sections: dict[str, list[str]] = {}
    current = ""
    for line in lines:
        stripped = line.strip()
        if stripped in SECTION_TITLES:
            current = stripped
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(line)
    metadata = _metadata(sections.get("Metadata") or [])
    if metadata.get("Loom video ID"):
        loom_id = metadata["Loom video ID"]
    return ArchiveBlock(loom_id=loom_id, title=title or loom_id, metadata=metadata, sections=sections, raw_text=body)


def _metadata(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _section_text(sections: dict[str, list[str]], name: str) -> str:
    lines = []
    for line in sections.get(name) or []:
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()


def _section_bullets(sections: dict[str, list[str]], name: str) -> list[str]:
    values: list[str] = []
    current = ""
    for line in sections.get(name) or []:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current:
                values.append(current.strip())
            current = stripped[2:].strip()
        elif current and re.match(r"^(Owner|Due|Status|Priority|Requested by|Context|Estimate notes):", stripped):
            current += f" | {stripped}"
        elif current:
            current += f" {stripped}"
        else:
            values.append(stripped)
    if current:
        values.append(current.strip())
    return _merge_unique(values)


def _tags(text: str) -> list[str]:
    return sorted({match.casefold() for match in re.findall(r"#[\wА-Яа-яІіЇїЄєҐґ-]+", text, flags=re.UNICODE)})


def _clean_title(title: str, loom_id: str) -> str:
    cleaned = re.sub(r"#task_(?:discussion|demo)\s*", "", title, flags=re.IGNORECASE).strip()
    return cleaned or loom_id


def _classify(*, title: str, text: str) -> tuple[str, str]:
    haystack = f"{title}\n{text}".casefold()
    if any(token in haystack for token in ("bitrix", "битрикс", "crm", "б24", "bitrix24", "assetpayments", "payments pro", "заказ")):
        system = "bitrix"
    elif any(token in haystack for token in ("aicallorder", "loom", "transcript")):
        system = "aicallorder"
    else:
        system = "unknown"
    feature_keywords = [
        ("payments", ("payment", "платеж", "оплат", "assetpayments")),
        ("discounts_balances", ("скидк", "ндс", "баланс", "кэшбэк", "cashback")),
        ("orders", ("заказ", "order")),
        ("comments", ("коммент", "comment")),
        ("checklists", ("чеклист", "checklist")),
        ("telegram_publication", ("telegram", "телеграм")),
        ("knowledge_base", ("knowledge", "база знаний", "notion")),
    ]
    feature_area = ""
    for value, keywords in feature_keywords:
        if any(keyword in haystack for keyword in keywords):
            feature_area = value
            break
    return system, feature_area


def _slug(value: str) -> str:
    raw = re.sub(r"[^\wА-Яа-яІіЇїЄєҐґ]+", "_", value.casefold(), flags=re.UNICODE).strip("_")
    if not raw:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return raw[:80].strip("_")


def _merge_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result
