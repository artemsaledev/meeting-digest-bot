from __future__ import annotations

from collections import OrderedDict
from datetime import date
import re
from typing import Any

from .models import DailyPersonPlan, DailyPlan, DailyPlanItem, MeetingRecord
from .people import PeopleDirectory, Person


PLAN_SECTION = "plan"
BLOCKER_SECTION = "blockers"
DONE_SECTION = "done"


class DailyPlanParser:
    def __init__(self, people: PeopleDirectory | None = None) -> None:
        self.people = people or PeopleDirectory.from_file()

    def parse_meetings(
        self,
        *,
        report_date: date,
        meetings: list[MeetingRecord],
        team_name: str = "Bitrix Develop Team",
    ) -> DailyPlan:
        people: "OrderedDict[int | str, DailyPersonPlan]" = OrderedDict()
        unmatched_items: list[str] = []
        source_meeting_ids: list[str] = []

        for meeting in meetings:
            source_meeting_ids.append(meeting.loom_video_id)
            parsed_people, parsed_unmatched = self.parse_text(
                text=meeting.transcript_text,
                source_meeting_id=meeting.loom_video_id,
                source_meeting_title=meeting.title,
            )
            unmatched_items.extend(parsed_unmatched)
            for parsed in parsed_people:
                key: int | str = parsed.bitrix_user_id or parsed.person_name
                if key not in people:
                    people[key] = DailyPersonPlan(
                        person_name=parsed.person_name,
                        bitrix_user_id=parsed.bitrix_user_id,
                    )
                people[key].plan_items.extend(parsed.plan_items)
                people[key].blockers.extend(parsed.blockers)

        return DailyPlan(
            report_date=report_date,
            team_name=team_name,
            source_meeting_ids=source_meeting_ids,
            people=list(people.values()),
            unmatched_items=unmatched_items,
        )

    def parse_text(
        self,
        *,
        text: str,
        source_meeting_id: str | None = None,
        source_meeting_title: str | None = None,
    ) -> tuple[list[DailyPersonPlan], list[str]]:
        lines = self._normalize_transcript_lines(text)
        result: list[DailyPersonPlan] = []
        unmatched_items: list[str] = []
        current_person: Person | None = None
        current_plan: DailyPersonPlan | None = None
        current_section = PLAN_SECTION

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            person, remainder = self._extract_person_header(line)
            if person:
                current_person = person
                current_plan = DailyPersonPlan(
                    person_name=person.full_name,
                    bitrix_user_id=person.bitrix_user_id,
                )
                result.append(current_plan)
                current_section = PLAN_SECTION
                if remainder:
                    section, item_text = self._extract_section_and_inline_item(remainder, current_section)
                    current_section = section
                    if item_text:
                        self._append_item(
                            current_plan=current_plan,
                            current_person=current_person,
                            item_text=item_text,
                            section=current_section,
                            source_meeting_id=source_meeting_id,
                            source_meeting_title=source_meeting_title,
                        )
                continue

            section, item_text = self._extract_section_and_inline_item(line, current_section)
            current_section = section
            if not item_text:
                continue

            for split_item in self._split_inline_items(item_text):
                if current_plan and current_person:
                    self._append_item(
                        current_plan=current_plan,
                        current_person=current_person,
                        item_text=split_item,
                        section=current_section,
                        source_meeting_id=source_meeting_id,
                        source_meeting_title=source_meeting_title,
                    )
                elif self._looks_like_task_item(split_item) and not self._is_noise_line(split_item):
                    unmatched_items.append(split_item)

        return self._dedupe_people_items(result), unmatched_items

    def _append_item(
        self,
        *,
        current_plan: DailyPersonPlan,
        current_person: Person,
        item_text: str,
        section: str,
        source_meeting_id: str | None,
        source_meeting_title: str | None,
    ) -> None:
        title = self._clean_item_text(item_text)
        title = self._strip_question_lead(title)
        if not title or self._is_noise_line(title):
            return
        if not self._looks_like_task_item(title) or self._is_conversational_line(title):
            return
        item = DailyPlanItem(
            title=title,
            person_name=current_person.full_name,
            bitrix_user_id=current_person.bitrix_user_id,
            source_meeting_id=source_meeting_id,
            source_meeting_title=source_meeting_title,
            item_type=BLOCKER_SECTION if section == BLOCKER_SECTION else PLAN_SECTION,
        )
        if section == BLOCKER_SECTION:
            current_plan.blockers.append(item)
        elif section != DONE_SECTION:
            current_plan.plan_items.append(item)

    def _extract_person_header(self, line: str) -> tuple[Person | None, str]:
        cleaned = self._strip_list_marker(line).strip()
        direct_person = self._find_exact_person(cleaned.rstrip(".:;-"))
        if direct_person and len(cleaned.split()) <= 4:
            return direct_person, ""

        for separator in (":", " - ", " — ", ". "):
            if separator in cleaned:
                head, tail = cleaned.split(separator, 1)
                person = self.people.find(head.strip())
                if person:
                    return person, tail.strip()

        for person in self.people.people:
            aliases = sorted((person.full_name, *person.aliases), key=len, reverse=True)
            for alias in aliases:
                pattern = re.compile(rf"^{re.escape(alias)}\b[,:;.\-\s]*(.*)$", flags=re.IGNORECASE)
                match = pattern.match(cleaned)
                remainder = match.group(1).strip() if match else ""
                if match:
                    prompt_tail = self._tail_after_person_prompt(remainder)
                    if prompt_tail is not None:
                        return person, prompt_tail
                    if self._is_structured_remainder(remainder):
                        return person, remainder
                    if not remainder:
                        return person, ""
                embedded = self._extract_embedded_person_prompt(cleaned, alias)
                if embedded is not None:
                    return person, embedded
        return None, ""

    def _extract_section_and_inline_item(self, line: str, current_section: str) -> tuple[str, str]:
        cleaned = self._strip_list_marker(line).strip()
        matched_section, matched_item = self._match_section_marker(cleaned)
        if matched_section:
            return matched_section, matched_item
        return current_section, cleaned

    def _normalize_transcript_lines(self, text: str) -> list[str]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$", "", normalized)
        normalized = re.sub(r"(?m)^\s*(Transcript|Summary|Metadata)\s*$", "", normalized, flags=re.IGNORECASE)
        lines: list[str] = []
        for raw_line in normalized.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", line):
                continue
            lines.append(line)
        return lines

    @staticmethod
    def _strip_list_marker(line: str) -> str:
        return re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()

    @staticmethod
    def _split_inline_items(text: str) -> list[str]:
        cleaned = text.strip()
        if not cleaned:
            return []
        parts = re.split(r"\s*(?:;|\n)\s*", cleaned)
        if len(parts) == 1:
            return [cleaned]
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _clean_item_text(text: str) -> str:
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", text).strip()
        cleaned = cleaned.strip(" .;")
        return cleaned

    @staticmethod
    def _looks_like_task_item(text: str) -> bool:
        return bool(text.strip()) and "?" not in text.strip()

    @staticmethod
    def _is_plan_marker(normalized_line: str) -> bool:
        markers = {
            "план",
            "план на сегодня",
            "задачи",
            "задачи на сегодня",
            "сегодня",
            "что делаю сегодня",
        }
        return any(normalized_line == marker or normalized_line.startswith(marker + " ") for marker in markers)

    @staticmethod
    def _is_blocker_marker(normalized_line: str) -> bool:
        markers = {
            "блокер",
            "блокеры",
            "проблемы",
            "зависимости",
            "жду",
        }
        return any(normalized_line == marker or normalized_line.startswith(marker + " ") for marker in markers)

    @staticmethod
    def _is_done_marker(normalized_line: str) -> bool:
        markers = {
            "вчера",
            "сделано",
            "сделал",
            "что сделал",
            "готово",
        }
        return any(normalized_line == marker or normalized_line.startswith(marker + " ") for marker in markers)

    @staticmethod
    def _is_noise_line(text: str) -> bool:
        normalized = PeopleDirectory.normalize_name(text)
        if normalized in {
            "нет",
            "нет блокеров",
            "без блокеров",
            "не знаю",
            "ок",
            "окей",
            "спасибо",
        }:
            return True
        if normalized.startswith("daily ") or normalized.startswith("команда "):
            return True
        return any(
            marker in normalized
            for marker in (
                "нічого не робив",
                "ничего не делал",
                "дякую за перегляд",
                "спасибо за просмотр",
                "у мене поки все",
                "у меня пока все",
                "я не знаю до кого",
                "ігор прочитає функціонал",
                "игор прочитает функционал",
                "ну він мені вказав",
                "ну он мне указал",
                "щоб файликом загружати",
            )
        )

    def _is_conversational_line(self, text: str) -> bool:
        normalized = PeopleDirectory.normalize_name(text)
        if self._contains_person_prompt(normalized):
            return True
        if len(text) > 260:
            return True
        return normalized.startswith(
            (
                "ну ми тоді",
                "ну мы тогда",
                "давайте",
                "зараз",
                "сейчас",
                "окей",
                "добре",
                "хорошо",
                "так в принципі",
                "так в принципе",
            )
        )

    @staticmethod
    def _strip_question_lead(text: str) -> str:
        if "?" not in text:
            return text
        tail = text.split("?")[-1].strip()
        return tail or ""

    def _find_exact_person(self, value: str) -> Person | None:
        normalized = PeopleDirectory.normalize_name(value)
        if not normalized:
            return None
        for person in self.people.people:
            for alias in (person.full_name, *person.aliases):
                if normalized == PeopleDirectory.normalize_name(alias):
                    return person
        return None

    def _is_structured_remainder(self, remainder: str) -> bool:
        if not remainder:
            return False
        normalized = PeopleDirectory.normalize_name(remainder)
        if normalized.startswith(("ты ", "у тебя ", "скажи ", "подскажи ", "можешь ", "когда ", "что ")):
            return False
        section, item = self._match_section_marker(remainder)
        return bool(section and (item or normalized in self._all_section_markers()))

    def _extract_embedded_person_prompt(self, line: str, alias: str) -> str | None:
        if not re.search(rf"\b{re.escape(alias)}\b", line, flags=re.IGNORECASE):
            return None
        normalized = PeopleDirectory.normalize_name(line)
        if not self._contains_person_prompt(normalized):
            return None
        parts = re.split(rf"\b{re.escape(alias)}\b", line, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) < 2:
            return None
        return self._tail_after_person_prompt(parts[1]) or ""

    def _tail_after_person_prompt(self, text: str) -> str | None:
        normalized = PeopleDirectory.normalize_name(text)
        if not normalized or not self._contains_person_prompt(normalized):
            return None
        if "?" in text:
            return text.split("?", 1)[1].strip()
        lowered = text.casefold()
        for marker in ("план.", "план:", "план -", "план —"):
            index = lowered.find(marker)
            if index >= 0:
                return text[index + len(marker) :].strip()
        return ""

    @staticmethod
    def _contains_person_prompt(normalized: str) -> bool:
        return any(
            marker in normalized
            for marker in (
                "який в тебе",
                "какой у тебя",
                "у тебе який",
                "у тебя какой",
                "в тебе який",
                "в тебе є",
                "що в тебе в роботі",
                "что у тебя в работе",
                "твій план",
                "свій план",
                "план тестів",
                "план тестов",
            )
        )

    @classmethod
    def _match_section_marker(cls, line: str) -> tuple[str | None, str]:
        cleaned = line.strip()
        normalized = PeopleDirectory.normalize_name(cleaned)
        for section, markers in cls._section_markers().items():
            for marker in sorted(markers, key=len, reverse=True):
                if normalized == marker:
                    return section, ""
                if normalized.startswith(marker + " "):
                    if marker == "жду":
                        return section, cleaned
                    return section, cls._tail_after_marker(cleaned, marker)
        return None, ""

    @staticmethod
    def _tail_after_marker(line: str, marker: str) -> str:
        # normalize_name keeps character count stable for the supported Cyrillic
        # markers, so this lets us preserve the original casing of the task text.
        return line[len(marker) :].lstrip(" :;—-")

    @staticmethod
    def _section_markers() -> dict[str, set[str]]:
        return {
            PLAN_SECTION: {
                "план",
                "план на сегодня",
                "задачи",
                "задачи на сегодня",
                "сегодня",
                "что делаю сегодня",
            },
            BLOCKER_SECTION: {
                "блокер",
                "блокеры",
                "проблемы",
                "зависимости",
                "жду",
            },
            DONE_SECTION: {
                "вчера",
                "сделано",
                "сделал",
                "что сделал",
                "готово",
            },
        }

    @classmethod
    def _all_section_markers(cls) -> set[str]:
        result: set[str] = set()
        for markers in cls._section_markers().values():
            result.update(markers)
        return result

    @staticmethod
    def _dedupe_people_items(plans: list[DailyPersonPlan]) -> list[DailyPersonPlan]:
        for plan in plans:
            plan.plan_items = DailyPlanParser._dedupe_items(plan.plan_items)
            plan.blockers = DailyPlanParser._dedupe_items(plan.blockers)
        return [plan for plan in plans if plan.plan_items or plan.blockers]

    @staticmethod
    def _dedupe_items(items: list[DailyPlanItem]) -> list[DailyPlanItem]:
        result: list[DailyPlanItem] = []
        seen: set[str] = set()
        for item in items:
            key = PeopleDirectory.normalize_name(item.title)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


class DailyPlanV2Parser:
    """Build daily plans from structured Loom artifacts first, transcript second.

    The first implementation parsed raw speech directly. That is useful as a
    fallback, but it turns pauses, questions, and "I finished this on Friday"
    into checklists too easily. V2 treats the already processed AIcallorder
    digest as the source of truth for commitments and keeps transcript parsing
    only for posts where action_items were not produced.
    """

    DONE_STATUS_MARKERS = {
        "done",
        "closed",
        "completed",
        "готово",
        "выполнено",
        "виконано",
        "завершено",
        "закрыто",
        "закрито",
    }
    META_COMMITMENT_MARKERS = {
        "формат по дейлі",
        "формат по daily",
        "просто пропоную",
        "буду протанно обробляти",
        "послідовно йти по людям",
        "последовательно идти по людям",
        "давайте порядок",
        "можемо розбігатися",
    }

    def __init__(self, people: PeopleDirectory | None = None, fallback: DailyPlanParser | None = None) -> None:
        self.people = people or PeopleDirectory.from_file()
        self.fallback = fallback or DailyPlanParser(self.people)

    def parse_meetings(
        self,
        *,
        report_date: date,
        meetings: list[MeetingRecord],
        team_name: str = "Bitrix Develop Team",
    ) -> DailyPlan:
        people: "OrderedDict[int | str, DailyPersonPlan]" = OrderedDict()
        unmatched_items: list[str] = []
        source_meeting_ids: list[str] = []
        summaries: list[str] = []
        global_blockers: list[str] = []
        completed_items: list[str] = []
        decisions: list[str] = []
        review_notes: list[str] = []

        for meeting in meetings:
            source_meeting_ids.append(meeting.loom_video_id)
            artifacts = meeting.artifacts or {}
            summary = self._as_text(artifacts.get("summary"))
            if summary:
                summaries.append(f"{meeting.title}: {summary}")

            meeting_decisions = self._clean_list(artifacts.get("decisions"))
            decisions.extend(self._unique_missing(decisions, meeting_decisions))
            meeting_blockers = self._clean_list(artifacts.get("blockers"))
            global_blockers.extend(self._unique_missing(global_blockers, meeting_blockers))
            meeting_completed = self._clean_list(artifacts.get("completed_today"))
            completed_items.extend(self._unique_missing(completed_items, meeting_completed))

            action_items = artifacts.get("action_items")
            if isinstance(action_items, list) and action_items:
                self._append_artifact_actions(
                    people=people,
                    unmatched_items=unmatched_items,
                    completed_items=completed_items,
                    meeting=meeting,
                    values=action_items,
                )
                continue

            fallback_plan = self.fallback.parse_meetings(
                report_date=report_date,
                meetings=[meeting],
                team_name=team_name,
            )
            self._merge_people(people, fallback_plan.people)
            unmatched_items.extend(self._unique_missing(unmatched_items, fallback_plan.unmatched_items))
            review_notes.append(
                f"{meeting.title}: action_items не найдены, использован fallback-разбор транскрипта."
            )

        for person_plan in people.values():
            person_plan.plan_items = DailyPlanParser._dedupe_items(person_plan.plan_items)
            person_plan.blockers = DailyPlanParser._dedupe_items(person_plan.blockers)

        return DailyPlan(
            report_date=report_date,
            team_name=team_name,
            source_meeting_ids=source_meeting_ids,
            summary="\n".join(summaries[:5]).strip(),
            global_blockers=global_blockers,
            completed_items=completed_items,
            decisions=decisions,
            review_notes=review_notes,
            people=[plan for plan in people.values() if plan.plan_items or plan.blockers],
            unmatched_items=unmatched_items,
        )

    def _append_artifact_actions(
        self,
        *,
        people: "OrderedDict[int | str, DailyPersonPlan]",
        unmatched_items: list[str],
        completed_items: list[str],
        meeting: MeetingRecord,
        values: list[Any],
    ) -> None:
        for value in values:
            title = self._action_title(value)
            if not title:
                continue
            status = self._action_status(value)
            owner = self._action_owner(value)
            person = self._resolve_owner(owner=owner, title=title)
            cleaned_title = self._clean_commitment_title(title)

            if not cleaned_title or self._is_meta_commitment(cleaned_title):
                continue
            if self._is_done_status(status):
                completed_items.extend(self._unique_missing(completed_items, [cleaned_title]))
                continue
            if not person:
                label = cleaned_title if not owner else f"{cleaned_title} | owner={owner}"
                unmatched_items.extend(self._unique_missing(unmatched_items, [label]))
                continue

            key: int | str = person.bitrix_user_id or person.full_name
            if key not in people:
                people[key] = DailyPersonPlan(
                    person_name=person.full_name,
                    bitrix_user_id=person.bitrix_user_id,
                )
            people[key].plan_items.append(
                DailyPlanItem(
                    title=cleaned_title,
                    person_name=person.full_name,
                    bitrix_user_id=person.bitrix_user_id,
                    source_meeting_id=meeting.loom_video_id,
                    source_meeting_title=meeting.title,
                    item_type=PLAN_SECTION,
                )
            )

    def _resolve_owner(self, *, owner: str, title: str) -> Person | None:
        person = self.people.find(owner)
        if person:
            return person
        normalized_owner = PeopleDirectory.normalize_name(owner)
        if "миша" in normalized_owner or "міша" in normalized_owner:
            return self.people.find("Миша")
        if "ваня" in normalized_owner:
            return self.people.find("Ваня")
        if normalized_owner in {"саша", "саша у відпустці", "саша в отпуске"}:
            return None

        # Sometimes owner is omitted, but the action title starts with a name.
        for person_candidate in self.people.people:
            aliases = (person_candidate.full_name, *person_candidate.aliases)
            if any(PeopleDirectory.normalize_name(title).startswith(PeopleDirectory.normalize_name(alias) + " ") for alias in aliases):
                return person_candidate
        return None

    @staticmethod
    def _action_title(value: Any) -> str:
        if isinstance(value, dict):
            return DailyPlanV2Parser._as_text(value.get("title") or value.get("name") or "")
        return DailyPlanV2Parser._as_text(value)

    @staticmethod
    def _action_owner(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        return DailyPlanV2Parser._as_text(value.get("owner") or value.get("responsible") or "")

    @staticmethod
    def _action_status(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        return DailyPlanV2Parser._as_text(value.get("status") or "")

    @classmethod
    def _clean_commitment_title(cls, title: str) -> str:
        cleaned = cls._as_text(title)
        cleaned = re.sub(r"\s*\|\s*(?:owner|due|due_date|status)\s*=.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" .;")

    @classmethod
    def _is_done_status(cls, status: str) -> bool:
        normalized = PeopleDirectory.normalize_name(status)
        return any(marker in normalized for marker in cls.DONE_STATUS_MARKERS)

    @classmethod
    def _is_meta_commitment(cls, title: str) -> bool:
        normalized = PeopleDirectory.normalize_name(title)
        if "?" in title:
            return True
        return any(marker in normalized for marker in cls.META_COMMITMENT_MARKERS)

    @staticmethod
    def _clean_list(values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            text = DailyPlanV2Parser._as_text(value.get("title") if isinstance(value, dict) else value)
            if text:
                result.append(text)
        return result

    @staticmethod
    def _as_text(value: object) -> str:
        text = str(value or "").replace("\u00a0", " ").strip()
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines).strip()

    @staticmethod
    def _merge_people(target: "OrderedDict[int | str, DailyPersonPlan]", source: list[DailyPersonPlan]) -> None:
        for parsed in source:
            key: int | str = parsed.bitrix_user_id or parsed.person_name
            if key not in target:
                target[key] = DailyPersonPlan(
                    person_name=parsed.person_name,
                    bitrix_user_id=parsed.bitrix_user_id,
                )
            target[key].plan_items.extend(parsed.plan_items)
            target[key].blockers.extend(parsed.blockers)

    @staticmethod
    def _unique_missing(existing: list[str], candidates: list[str]) -> list[str]:
        seen = {PeopleDirectory.normalize_name(item) for item in existing}
        result: list[str] = []
        for item in candidates:
            key = PeopleDirectory.normalize_name(item)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result
