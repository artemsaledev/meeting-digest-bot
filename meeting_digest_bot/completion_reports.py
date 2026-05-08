from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any

from .people import PeopleDirectory, Person


@dataclass(slots=True)
class ChecklistCompletionItem:
    title: str
    group_title: str
    is_complete: bool
    category: str = "person"
    bitrix_user_id: int | None = None
    person_name: str = ""
    telegram_username: str = ""


@dataclass(slots=True)
class DailyCompletionReport:
    report_date: date
    team_name: str
    task_id: int
    task_url: str
    task_title: str = ""
    task_description: str = ""
    total_items: int = 0
    completed_items: int = 0
    closed_items: list[ChecklistCompletionItem] = field(default_factory=list)
    open_items: list[ChecklistCompletionItem] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return len(self.open_items)


class CompletionReportBuilder:
    def __init__(self, people: PeopleDirectory | None = None) -> None:
        self.people = people or PeopleDirectory.from_file()

    def build_daily(
        self,
        *,
        report_date: date,
        team_name: str,
        task_id: int,
        task_url: str,
        task_title: str = "",
        task_description: str = "",
        checklist_rows: list[dict[str, Any]],
    ) -> DailyCompletionReport:
        parents = self._parent_titles(checklist_rows)
        report = DailyCompletionReport(
            report_date=report_date,
            team_name=team_name,
            task_id=task_id,
            task_url=task_url,
            task_title=task_title,
            task_description=task_description,
        )

        for row in checklist_rows:
            parent_id = str(row.get("PARENT_ID") or row.get("parentId") or row.get("parent_id") or "0")
            if parent_id in {"0", ""}:
                continue
            title = str(row.get("TITLE") or row.get("title") or "").strip()
            if not title:
                continue
            group_title = parents.get(parent_id, "")
            category = self._category_for_group(group_title)
            person = self._person_for_row(row, group_title, category)
            item = ChecklistCompletionItem(
                title=title,
                group_title=group_title,
                is_complete=self._is_complete(row),
                category=category,
                bitrix_user_id=person.bitrix_user_id if person else None,
                person_name=person.full_name if person else self._fallback_label_for_group(group_title, category),
                telegram_username=person.telegram_username if person else "",
            )
            report.total_items += 1
            if item.is_complete:
                report.completed_items += 1
                report.closed_items.append(item)
            else:
                report.open_items.append(item)
        return report

    def format_daily_comment(self, report: DailyCompletionReport) -> str:
        lines = [
            f"Итоги выполнения плана дня {report.report_date.strftime('%d.%m.%Y')}",
            f"Команда: {report.team_name}",
            f"Задача: #{report.task_id}",
            report.task_url,
            "",
            f"Всего пунктов: {report.total_items}",
            f"Выполнено: {report.completed_items}",
            f"Не закрыто: {report.open_count}",
        ]
        if not report.open_items:
            lines.extend(["", "Все пункты чек-листа закрыты."])
            return "\n".join(lines).strip()

        person_open = self._dedupe_items(self._person_items(report.open_items))
        pm_open = self._dedupe_items(self._pm_items(report.open_items))
        other_open = self._dedupe_items(self._other_items(report.open_items))

        if person_open:
            lines.extend(["", "Не закрыто по ответственным:"])
        for person_label, items in self._group_open_items(person_open).items():
            lines.append(person_label)
            lines.extend(f"- {item.title}" for item in items)

        if pm_open:
            lines.extend(["", "PM-контроль / ручная проверка:"])
            for group_title, items in self._group_by_title(pm_open).items():
                lines.append(group_title)
                lines.extend(f"- {item.title}" for item in items)

        if other_open:
            lines.extend(["", "Не закрыто без явного ответственного:"])
            lines.extend(f"- {item.group_title}: {item.title}" for item in other_open)
        return "\n".join(lines).strip()

    def format_daily_telegram(self, report: DailyCompletionReport) -> str:
        person_open = self._dedupe_items(self._person_items(report.open_items))
        pm_open = self._dedupe_items(self._pm_items(report.open_items))
        other_open = self._dedupe_items(self._other_items(report.open_items))
        person_total = len(self._person_items(report.open_items + report.closed_items))
        person_completed = len([item for item in self._person_items(report.closed_items) if item.is_complete])
        pm_total = len(self._pm_items(report.open_items + report.closed_items))
        pm_completed = len([item for item in self._pm_items(report.closed_items) if item.is_complete])

        lines = [
            f"Итоги плана дня {report.report_date.strftime('%d.%m.%Y')}",
            f"Задача #{report.task_id}: {report.task_url}",
            f"Выполнено: {report.completed_items}/{report.total_items}",
        ]
        if person_total or pm_total:
            lines.append(f"По людям: {person_completed}/{person_total}; PM-контроль: {pm_completed}/{pm_total}")
        if not report.open_items:
            lines.append("Все пункты закрыты.")
            return "\n".join(lines).strip()

        if person_open:
            lines.extend(["", "Не закрыто по людям:"])
        for person_label, items in self._group_open_items(person_open).items():
            lines.append(person_label)
            for item in items[:5]:
                lines.append(f"- {item.title}")
            if len(items) > 5:
                lines.append(f"- ...и еще {len(items) - 5} пунктов")

        if pm_open:
            pm_mention = self._pm_mention()
            lines.extend(["", f"PM-контроль {pm_mention}".rstrip()])
            for group_title, items in self._group_by_title(pm_open).items():
                lines.append(f"{group_title}: {len(items)} открыто")
                for item in items[:3]:
                    lines.append(f"- {item.title}")
                if len(items) > 3:
                    lines.append(f"- ...и еще {len(items) - 3} пунктов")

        if other_open:
            lines.extend(["", "Без явного ответственного:"])
            for item in other_open[:5]:
                lines.append(f"- {item.group_title}: {item.title}")
            if len(other_open) > 5:
                lines.append(f"- ...и еще {len(other_open) - 5} пунктов")

        lines.extend(["", "Полный список и отметки чек-листов смотри в задаче."])
        return self._fit_telegram_text(lines)

    def format_weekly_comment(
        self,
        *,
        week_from: date,
        week_to: date,
        team_name: str,
        reports: list[DailyCompletionReport],
        missing_dates: list[date] | None = None,
    ) -> str:
        total = sum(report.total_items for report in reports)
        completed = sum(report.completed_items for report in reports)
        open_items = self._dedupe_report_items([(report, item) for report in reports for item in report.open_items])
        closed_items = self._dedupe_report_items([(report, item) for report in reports for item in report.closed_items])
        missing_dates = missing_dates or []
        lines = [
            f"Единый weekly PM-дайджест {week_from.strftime('%d.%m')} - {week_to.strftime('%d.%m.%Y')}",
            f"Команда: {team_name}",
            "",
            "Сводка выполнения",
            f"- Daily-задач найдено: {len(reports)}",
            f"- Всего пунктов чек-листов: {total}",
            f"- Закрыто: {completed}",
            f"- Не закрыто: {len(open_items)}",
        ]
        if missing_dates:
            lines.append(f"- Дней без найденной daily-задачи: {len(missing_dates)}")

        mentions = self._weekly_mentions([item for _, item in open_items])
        if mentions:
            lines.extend(["", f"Ответственные с незакрытыми пунктами: {' '.join(mentions)}"])

        task_links = [f"#{report.task_id}: {report.task_url}" for report in reports]
        if task_links:
            lines.extend(["", "Daily-задачи недели"])
            lines.extend(f"- {item}" for item in task_links)

        focus_lines = self._weekly_focus_lines(reports)
        if focus_lines:
            lines.extend(["", "Фокус недели по daily-планам"])
            lines.extend(f"- {item}" for item in focus_lines[:12])

        if closed_items:
            lines.extend(["", "Закрыто за неделю"])
            for report, item in closed_items:
                person = self._person_label(item)
                lines.append(f"- {report.report_date.strftime('%d.%m')} | {person}: {item.title}")

        if open_items:
            lines.extend(["", "Не закрыто по ответственным"])
            for person, grouped_items in self._group_report_items_by_person(open_items).items():
                lines.append(person)
                for report, item in grouped_items:
                    lines.append(f"- {report.report_date.strftime('%d.%m')}: {item.title}")
        else:
            lines.extend(["", "Не закрыто по ответственным", "- Все найденные пункты недели закрыты."])

        verification_count = sum(1 for _, item in open_items if self._is_verification_item(item))
        pm_count = sum(1 for _, item in open_items if self._is_pm_item(item))
        if verification_count or pm_count:
            lines.extend(["", "Контрольные акценты"])
            if pm_count:
                lines.append(f"- PM-контроль: {pm_count} открытых пунктов")
            if verification_count:
                lines.append(f"- Требует ручной проверки / несет риск: {verification_count} пунктов")

        if missing_dates:
            lines.extend(["", "Daily-задачи не найдены"])
            lines.extend(f"- {item.strftime('%d.%m.%Y')}" for item in missing_dates)
        return "\n".join(lines).strip()

    def format_weekly_telegram(
        self,
        *,
        week_from: date,
        week_to: date,
        team_name: str,
        reports: list[DailyCompletionReport],
        missing_dates: list[date] | None = None,
        weekly_task_id: int | None = None,
        weekly_task_url: str | None = None,
    ) -> str:
        total = sum(report.total_items for report in reports)
        completed = sum(report.completed_items for report in reports)
        open_items = self._dedupe_report_items([(report, item) for report in reports for item in report.open_items])
        closed_items = self._dedupe_report_items([(report, item) for report in reports for item in report.closed_items])
        missing_dates = missing_dates or []
        mentions = self._weekly_mentions([item for _, item in open_items])

        lines = [
            f"Weekly PM-итоги {week_from.strftime('%d.%m')} - {week_to.strftime('%d.%m.%Y')}",
            f"Команда: {team_name}",
        ]
        if weekly_task_id and weekly_task_url:
            lines.extend([f"Задача недели #{weekly_task_id}:", weekly_task_url])
        lines.extend(
            [
                "",
                f"Daily-задач: {len(reports)}; пунктов: {total}; закрыто: {completed}; открыто: {len(open_items)}",
            ]
        )
        if missing_dates:
            lines.append(f"Не найдены daily-задачи: {', '.join(item.strftime('%d.%m') for item in missing_dates)}")
        if mentions:
            lines.extend(["", f"Ответственные с открытыми пунктами: {' '.join(mentions)}"])

        open_by_person = self._group_report_items_by_person(open_items)
        if open_by_person:
            lines.extend(["", "Открыто по ответственным:"])
            for person, items in open_by_person.items():
                lines.append(f"- {person}: {len(items)}")

        if closed_items:
            closed_by_person = self._group_report_items_by_person(closed_items)
            closed_summary = ", ".join(f"{person}: {len(items)}" for person, items in closed_by_person.items())
            if closed_summary:
                lines.extend(["", f"Закрыто по ответственным: {closed_summary}"])

        task_numbers = ", ".join(f"#{report.task_id}" for report in reports)
        if task_numbers:
            lines.extend(["", f"Daily-подзадачи: {task_numbers}"])
        lines.extend(["", "Полный список закрытых и открытых пунктов смотри в задаче недели."])
        return self._fit_telegram_text(lines, limit=3600)

    def _person_for_row(self, row: dict[str, Any], group_title: str, category: str = "person") -> Person | None:
        if category.startswith("pm"):
            return None
        member_ids = self._member_ids(row)
        for member_id in member_ids:
            person = self.people.find_by_bitrix_user_id(member_id)
            if person:
                return person
        return self.people.find(group_title)

    @staticmethod
    def _category_for_group(group_title: str) -> str:
        normalized = group_title.casefold().strip()
        if "чеклист пм" in normalized or normalized == "pm":
            return "pm_checklist"
        if normalized.startswith("pm: требует") or "требует подтверждения" in normalized:
            return "pm_verification"
        if normalized.startswith("pm: не потерять") or "не потерять сегодня" in normalized:
            return "pm_dont_lose"
        if normalized.startswith("pm:") or "пм" in normalized:
            return "pm_other"
        return "person"

    @staticmethod
    def _fallback_label_for_group(group_title: str, category: str) -> str:
        if category.startswith("pm"):
            return "PM-контроль"
        return group_title

    @staticmethod
    def _parent_titles(rows: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in rows:
            parent_id = str(row.get("PARENT_ID") or row.get("parentId") or row.get("parent_id") or "0")
            if parent_id not in {"0", ""}:
                continue
            row_id = str(row.get("ID") or row.get("id") or "")
            title = str(row.get("TITLE") or row.get("title") or "").strip()
            if row_id and title:
                result[row_id] = title
        return result

    @staticmethod
    def _is_complete(row: dict[str, Any]) -> bool:
        value = str(row.get("IS_COMPLETE") or row.get("isComplete") or row.get("is_complete") or "").strip()
        return value.upper() == "Y" or value.lower() in {"true", "1"}

    @staticmethod
    def _member_ids(row: dict[str, Any]) -> list[int]:
        raw = row.get("members") or row.get("MEMBERS") or {}
        if isinstance(raw, dict):
            values = raw.keys()
        elif isinstance(raw, list):
            values = raw
        else:
            values = []
        result: list[int] = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        return result

    def _group_open_items(self, items: list[ChecklistCompletionItem]) -> dict[str, list[ChecklistCompletionItem]]:
        grouped: dict[str, list[ChecklistCompletionItem]] = {}
        for item in items:
            grouped.setdefault(self._person_label(item), []).append(item)
        return grouped

    @staticmethod
    def _group_by_title(items: list[ChecklistCompletionItem]) -> dict[str, list[ChecklistCompletionItem]]:
        grouped: dict[str, list[ChecklistCompletionItem]] = {}
        for item in items:
            grouped.setdefault(item.group_title or "PM-контроль", []).append(item)
        return grouped

    @staticmethod
    def _person_items(items: list[ChecklistCompletionItem]) -> list[ChecklistCompletionItem]:
        return [item for item in items if item.category == "person"]

    @staticmethod
    def _pm_items(items: list[ChecklistCompletionItem]) -> list[ChecklistCompletionItem]:
        return [item for item in items if item.category.startswith("pm")]

    @staticmethod
    def _other_items(items: list[ChecklistCompletionItem]) -> list[ChecklistCompletionItem]:
        return [item for item in items if item.category not in {"person"} and not item.category.startswith("pm")]

    @classmethod
    def _dedupe_items(cls, items: list[ChecklistCompletionItem]) -> list[ChecklistCompletionItem]:
        result: list[ChecklistCompletionItem] = []
        seen: set[str] = set()
        for item in items:
            key = cls._normalize_item_text(item.title)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _person_label(self, item: ChecklistCompletionItem) -> str:
        if item.category.startswith("pm"):
            pm_mention = self._pm_mention()
            return f"PM-контроль {pm_mention}".rstrip()
        if item.telegram_username:
            return f"{item.person_name} {item.telegram_username}"
        return item.person_name or item.group_title or "Без ответственного"

    def _weekly_mentions(self, items: list[ChecklistCompletionItem]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            mention = item.telegram_username.strip()
            if not mention and item.category.startswith("pm"):
                mention = self._pm_mention()
            if not mention or mention in seen:
                continue
            seen.add(mention)
            result.append(mention)
        return result

    def _group_report_items_by_person(
        self,
        items: list[tuple[DailyCompletionReport, ChecklistCompletionItem]],
    ) -> dict[str, list[tuple[DailyCompletionReport, ChecklistCompletionItem]]]:
        grouped: dict[str, list[tuple[DailyCompletionReport, ChecklistCompletionItem]]] = {}
        for report, item in items:
            grouped.setdefault(self._person_label(item), []).append((report, item))
        return grouped

    @classmethod
    def _dedupe_report_items(
        cls,
        items: list[tuple[DailyCompletionReport, ChecklistCompletionItem]],
    ) -> list[tuple[DailyCompletionReport, ChecklistCompletionItem]]:
        result: list[tuple[DailyCompletionReport, ChecklistCompletionItem]] = []
        seen: set[tuple[str, str, str]] = set()
        for report, item in items:
            key = (
                report.report_date.isoformat(),
                item.group_title.casefold().strip(),
                cls._normalize_item_text(item.title),
            )
            if not key[2] or key in seen:
                continue
            seen.add(key)
            result.append((report, item))
        return result

    def _pm_mention(self) -> str:
        person = self.people.find_by_bitrix_user_id(114736)
        if person and person.telegram_username:
            return person.telegram_username
        return ""

    @staticmethod
    def _is_pm_item(item: ChecklistCompletionItem) -> bool:
        return item.category.startswith("pm")

    @staticmethod
    def _is_verification_item(item: ChecklistCompletionItem) -> bool:
        text = f"{item.group_title} {item.title}".casefold()
        markers = (
            "требует",
            "провер",
            "риск",
            "блокер",
            "needs_verification",
            "blocked",
            "waiting_dependency",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _fit_telegram_text(cls, lines: list[str], limit: int = 3900) -> str:
        text = "\n".join(lines).strip()
        if len(text) <= limit:
            return text
        result: list[str] = []
        overflow = 0
        for line in lines:
            candidate = "\n".join(result + [line, "", "Сообщение сокращено, полный список в задаче."]).strip()
            if len(candidate) > limit:
                overflow += 1
                continue
            result.append(line)
        if overflow:
            result.extend(["", f"Сообщение сокращено: скрыто {overflow} строк, полный список в задаче."])
        return "\n".join(result).strip()

    @staticmethod
    def _normalize_item_text(text: str) -> str:
        normalized = re.sub(r"\[[^\]]+\]", "", text or "")
        normalized = re.sub(r"\([^)]*зависит[^)]*\)", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"[^\wА-Яа-яІіЇїЄєҐґ]+", " ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        return normalized

    def _weekly_focus_lines(self, reports: list[DailyCompletionReport]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for report in reports:
            focus_items = self._extract_markdown_section_items(report.task_description, "Фокус дня")
            if not focus_items:
                summary = self._extract_plain_section_text(report.task_description, "Краткое резюме")
                focus_items = [summary] if summary else []
            for item in focus_items:
                text = f"{report.report_date.strftime('%d.%m')}: {item}"
                normalized = text.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                result.append(text)
        return result

    @staticmethod
    def _extract_markdown_section_items(text: str, section_name: str) -> list[str]:
        if not text:
            return []
        pattern = re.compile(
            rf"(?ims)^##\s*\d*\.?\s*{re.escape(section_name)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)"
        )
        match = pattern.search(text)
        if not match:
            return []
        result: list[str] = []
        for raw_line in match.group("body").splitlines():
            line = raw_line.strip()
            if not line.startswith(("-", "*")):
                continue
            cleaned = line.lstrip("-* ").strip()
            if cleaned:
                result.append(cleaned)
        return result

    @staticmethod
    def _extract_plain_section_text(text: str, section_name: str) -> str:
        if not text:
            return ""
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized_text.splitlines()
        known_sections = {
            "дата",
            "команда",
            "источник #daily встреч",
            "краткое резюме",
            "план по людям",
            "общие блокеры / зависимости",
            "сделано / подтверждено на daily",
            "требует ручной проверки ответственного",
            "фокус дня",
            "чеклист пма",
            "зависимости / последовательности",
            "требует подтверждения / ручной проверки",
            "не закрыто / в работе",
            "блокеры / риски",
            "не потерять сегодня",
        }
        start: int | None = None
        for index, raw_line in enumerate(lines):
            if raw_line.strip().casefold() == section_name.casefold():
                start = index + 1
                break
        if start is None:
            return ""
        body: list[str] = []
        for raw_line in lines[start:]:
            line = raw_line.strip()
            if line.casefold() in known_sections:
                break
            if line:
                body.append(line)
        return " ".join(body).strip()
