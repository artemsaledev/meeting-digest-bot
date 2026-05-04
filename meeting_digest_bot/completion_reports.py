from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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
    total_items: int = 0
    completed_items: int = 0
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
        checklist_rows: list[dict[str, Any]],
    ) -> DailyCompletionReport:
        parents = self._parent_titles(checklist_rows)
        report = DailyCompletionReport(
            report_date=report_date,
            team_name=team_name,
            task_id=task_id,
            task_url=task_url,
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
    ) -> str:
        total = sum(report.total_items for report in reports)
        completed = sum(report.completed_items for report in reports)
        open_items = [(report, item) for report in reports for item in report.open_items]
        lines = [
            f"Еженедельный отчёт по выполнению daily-планов {week_from.strftime('%d.%m')} - {week_to.strftime('%d.%m.%Y')}",
            f"Команда: {team_name}",
            "",
            f"Всего пунктов: {total}",
            f"Выполнено: {completed}",
            f"Не закрыто: {len(open_items)}",
        ]
        if not open_items:
            lines.extend(["", "Все найденные пункты недели закрыты."])
            return "\n".join(lines).strip()

        mentions = self._weekly_mentions([item for _, item in open_items])
        if mentions:
            lines.extend(["", f"Ответственные с незакрытыми пунктами: {' '.join(mentions)}"])
        lines.extend(["", "Незакрытые пункты по дням:"])
        for report, item in open_items:
            person = self._person_label(item)
            lines.append(f"{report.report_date.strftime('%d.%m')} - {person}: {item.title}")
        return "\n".join(lines).strip()

    def format_weekly_telegram(
        self,
        *,
        week_from: date,
        week_to: date,
        team_name: str,
        reports: list[DailyCompletionReport],
    ) -> str:
        return self.format_weekly_comment(
            week_from=week_from,
            week_to=week_to,
            team_name=team_name,
            reports=reports,
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
