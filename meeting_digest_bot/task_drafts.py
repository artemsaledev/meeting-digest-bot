from __future__ import annotations

from typing import Any

from .models import ChecklistGroup, ChecklistItem, DailyPlan, DailyRollup, MeetingRecord, PublicationRecord, TaskDraft, WeeklyRollup


CRM_COMMENT_FORMAT_RULES = """
Bitrix task comments must be plain text. Do not use Markdown tables, HTML tables,
or pipe-delimited pseudo-tables. If source data is structured, render it as:
- Main item text
  Field: value
  Field: value
""".strip()


def build_meeting_task_draft(
    *,
    meeting: MeetingRecord,
    publication: PublicationRecord | None,
    default_tags: list[str] | None = None,
) -> TaskDraft:
    artifacts = meeting.artifacts or {}
    tech_spec = artifacts.get("technical_spec_draft") or {}
    summary = str(artifacts.get("summary") or "").strip()
    decisions = _clean_list(artifacts.get("decisions"))
    blockers = _clean_list(artifacts.get("blockers"))
    action_items = _extract_action_item_titles(artifacts.get("action_items"))
    completed_today = _clean_list(artifacts.get("completed_today"))
    tech_debt = _clean_list(artifacts.get("remaining_tech_debt"))
    business_requests = _extract_business_request_titles(artifacts.get("business_requests_for_estimation"))
    scope = _clean_list(tech_spec.get("scope"))
    functional = _clean_list(tech_spec.get("functional_requirements"))
    dependencies = _clean_list(tech_spec.get("dependencies"))
    acceptance = _clean_list(tech_spec.get("acceptance_criteria"))
    open_questions = _clean_list(tech_spec.get("open_questions"))

    title = str(tech_spec.get("title") or "").strip() or meeting.title.strip()
    description_parts = [
        f"Встреча: {meeting.title}",
        f"Loom: {meeting.source_url}",
    ]
    if publication and publication.google_doc_url:
        description_parts.append(f"Summary Doc: {publication.google_doc_url}")
    if publication and publication.transcript_doc_url:
        description_parts.append(f"Transcript Doc: {publication.transcript_doc_url}")
    if summary:
        description_parts.extend(["", "Краткое резюме", summary])
    if decisions:
        description_parts.extend(["", "Принятые решения", *_to_bullets(decisions)])
    if scope:
        description_parts.extend(["", "Scope", *_to_bullets(scope)])
    if functional:
        description_parts.extend(["", "Функциональные требования", *_to_bullets(functional)])
    if dependencies:
        description_parts.extend(["", "Зависимости", *_to_bullets(dependencies)])
    if open_questions:
        description_parts.extend(["", "Открытые вопросы", *_to_bullets(open_questions)])
    if blockers:
        description_parts.extend(["", "Блокеры", *_to_bullets(blockers)])

    checklist_groups = [
        ChecklistGroup(title="QA", items=acceptance or action_items[:8]),
        ChecklistGroup(title="Критерии приемки PM", items=acceptance),
    ]

    return TaskDraft(
        title=title,
        description="\n".join(part for part in description_parts if part is not None).strip(),
        comment=_build_meeting_comment(
            meeting=meeting,
            publication=publication,
            summary=summary,
            decisions=decisions,
            action_items=_extract_action_item_details(artifacts.get("action_items")),
            completed_today=completed_today,
            blockers=blockers,
            tech_debt=tech_debt,
            business_requests=business_requests,
            open_questions=open_questions,
        ),
        checklist_groups=[group for group in checklist_groups if group.items],
        tags=list(default_tags or []),
        meta={
            "loom_video_id": meeting.loom_video_id,
            "meeting_type": meeting.meeting_type,
            "recorded_at": meeting.recorded_at,
            "post_url": publication.post_url if publication else None,
            "google_doc_url": publication.google_doc_url if publication else None,
            "transcript_doc_url": publication.transcript_doc_url if publication else None,
        },
    )


def build_weekly_task_draft(
    *,
    rollup: WeeklyRollup,
    default_tags: list[str] | None = None,
) -> TaskDraft:
    title = f"Неделя {rollup.week_from.strftime('%d.%m')}-{rollup.week_to.strftime('%d.%m')}"
    description_parts = [
        f"Период: {rollup.week_from.isoformat()} - {rollup.week_to.isoformat()}",
    ]
    if rollup.summary:
        description_parts.extend(["", "Summary недели", rollup.summary])
    if rollup.blockers:
        description_parts.extend(["", "Блокеры", *_to_bullets(rollup.blockers)])
    if rollup.tech_debt:
        description_parts.extend(["", "Осталось / техдолг", *_to_bullets(rollup.tech_debt)])
    if rollup.business_requests:
        description_parts.extend(["", "На оценку", *_to_bullets(rollup.business_requests)])
    if rollup.source_meeting_ids:
        description_parts.extend(["", "Источник встреч", *_to_bullets(rollup.source_meeting_ids)])

    checklist_groups = [
        ChecklistGroup(title="Обязательства недели", items=rollup.commitments),
    ]
    return TaskDraft(
        title=title,
        description="\n".join(description_parts).strip(),
        comment="Weekly digest синхронизирован в CRM.",
        checklist_groups=[group for group in checklist_groups if group.items],
        tags=list(default_tags or []),
        meta={
            "week_from": rollup.week_from.isoformat(),
            "week_to": rollup.week_to.isoformat(),
        },
    )


def build_daily_task_draft(
    *,
    rollup: DailyRollup,
    default_tags: list[str] | None = None,
) -> TaskDraft:
    title = f"День {rollup.report_date.strftime('%d.%m.%Y')}"
    description_parts = [
        f"Дата: {rollup.report_date.isoformat()}",
    ]
    if rollup.summary:
        description_parts.extend(["", "Summary дня", rollup.summary])
    if rollup.blockers:
        description_parts.extend(["", "Блокеры", *_to_bullets(rollup.blockers)])
    if rollup.tech_debt:
        description_parts.extend(["", "Осталось / техдолг", *_to_bullets(rollup.tech_debt)])
    if rollup.business_requests:
        description_parts.extend(["", "На оценку", *_to_bullets(rollup.business_requests)])
    if rollup.source_meeting_ids:
        description_parts.extend(["", "Источник встреч", *_to_bullets(rollup.source_meeting_ids)])

    return TaskDraft(
        title=title,
        description="\n".join(description_parts).strip(),
        comment="Daily digest синхронизирован в CRM.",
        checklist_groups=[
            ChecklistGroup(title="Обязательства дня", items=rollup.commitments),
        ],
        tags=list(default_tags or []),
        meta={"report_date": rollup.report_date.isoformat()},
    )


def build_daily_plan_task_draft(
    *,
    plan: DailyPlan,
    default_tags: list[str] | None = None,
) -> TaskDraft:
    title = f"План дня {plan.report_date.strftime('%d.%m.%Y')} / {plan.team_name}"
    description_parts = [
        f"Дата: {plan.report_date.isoformat()}",
        f"Команда: {plan.team_name}",
    ]
    if plan.source_meeting_ids:
        description_parts.extend(["", "Источник #daily встреч", *_to_bullets(plan.source_meeting_ids)])

    if plan.people:
        description_parts.extend(["", "План по людям"])
        for person_plan in plan.people:
            description_parts.append(f"{person_plan.person_name} ({person_plan.bitrix_user_id or 'без Bitrix ID'})")
            if person_plan.plan_items:
                description_parts.extend(_to_bullets([item.title for item in person_plan.plan_items]))
            if person_plan.blockers:
                description_parts.append("Блокеры:")
                description_parts.extend(_to_bullets([item.title for item in person_plan.blockers]))

    if plan.unmatched_items:
        description_parts.extend(["", "Не удалось назначить ответственного", *_to_bullets(plan.unmatched_items)])

    checklist_groups: list[ChecklistGroup] = []
    for person_plan in plan.people:
        items: list[ChecklistItem] = []
        for item in person_plan.plan_items:
            members = [item.bitrix_user_id] if item.bitrix_user_id else []
            items.append(
                ChecklistItem(
                    title=item.title,
                    members=members,
                    meta={
                        "person_name": item.person_name,
                        "item_type": item.item_type,
                        "source_meeting_id": item.source_meeting_id,
                    },
                )
            )
        if person_plan.blockers:
            for item in person_plan.blockers:
                members = [item.bitrix_user_id] if item.bitrix_user_id else []
                items.append(
                    ChecklistItem(
                        title=f"Блокер: {item.title}",
                        members=members,
                        meta={
                            "person_name": item.person_name,
                            "item_type": item.item_type,
                            "source_meeting_id": item.source_meeting_id,
                        },
                    )
                )
        if items:
            checklist_groups.append(ChecklistGroup(title=person_plan.person_name, items=items))

    comment_lines = [
        f"План дня сформирован из #daily встреч за {plan.report_date.strftime('%d.%m.%Y')}.",
        f"Команда: {plan.team_name}",
        f"Ответственных найдено: {len(plan.people)}",
    ]
    if plan.unmatched_items:
        comment_lines.append(f"Без ответственного: {len(plan.unmatched_items)}")

    return TaskDraft(
        title=title,
        description="\n".join(description_parts).strip(),
        comment="\n".join(comment_lines).strip(),
        checklist_groups=checklist_groups,
        tags=list(default_tags or []) + ["daily-plan"],
        meta={
            "report_date": plan.report_date.isoformat(),
            "team_name": plan.team_name,
            "source_meeting_ids": plan.source_meeting_ids,
            "daily_plan": True,
        },
    )


def _clean_list(values: object) -> list[str]:
    result: list[str] = []
    if not isinstance(values, list):
        return result
    for value in values:
        text = _plain_text_for_crm(value)
        if text:
            result.append(text)
    return result


def _extract_action_item_titles(values: object) -> list[str]:
    result: list[str] = []
    if not isinstance(values, list):
        return result
    for value in values:
        if isinstance(value, dict):
            text = _plain_text_for_crm(value.get("title") or "")
        else:
            text = _plain_text_for_crm(value)
        if text:
            result.append(text)
    return result


def _extract_action_item_details(values: object) -> list[str]:
    result: list[str] = []
    if not isinstance(values, list):
        return result
    for value in values:
        if isinstance(value, dict):
            title = _plain_text_for_crm(value.get("title") or "")
            if not title:
                continue
            result.append(
                _format_detail_block(
                    title,
                    [
                        ("Ответственный", value.get("owner")),
                        ("Срок", value.get("due") or value.get("due_date")),
                        ("Статус", value.get("status")),
                    ],
                )
            )
        else:
            text = _plain_text_for_crm(value)
            if text:
                result.append(text)
    return result


def _extract_business_request_titles(values: object) -> list[str]:
    result: list[str] = []
    if not isinstance(values, list):
        return result
    for value in values:
        if isinstance(value, dict):
            title = _plain_text_for_crm(value.get("title") or "")
            if title:
                result.append(
                    _format_detail_block(
                        title,
                        [
                            ("Приоритет", value.get("priority")),
                            ("Инициатор", value.get("requested_by")),
                            ("Контекст", value.get("context")),
                            ("Заметки к оценке", value.get("estimate_notes")),
                        ],
                    )
                )
        else:
            text = _plain_text_for_crm(value)
            if text:
                result.append(text)
    return result


def _build_meeting_comment(
    *,
    meeting: MeetingRecord,
    publication: PublicationRecord | None,
    summary: str,
    decisions: list[str],
    action_items: list[str],
    completed_today: list[str],
    blockers: list[str],
    tech_debt: list[str],
    business_requests: list[str],
    open_questions: list[str],
) -> str:
    payload = publication.payload_json if publication else {}
    lines = [
        f"Итоги встречи: {meeting.title}",
        "",
        "Результаты обработки транскрипции",
    ]
    if summary:
        lines.extend(["", "Краткое резюме", summary])
    _extend_section(lines, "Принятые решения", decisions, limit=10)
    _extend_section(lines, "Action items / обязательства", action_items, limit=12)
    _extend_section(lines, "Сделано / подтверждено на встрече", completed_today, limit=8)
    _extend_section(lines, "Блокеры", blockers, limit=8)
    _extend_section(lines, "Осталось / техдолг", tech_debt, limit=8)
    _extend_section(lines, "Запросы на оценку", business_requests, limit=8)
    _extend_section(lines, "Открытые вопросы", open_questions, limit=8)

    lines.extend(["", "Links", f"Loom: {meeting.source_url}"])
    if publication and publication.google_doc_url:
        lines.append(f"Google Doc: {publication.google_doc_url}")
    if payload.get("doc_section_title"):
        lines.append(f"Doc section: {payload.get('doc_section_title')}")
    if publication and publication.transcript_doc_url:
        lines.append(f"Transcript Doc: {publication.transcript_doc_url}")
    if payload.get("transcript_section_title"):
        lines.append(f"Transcript section: {payload.get('transcript_section_title')}")
    return _truncate_comment("\n".join(lines).strip())


def _extend_section(lines: list[str], title: str, items: list[str], *, limit: int) -> None:
    if not items:
        return
    lines.extend(["", title, *_to_bullets(items[:limit])])


def _format_detail_block(title: str, fields: list[tuple[str, object]]) -> str:
    lines = [_plain_text_for_crm(title)]
    for label, raw_value in fields:
        value = _plain_text_for_crm(raw_value)
        if not value or value == "-":
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _plain_text_for_crm(value: object) -> str:
    text = str(value or "").replace("\u00a0", " ").strip()
    if not text:
        return ""
    text = _markdown_tables_to_lines(text)
    text = _pipe_metadata_to_lines(text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _markdown_tables_to_lines(text: str) -> str:
    lines = text.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _looks_like_markdown_table_row(line) and index + 1 < len(lines) and _is_markdown_table_separator(lines[index + 1]):
            headers = _split_markdown_table_row(line)
            index += 2
            while index < len(lines) and _looks_like_markdown_table_row(lines[index]):
                values = _split_markdown_table_row(lines[index])
                pairs = []
                for header, value in zip(headers, values):
                    if header and value:
                        pairs.append(f"{header}: {value}")
                if pairs:
                    result.append("; ".join(pairs))
                index += 1
            continue
        result.append(line)
        index += 1
    return "\n".join(result)


def _looks_like_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|").strip()
    if not stripped:
        return False
    return all(char in "-:| " for char in stripped) and "-" in stripped


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _pipe_metadata_to_lines(text: str) -> str:
    converted: list[str] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("|")]
        has_key_value = any("=" in part for part in parts[1:])
        if len(parts) <= 1 or not has_key_value:
            converted.append(line)
            continue
        converted.append(parts[0])
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = [chunk.strip() for chunk in part.split("=", 1)]
            if not value or value == "-":
                continue
            label = _pipe_metadata_label(key)
            converted.append(f"{label}: {value}")
    return "\n".join(converted)


def _pipe_metadata_label(key: str) -> str:
    labels = {
        "owner": "Ответственный",
        "due": "Срок",
        "due_date": "Срок",
        "status": "Статус",
        "priority": "Приоритет",
        "requested_by": "Инициатор",
        "context": "Контекст",
        "estimate_notes": "Заметки к оценке",
    }
    return labels.get(key.strip().lower(), key.strip())


def _truncate_comment(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n...Комментарий сокращен, полный текст доступен в Google Doc/Transcript Doc."


def _checklist_text(value: Any) -> str:
    if isinstance(value, ChecklistItem):
        return value.title.strip()
    if isinstance(value, dict):
        return str(value.get("title") or value.get("TITLE") or "").strip()
    return _plain_text_for_crm(value)


def _to_bullets(values: list[Any]) -> list[str]:
    result: list[str] = []
    for item in values:
        item_lines = _checklist_text(item).splitlines()
        if not item_lines:
            continue
        result.append(f"- {item_lines[0]}")
        for line in item_lines[1:]:
            if line.strip():
                result.append(f"  {line.strip()}")
    return result
