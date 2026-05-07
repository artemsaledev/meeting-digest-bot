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
            person = self._person_for_row(row, group_title)
            item = ChecklistCompletionItem(
                title=title,
                group_title=group_title,
                is_complete=self._is_complete(row),
                bitrix_user_id=person.bitrix_user_id if person else None,
                person_name=person.full_name if person else group_title,
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

        lines.extend(["", "Не закрыто по ответственным:"])
        for person_label, items in self._group_open_items(report.open_items).items():
            lines.append(person_label)
            lines.extend(f"- {item.title}" for item in items)
        return "\n".join(lines).strip()

    def format_daily_telegram(self, report: DailyCompletionReport) -> str:
        lines = [
            f"Итоги плана дня {report.report_date.strftime('%d.%m.%Y')}",
            f"Задача #{report.task_id}: {report.task_url}",
            f"Выполнено: {report.completed_items}/{report.total_items}",
        ]
        if not report.open_items:
            lines.append("Все пункты закрыты.")
            return "\n".join(lines).strip()

        lines.extend(["", "Не закрыто:"])
        for person_label, items in self._group_open_items(report.open_items).items():
            lines.append(person_label)
            lines.extend(f"- {item.title}" for item in items)
        return "\n".join(lines).strip()

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
        open_items = [(report, item) for report in reports for item in report.open_items]
        closed_items = [(report, item) for report in reports for item in report.closed_items]
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

        focus_lines = self._weekly_focus_lines(reports)
        if focus_lines:
            lines.extend(["", "Фокус недели по daily-планам"])
            lines.extend(f"- {item}" for item in focus_lines[:20])

        if closed_items:
            lines.extend(["", "Закрыто за неделю"])
            for report, item in closed_items[:60]:
                person = self._person_label(item)
                lines.append(f"- {report.report_date.strftime('%d.%m')} | {person}: {item.title}")
            if len(closed_items) > 60:
                lines.append(f"- ...и еще {len(closed_items) - 60} закрытых пунктов")

        if open_items:
            lines.extend(["", "Не закрыто по дням"])
            for report, item in open_items:
                person = self._person_label(item)
                lines.append(f"- {report.report_date.strftime('%d.%m')} | {person}: {item.title}")
        else:
            lines.extend(["", "Не закрыто по дням", "- Все найденные пункты недели закрыты."])

        pm_open = [(report, item) for report, item in open_items if self._is_pm_item(item)]
        if pm_open:
            lines.extend(["", "PM follow-up / контроль ПМа"])
            for report, item in pm_open:
                lines.append(f"- {report.report_date.strftime('%d.%m')} | {item.group_title}: {item.title}")

        verification_open = [(report, item) for report, item in open_items if self._is_verification_item(item)]
        if verification_open:
            lines.extend(["", "Требует проверки / риски"])
            for report, item in verification_open:
                lines.append(f"- {report.report_date.strftime('%d.%m')} | {item.title}")

        if missing_dates:
            lines.extend(["", "Daily-задачи не найдены"])
            lines.extend(f"- {item.strftime('%d.%m.%Y')}" for item in missing_dates)

        task_links = [f"#{report.task_id}: {report.task_url}" for report in reports]
        if task_links:
            lines.extend(["", "Источники daily-задач"])
            lines.extend(f"- {item}" for item in task_links)
        return "\n".join(lines).strip()

    def format_weekly_telegram(
        self,
        *,
        week_from: date,
        week_to: date,
        team_name: str,
        reports: list[DailyCompletionReport],
        missing_dates: list[date] | None = None,
    ) -> str:
        return self.format_weekly_comment(
            week_from=week_from,
            week_to=week_to,
            team_name=team_name,
            reports=reports,
            missing_dates=missing_dates,
        )

    def _person_for_row(self, row: dict[str, Any], group_title: str) -> Person | None:
        member_ids = self._member_ids(row)
        for member_id in member_ids:
            person = self.people.find_by_bitrix_user_id(member_id)
            if person:
                return person
        return self.people.find(group_title)

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
    def _person_label(item: ChecklistCompletionItem) -> str:
        if item.telegram_username:
            return f"{item.person_name} {item.telegram_username}"
        return item.person_name or item.group_title or "Без ответственного"

    def _weekly_mentions(self, items: list[ChecklistCompletionItem]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            mention = item.telegram_username.strip()
            if not mention or mention in seen:
                continue
            seen.add(mention)
            result.append(mention)
        return result

    @staticmethod
    def _is_pm_item(item: ChecklistCompletionItem) -> bool:
        title = item.group_title.casefold()
        return title.startswith("pm:") or "чеклист пм" in title or "пма" in title

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
