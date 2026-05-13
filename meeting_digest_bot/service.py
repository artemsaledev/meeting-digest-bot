from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any

import requests

from .aicallorder_db import AIcallorderRepository
from .bitrix_client import BitrixClient
from .completion_reports import CompletionReportBuilder, DailyCompletionReport
from .config import Settings
from .daily_pm_llm import DailyPMChecklistLLM, DailyPMLLMConfig
from .daily_plan import DailyPlanV2Parser
from .models import (
    DailyPersonPlan,
    DailyPlanItem,
    DailyReportRequest,
    DailyPlanSyncRequest,
    DailyRollup,
    DaySyncRequest,
    DigestType,
    PostSyncRequest,
    PublicationRegistrationRequest,
    SyncAction,
    SyncResult,
    TaskDraft,
    WeeklyReportRequest,
    WeekSyncRequest,
    WeeklyRollup,
)
from .state_db import StateRepository
from .task_drafts import build_daily_plan_task_draft, build_daily_task_draft, build_meeting_task_draft
from .task_matching import find_task_matches
from .weekly_llm import WeeklyLLMConfig, WeeklyRollupLLM


class MeetingDigestService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.aicallorder = AIcallorderRepository(settings.aicallorder_db_path)
        self.state = StateRepository(settings.state_db_path)
        self.bitrix = BitrixClient(
            legacy_base_url=settings.bitrix_webhook_base,
            modern_base_url=settings.bitrix_modern_webhook_base,
            use_json_suffix=settings.bitrix_webhook_json_suffix,
        )
        self.weekly_llm = WeeklyRollupLLM(
            WeeklyLLMConfig(
                enabled=settings.weekly_llm_enabled,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
        self.daily_plan_parser = DailyPlanV2Parser()
        self.daily_pm_llm = DailyPMChecklistLLM(
            DailyPMLLMConfig(
                enabled=settings.daily_pm_llm_enabled,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ),
            people=self.daily_plan_parser.people,
        )
        self.completion_reports = CompletionReportBuilder()

    def register_publication(self, payload: PublicationRegistrationRequest):
        return self.state.register_publication(payload)

    def sync_post(self, payload: PostSyncRequest) -> SyncResult:
        publication = self.state.get_publication_by_post_url(payload.post_url)
        if publication is None:
            raise ValueError(
                "Публикация не зарегистрирована. Сначала AIcallorder должен передать post_url и metadata в register endpoint."
            )
        if publication.digest_type == DigestType.weekly.value:
            if not publication.week_from or not publication.week_to:
                raise ValueError("Для weekly publication отсутствуют week_from/week_to.")
            payload_meta = publication.payload if isinstance(publication.payload, dict) else {}
            return self.run_weekly_report(
                WeeklyReportRequest(
                    week_from=date.fromisoformat(publication.week_from),
                    week_to=date.fromisoformat(publication.week_to),
                    team_name=str(payload_meta.get("team_name") or "Bitrix Develop Team"),
                    force=True,
                    send_telegram=False,
                )
            )
        if publication.digest_type == DigestType.daily.value:
            if not publication.report_date:
                raise ValueError("Для daily publication отсутствует report_date.")
            return self.sync_day(
                DaySyncRequest(
                    report_date=date.fromisoformat(publication.report_date),
                    action=payload.action,
                    task_id=payload.task_id,
                )
            )
        if not publication.loom_video_id:
            raise ValueError("Для meeting publication отсутствует loom_video_id.")
        meeting = self.aicallorder.get_meeting(publication.loom_video_id)
        if meeting is None:
            raise ValueError(f"В AIcallorder не найдена встреча {publication.loom_video_id}.")

        draft = build_meeting_task_draft(
            meeting=meeting,
            publication=publication,
            default_tags=self.settings.bitrix_tags,
        )
        source_type = "meeting"
        source_key = meeting.loom_video_id
        result = self._apply_task_draft(
            draft=draft,
            source_type=source_type,
            source_key=source_key,
            action=payload.action,
            explicit_task_id=payload.task_id,
        )
        result.details["post_url"] = publication.post_url
        result.details["telegram_message_id"] = publication.telegram_message_id
        result.details["telegram_chat_id"] = publication.telegram_chat_id
        return result

    def sync_week(self, payload: WeekSyncRequest) -> SyncResult:
        if payload.action == SyncAction.preview:
            source_type = "daily_plan_weekly_report"
            source_key = f"{payload.week_from.isoformat()}:{payload.week_to.isoformat()}:Bitrix Develop Team"
            reports = self._build_daily_completion_reports_between(
                payload.week_from,
                payload.week_to,
                "Bitrix Develop Team",
            )
            missing_dates = self._missing_daily_completion_dates(
                payload.week_from,
                payload.week_to,
                reports,
            )
            comment = self.completion_reports.format_weekly_comment(
                week_from=payload.week_from,
                week_to=payload.week_to,
                team_name="Bitrix Develop Team",
                reports=reports,
                missing_dates=missing_dates,
            )
            weekly_task_id = self._weekly_report_task_id(payload.week_from, payload.week_to, "Bitrix Develop Team")
            telegram_text = self.completion_reports.format_weekly_telegram(
                week_from=payload.week_from,
                week_to=payload.week_to,
                team_name="Bitrix Develop Team",
                reports=reports,
                missing_dates=missing_dates,
                weekly_task_id=weekly_task_id,
                weekly_task_url=self._task_url(weekly_task_id) if weekly_task_id else None,
            )
            return SyncResult(
                action="weekly_report_preview",
                title=self._weekly_report_title(payload.week_from, payload.week_to, "Bitrix Develop Team"),
                source_type=source_type,
                source_key=source_key,
                details=self._weekly_report_details(reports)
                | {
                    "missing_dates": [item.isoformat() for item in missing_dates],
                    "preview_text": comment,
                    "telegram_text": telegram_text,
                },
            )
        return self.run_weekly_report(
            WeeklyReportRequest(
                week_from=payload.week_from,
                week_to=payload.week_to,
                force=True,
                send_telegram=False,
            )
        )

    def sync_day(self, payload: DaySyncRequest) -> SyncResult:
        meetings = self.aicallorder.list_meetings_between(payload.report_date, payload.report_date)
        rollup = self._build_daily_rollup(payload.report_date, meetings)
        draft = build_daily_task_draft(rollup=rollup, default_tags=self.settings.bitrix_tags)
        source_type = "daily_digest"
        source_key = payload.report_date.isoformat()
        return self._apply_task_draft(
            draft=draft,
            source_type=source_type,
            source_key=source_key,
            action=payload.action,
            explicit_task_id=payload.task_id,
        )

    def sync_daily_plan(self, payload: DailyPlanSyncRequest) -> SyncResult:
        meetings = [
            meeting
            for meeting in self.aicallorder.list_meetings_between(payload.report_date, payload.report_date)
            if self._is_daily_plan_meeting(meeting)
        ]
        if not meetings:
            raise ValueError(f"За {payload.report_date.isoformat()} не найдены встречи с #daily.")
        plan = self.daily_plan_parser.parse_meetings(
            report_date=payload.report_date,
            meetings=meetings,
            team_name=payload.team_name,
        )
        self._enhance_daily_plan_pm(plan=plan, meetings=meetings)
        draft = build_daily_plan_task_draft(plan=plan, default_tags=self.settings.bitrix_tags)
        source_type = "daily_plan"
        source_key = f"{payload.report_date.isoformat()}:{payload.team_name}"
        return self._apply_task_draft(
            draft=draft,
            source_type=source_type,
            source_key=source_key,
            action=payload.action,
            explicit_task_id=payload.task_id,
        )

    def _enhance_daily_plan_pm(self, *, plan, meetings) -> None:
        if not self.daily_pm_llm.config.usable:
            plan.pm_generation_notes.append("PM daily checklist LLM disabled or not configured; fallback daily_plan_v2 used.")
            return
        try:
            result = self.daily_pm_llm.enhance(
                report_date=plan.report_date,
                team_name=plan.team_name,
                base_plan=plan,
                meetings=meetings,
            )
        except Exception as exc:
            plan.pm_generation_notes.append(f"PM daily checklist LLM failed: {type(exc).__name__}: {exc}")
            return
        if not result:
            plan.pm_generation_notes.append("PM daily checklist LLM returned empty result; fallback daily_plan_v2 used.")
            return
        plan.pm_markdown = result.markdown
        plan.pm_checklist = result.pm_checklist
        plan.pm_needs_verification = result.needs_verification
        plan.pm_dont_lose_today = result.dont_lose_today
        plan.pm_generation_notes.extend(result.notes)
        self._replace_daily_people_plan_from_pm_analysis(plan=plan, people_plan=result.people_plan)

    def _replace_daily_people_plan_from_pm_analysis(self, *, plan, people_plan) -> None:
        if not people_plan:
            return

        grouped: dict[int | str, DailyPersonPlan] = {}
        for entry in people_plan:
            if not isinstance(entry, dict):
                continue
            task = str(entry.get("task") or "").strip()
            if not task:
                continue
            status = str(entry.get("status") or "todo").strip() or "todo"
            if status == "done":
                continue

            raw_person_name = str(entry.get("person") or "").strip()
            person = self.daily_plan_parser.people.find(raw_person_name)
            person_name = person.full_name if person else raw_person_name or "Не удалось назначить ответственного"
            bitrix_user_id = person.bitrix_user_id if person else None
            key: int | str = bitrix_user_id or person_name
            if key not in grouped:
                grouped[key] = DailyPersonPlan(
                    person_name=person_name,
                    bitrix_user_id=bitrix_user_id,
                )

            dependency = str(entry.get("dependency") or "").strip()
            title = task
            if dependency:
                title = f"{title} (зависит от: {dependency})"
            if status and status != "todo":
                title = f"{title} [{status}]"

            item = DailyPlanItem(
                title=title,
                person_name=person_name,
                bitrix_user_id=bitrix_user_id,
                source_meeting_id=",".join(plan.source_meeting_ids) if plan.source_meeting_ids else None,
                source_meeting_title="PM daily checklist",
                item_type="blocked" if status == "blocked" else "plan",
            )
            if status == "blocked":
                grouped[key].blockers.append(item)
            else:
                grouped[key].plan_items.append(item)

        if grouped:
            plan.people = list(grouped.values())
            plan.unmatched_items = []
            plan.review_notes.append("План по людям заменен нормализованным PM LLM-разбором.")

    def run_daily_report(self, payload: DailyReportRequest) -> SyncResult:
        source_type = "daily_plan_report"
        source_key = f"{payload.report_date.isoformat()}:{payload.team_name}"
        existing = self.state.get_task_binding(source_type=source_type, source_key=source_key)
        report = self._build_daily_completion_report(payload.report_date, payload.team_name)
        comment = self.completion_reports.format_daily_comment(report)
        telegram_text = self.completion_reports.format_daily_telegram(report)
        telegram_result = None

        if existing and not payload.force:
            existing_meta = existing.get("meta") or {}
            existing_telegram = existing_meta.get("telegram") or {}
            telegram_already_sent = bool(existing_telegram.get("sent")) if isinstance(existing_telegram, dict) else False
            if payload.send_telegram and not telegram_already_sent:
                telegram_result = self._send_telegram_report(telegram_text)
                self.state.upsert_task_binding(
                    source_type=source_type,
                    source_key=source_key,
                    bitrix_task_id=report.task_id,
                    mode="reported",
                    title=f"Итоги плана дня {payload.report_date.strftime('%d.%m.%Y')}",
                    meta={
                        "report_date": payload.report_date.isoformat(),
                        "team_name": payload.team_name,
                        "total": report.total_items,
                        "completed": report.completed_items,
                        "open": report.open_count,
                        "telegram": telegram_result,
                        "telegram_text": telegram_text,
                        "crm_comment": "already_reported",
                    },
                )
                return SyncResult(
                    action="daily_report_telegram_sent",
                    task_id=report.task_id,
                    task_url=report.task_url,
                    title=f"Итоги плана дня {payload.report_date.strftime('%d.%m.%Y')}",
                    source_type=source_type,
                    source_key=source_key,
                    details={
                        "reason": "already_reported_without_telegram",
                        "total": report.total_items,
                        "completed": report.completed_items,
                        "open": report.open_count,
                        "telegram": telegram_result,
                        "telegram_text": telegram_text,
                    },
                )
            return SyncResult(
                action="daily_report_skipped",
                task_id=report.task_id,
                task_url=report.task_url,
                title=f"Итоги плана дня {payload.report_date.strftime('%d.%m.%Y')}",
                source_type=source_type,
                source_key=source_key,
                details={
                    "reason": "already_reported",
                    "total": report.total_items,
                    "completed": report.completed_items,
                    "open": report.open_count,
                    "telegram_text": telegram_text,
                },
            )

        self._send_task_comment(report.task_id, comment)
        if payload.send_telegram:
            telegram_result = self._send_telegram_report(telegram_text)
        self.state.upsert_task_binding(
            source_type=source_type,
            source_key=source_key,
            bitrix_task_id=report.task_id,
            mode="reported",
            title=f"Итоги плана дня {payload.report_date.strftime('%d.%m.%Y')}",
            meta={
                "report_date": payload.report_date.isoformat(),
                "team_name": payload.team_name,
                "total": report.total_items,
                "completed": report.completed_items,
                "open": report.open_count,
                "telegram": telegram_result,
                "telegram_text": telegram_text,
            },
        )
        return SyncResult(
            action="daily_reported",
            task_id=report.task_id,
            task_url=report.task_url,
            title=f"Итоги плана дня {payload.report_date.strftime('%d.%m.%Y')}",
            source_type=source_type,
            source_key=source_key,
            details={
                "total": report.total_items,
                "completed": report.completed_items,
                "open": report.open_count,
                "telegram": telegram_result,
                "telegram_text": telegram_text,
            },
        )

    def run_weekly_report(self, payload: WeeklyReportRequest) -> SyncResult:
        source_type = "daily_plan_weekly_report"
        source_key = f"{payload.week_from.isoformat()}:{payload.week_to.isoformat()}:{payload.team_name}"
        reports = self._build_daily_completion_reports_between(payload.week_from, payload.week_to, payload.team_name)
        missing_dates = self._missing_daily_completion_dates(
            payload.week_from,
            payload.week_to,
            reports,
        )
        comment = self.completion_reports.format_weekly_comment(
            week_from=payload.week_from,
            week_to=payload.week_to,
            team_name=payload.team_name,
            reports=reports,
            missing_dates=missing_dates,
        )
        weekly_task_id = self._ensure_weekly_report_task(
            week_from=payload.week_from,
            week_to=payload.week_to,
            team_name=payload.team_name,
            description=comment,
        )
        subtask_details = self._attach_daily_tasks_to_weekly(weekly_task_id, reports)
        telegram_text = self.completion_reports.format_weekly_telegram(
            week_from=payload.week_from,
            week_to=payload.week_to,
            team_name=payload.team_name,
            reports=reports,
            missing_dates=missing_dates,
            weekly_task_id=weekly_task_id,
            weekly_task_url=self._task_url(weekly_task_id),
        )
        telegram_result = None
        if payload.send_telegram:
            telegram_result = self._send_telegram_report(telegram_text)
        self.state.upsert_task_binding(
            source_type=source_type,
            source_key=source_key,
            bitrix_task_id=weekly_task_id,
            mode="reported",
            title=self._weekly_report_title(payload.week_from, payload.week_to, payload.team_name),
            meta=self._weekly_report_details(reports) | {
                "week_from": payload.week_from.isoformat(),
                "week_to": payload.week_to.isoformat(),
                "team_name": payload.team_name,
                "missing_dates": [item.isoformat() for item in missing_dates],
                "telegram": telegram_result,
                "telegram_text": telegram_text,
                "full_report_text": comment,
                "subtasks": subtask_details,
            },
        )
        return SyncResult(
            action="weekly_reported",
            task_id=weekly_task_id,
            task_url=self._task_url(weekly_task_id),
            title=self._weekly_report_title(payload.week_from, payload.week_to, payload.team_name),
            source_type=source_type,
            source_key=source_key,
            details=self._weekly_report_details(reports)
            | {
                "missing_dates": [item.isoformat() for item in missing_dates],
                "telegram": telegram_result,
                "telegram_text": telegram_text,
                "subtasks": subtask_details,
            },
        )

    def _build_daily_completion_report(self, report_date: date, team_name: str) -> DailyCompletionReport:
        task_id = self._daily_plan_task_id(report_date, team_name)
        if not task_id:
            raise ValueError(f"Daily plan task is not found for {report_date.isoformat()} / {team_name}.")
        task = self._get_task_payload(task_id, select=["ID", "TITLE", "DESCRIPTION"])
        if not task:
            raise ValueError(f"Daily plan task #{task_id} is not found or unavailable in CRM.")
        checklist_rows = self.bitrix.list_checklist_items(task_id)
        return self.completion_reports.build_daily(
            report_date=report_date,
            team_name=team_name,
            task_id=task_id,
            task_url=self._task_url(task_id),
            task_title=str(task.get("title") or task.get("TITLE") or ""),
            task_description=str(task.get("description") or task.get("DESCRIPTION") or ""),
            checklist_rows=checklist_rows,
        )

    def _build_daily_completion_reports_between(
        self,
        week_from: date,
        week_to: date,
        team_name: str,
    ) -> list[DailyCompletionReport]:
        reports: list[DailyCompletionReport] = []
        current = week_from
        while current <= week_to:
            try:
                reports.append(self._build_daily_completion_report(current, team_name))
            except ValueError:
                pass
            current += timedelta(days=1)
        return reports

    @staticmethod
    def _missing_daily_completion_dates(
        week_from: date,
        week_to: date,
        reports: list[DailyCompletionReport],
    ) -> list[date]:
        found = {report.report_date for report in reports}
        missing: list[date] = []
        current = week_from
        while current <= week_to:
            if current not in found:
                missing.append(current)
            current += timedelta(days=1)
        return missing

    def _daily_plan_task_id(self, report_date: date, team_name: str) -> int | None:
        binding = self.state.get_task_binding(
            source_type="daily_plan",
            source_key=f"{report_date.isoformat()}:{team_name}",
        )
        if binding:
            try:
                task_id = int(binding["bitrix_task_id"])
            except (TypeError, ValueError):
                task_id = 0
            if task_id and self._task_exists(task_id):
                return task_id

        task_id = self._find_daily_plan_task_id_in_crm(report_date, team_name)
        if task_id:
            self.state.upsert_task_binding(
                source_type="daily_plan",
                source_key=f"{report_date.isoformat()}:{team_name}",
                bitrix_task_id=task_id,
                mode="found_in_crm",
                title=f"План дня {report_date.strftime('%d.%m.%Y')} / {team_name}",
                meta={"report_date": report_date.isoformat(), "team_name": team_name, "fallback": "crm_title_search"},
            )
        return task_id

    def _find_daily_plan_task_id_in_crm(self, report_date: date, team_name: str) -> int | None:
        try:
            data = self.bitrix.list_tasks(
                filter_data={"GROUP_ID": self.settings.bitrix_group_id},
                order={"ID": "desc"},
                select=["ID", "TITLE", "GROUP_ID"],
            )
        except Exception:
            return None
        result = data.get("result") or {}
        tasks = result.get("tasks") if isinstance(result, dict) else result
        if not isinstance(tasks, list):
            return None
        date_token = report_date.strftime("%d.%m.%Y")
        short_date_token = report_date.strftime("%d.%m")
        team_token = team_name.casefold().strip()
        candidates: list[tuple[int, int]] = []
        for task in tasks:
            raw_title = str(task.get("title") or task.get("TITLE") or "").strip()
            title = raw_title.casefold()
            if "итоги недели" in title or "weekly" in title:
                continue
            if "план дня" not in title:
                continue
            if team_token and team_token not in title:
                continue
            score = 0
            if date_token in raw_title:
                score += 10
            if short_date_token in raw_title:
                score += 5
            if "#daily" in title:
                score += 1
            if not score:
                continue
            task_id = task.get("id") or task.get("ID")
            try:
                candidates.append((score, int(task_id)))
            except (TypeError, ValueError):
                continue
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _weekly_task_id(self, week_from: date, week_to: date) -> int | None:
        binding = self.state.get_task_binding(
            source_type="weekly_digest",
            source_key=f"{week_from.isoformat()}:{week_to.isoformat()}",
        )
        if not binding:
            return None
        try:
            task_id = int(binding["bitrix_task_id"])
        except (TypeError, ValueError):
            return None
        return task_id if task_id and self._task_exists(task_id) else None

    def _weekly_report_task_id(self, week_from: date, week_to: date, team_name: str) -> int | None:
        binding = self.state.get_task_binding(
            source_type="daily_plan_weekly_report",
            source_key=f"{week_from.isoformat()}:{week_to.isoformat()}:{team_name}",
        )
        if not binding:
            return None
        try:
            task_id = int(binding["bitrix_task_id"])
        except (TypeError, ValueError):
            return None
        return task_id if task_id and self._task_exists(task_id) else None

    def _ensure_weekly_report_task(
        self,
        *,
        week_from: date,
        week_to: date,
        team_name: str,
        description: str,
    ) -> int:
        task_id = (
            self._weekly_report_task_id(week_from, week_to, team_name)
            or self._weekly_task_id(week_from, week_to)
            or self._find_weekly_report_task_id_in_crm(week_from, week_to, team_name)
        )
        draft = TaskDraft(
            title=self._weekly_report_title(week_from, week_to, team_name),
            description=description,
            tags=[*self.settings.bitrix_tags, "weekly-report"],
            meta={
                "weekly_report": True,
                "week_from": week_from.isoformat(),
                "week_to": week_to.isoformat(),
                "team_name": team_name,
            },
        )
        if task_id:
            self.bitrix.update_task(task_id, self._task_update_fields(draft))
            return task_id
        return self._create_task(draft)

    def _find_weekly_report_task_id_in_crm(self, week_from: date, week_to: date, team_name: str) -> int | None:
        expected_title = self._weekly_report_title(week_from, week_to, team_name).casefold().strip()
        try:
            data = self.bitrix.list_tasks(
                filter_data={"GROUP_ID": self.settings.bitrix_group_id},
                order={"ID": "desc"},
                select=["ID", "TITLE", "GROUP_ID"],
            )
        except Exception:
            return None
        result = data.get("result") or {}
        tasks = result.get("tasks") if isinstance(result, dict) else result
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            raw_title = str(task.get("title") or task.get("TITLE") or "").strip()
            if raw_title.casefold() != expected_title:
                continue
            task_id = task.get("id") or task.get("ID")
            try:
                return int(task_id)
            except (TypeError, ValueError):
                return None
        return None

    def _attach_daily_tasks_to_weekly(
        self,
        weekly_task_id: int,
        reports: list[DailyCompletionReport],
    ) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for report in reports:
            item: dict[str, Any] = {
                "report_date": report.report_date.isoformat(),
                "task_id": report.task_id,
                "task_url": report.task_url,
            }
            try:
                self.bitrix.set_task_parent(report.task_id, weekly_task_id)
                item["attached"] = True
            except Exception as exc:
                item["attached"] = False
                item["error"] = str(exc)
            details.append(item)
        return details

    @staticmethod
    def _weekly_report_title(week_from: date, week_to: date, team_name: str) -> str:
        return f"Итоги недели {week_from.strftime('%d.%m')} - {week_to.strftime('%d.%m.%Y')} / {team_name}"

    @staticmethod
    def _weekly_report_details(reports: list[DailyCompletionReport]) -> dict[str, Any]:
        total = sum(report.total_items for report in reports)
        completed = sum(report.completed_items for report in reports)
        open_count = sum(report.open_count for report in reports)
        return {
            "days_found": len(reports),
            "total": total,
            "completed": completed,
            "open": open_count,
            "daily_tasks": [report.task_id for report in reports],
        }

    def _send_telegram_report(self, text: str) -> dict[str, Any]:
        if not self.settings.telegram_bot_token:
            return {"sent": False, "reason": "telegram_bot_token_missing"}
        chat_id = self.settings.telegram_report_chat_id or self.state.get_latest_telegram_chat_id()
        if not chat_id:
            return {"sent": False, "reason": "telegram_report_chat_id_missing"}
        response = requests.post(
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        return {"sent": True, "chat_id": str(chat_id)}

    def _apply_task_draft(
        self,
        *,
        draft: TaskDraft,
        source_type: str,
        source_key: str,
        action: SyncAction,
        explicit_task_id: int | None,
    ) -> SyncResult:
        binding = self.state.get_task_binding(source_type=source_type, source_key=source_key)
        target_task_id = None if action == SyncAction.create else explicit_task_id or (binding["bitrix_task_id"] if binding else None)
        stale_binding_task_id: int | None = None

        if target_task_id and not self._task_exists(int(target_task_id)):
            if explicit_task_id:
                raise ValueError(
                    f"Задача #{target_task_id} не найдена или недоступна в CRM. "
                    "Если она была удалена, выполните команду без номера задачи с действием `создать`."
                )
            stale_binding_task_id = int(target_task_id)
            self.state.delete_task_binding(source_type=source_type, source_key=source_key)
            binding = None
            target_task_id = None

        effective_action = self._resolve_action(action=action, has_existing=bool(target_task_id))

        if effective_action == SyncAction.preview:
            return self._preview_task_draft(
                draft=draft,
                source_type=source_type,
                source_key=source_key,
                target_task_id=target_task_id,
                stale_binding_task_id=stale_binding_task_id,
            )

        existing_required_actions = {
            SyncAction.update_description,
            SyncAction.append_comment,
            SyncAction.append_checklists,
            SyncAction.append_to_weekly,
        }
        if target_task_id is None and effective_action in existing_required_actions:
            stale_suffix = (
                f" Ранее была привязка к задаче #{stale_binding_task_id}, но она не найдена в CRM, поэтому привязка сброшена."
                if stale_binding_task_id
                else ""
            )
            raise ValueError(
                "Для этой команды нужна существующая задача. Укажите номер задачи или сначала выполните команду создать."
                + stale_suffix
            )

        if effective_action == SyncAction.create:
            task_id = self._create_task(draft)
            self.state.upsert_task_binding(
                source_type=source_type,
                source_key=source_key,
                bitrix_task_id=task_id,
                mode="created",
                title=draft.title,
                meta=draft.meta,
            )
            return SyncResult(
                action="created",
                task_id=task_id,
                task_url=self._task_url(task_id),
                title=draft.title,
                source_type=source_type,
                source_key=source_key,
                details={"checklists": len(draft.checklist_groups)},
            )

        if effective_action == SyncAction.update_description:
            existing_description = self._get_task_description(target_task_id)
            merged_description, point_number, point_replaced = self._merge_task_description(
                existing_description=existing_description,
                draft=draft,
                source_type=source_type,
                source_key=source_key,
            )
            self.bitrix.update_task(target_task_id, self._task_merge_update_fields(draft, merged_description))
            checklist_details = None
            checklist_items = self._point_checklist_items(draft)
            if checklist_items:
                checklist_details = self.bitrix.add_checklist_group_deduped(
                    target_task_id,
                    self._point_checklist_title(point_number, draft),
                    checklist_items,
                )
            if draft.comment:
                self._send_task_comment(target_task_id, draft.comment)
            self.state.upsert_task_binding(
                source_type=source_type,
                source_key=source_key,
                bitrix_task_id=target_task_id,
                mode="updated",
                title=draft.title,
                meta=draft.meta,
            )
            return SyncResult(
                action="merged_update",
                task_id=target_task_id,
                task_url=self._task_url(target_task_id),
                title=draft.title,
                source_type=source_type,
                source_key=source_key,
                details={
                    "updated_fields": list(self._task_merge_update_fields(draft, merged_description).keys()),
                    "point_number": point_number,
                    "point_replaced": point_replaced,
                    "checklist": checklist_details,
                },
            )

        if effective_action == SyncAction.append_checklists:
            checklist_details = []
            for group in draft.checklist_groups:
                checklist_details.append(
                    self.bitrix.add_checklist_group_deduped(target_task_id, group.title, group.items)
                )
            self.state.upsert_task_binding(
                source_type=source_type,
                source_key=source_key,
                bitrix_task_id=target_task_id,
                mode="checklisted",
                title=draft.title,
                meta=draft.meta,
            )
            return SyncResult(
                action="checklisted",
                task_id=target_task_id,
                task_url=self._task_url(target_task_id),
                title=draft.title,
                source_type=source_type,
                source_key=source_key,
                details={"checklists": checklist_details},
            )

        if effective_action == SyncAction.append_to_weekly:
            if not target_task_id:
                raise ValueError("Для daily -> weekly нужен task_id задачи недели.")
            comment_result = None
            already_appended = (
                binding
                and int(binding["bitrix_task_id"]) == int(target_task_id)
                and str(binding.get("mode")) == "daily_to_weekly"
            )
            if not already_appended:
                comment_result = self._send_task_comment(target_task_id, draft.comment or draft.description[:3000])
            checklist_details = []
            for group in draft.checklist_groups:
                checklist_details.append(
                    self.bitrix.add_checklist_group_deduped(target_task_id, group.title, group.items)
                )
            self.state.upsert_task_binding(
                source_type=source_type,
                source_key=source_key,
                bitrix_task_id=target_task_id,
                mode="daily_to_weekly",
                title=draft.title,
                meta=draft.meta,
            )
            return SyncResult(
                action="daily_to_weekly",
                task_id=target_task_id,
                task_url=self._task_url(target_task_id),
                title=draft.title,
                source_type=source_type,
                source_key=source_key,
                details={
                    "comment": bool(comment_result),
                    "comment_skipped": bool(already_appended),
                    "checklists": checklist_details,
                },
            )

        self._send_task_comment(target_task_id, draft.comment or draft.description[:3000])
        self.state.upsert_task_binding(
            source_type=source_type,
            source_key=source_key,
            bitrix_task_id=target_task_id,
            mode="commented",
            title=draft.title,
            meta=draft.meta,
        )
        return SyncResult(
            action="commented",
            task_id=target_task_id,
            task_url=self._task_url(target_task_id),
            title=draft.title,
            source_type=source_type,
            source_key=source_key,
        )

    def _create_task(self, draft: TaskDraft) -> int:
        fields = self._task_fields(draft)
        task_id = self.bitrix.create_task(fields)
        for group in draft.checklist_groups:
            self.bitrix.add_checklist_group_deduped(task_id, group.title, group.items)
        if draft.comment:
            try:
                self._send_task_comment(task_id, draft.comment)
            except Exception:
                pass
        return task_id

    def _send_task_comment(self, task_id: int, text: str) -> dict[str, Any]:
        return self.bitrix.send_task_comment(
            task_id,
            text,
            author_id=self.settings.bitrix_actor_user_id,
        )

    def _task_fields(self, draft: TaskDraft) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "TITLE": draft.title,
            "DESCRIPTION": draft.description,
            "GROUP_ID": self.settings.bitrix_group_id,
        }
        if self.settings.bitrix_default_responsible_id:
            fields["RESPONSIBLE_ID"] = self.settings.bitrix_default_responsible_id
        if self.settings.bitrix_created_by_id:
            fields["CREATED_BY"] = self.settings.bitrix_created_by_id
        if self.settings.bitrix_default_auditor_ids:
            fields["AUDITORS"] = self.settings.bitrix_default_auditor_ids
        if self._is_daily_plan_draft(draft) and self.settings.bitrix_daily_plan_accomplice_ids:
            fields["ACCOMPLICES"] = self.settings.bitrix_daily_plan_accomplice_ids
        if draft.tags:
            fields["TAGS"] = draft.tags
        return fields

    def _task_update_fields(self, draft: TaskDraft) -> dict[str, Any]:
        fields = {
            "TITLE": draft.title,
            "DESCRIPTION": draft.description,
        }
        if self._is_daily_plan_draft(draft) and self.settings.bitrix_daily_plan_accomplice_ids:
            fields["ACCOMPLICES"] = self.settings.bitrix_daily_plan_accomplice_ids
        if draft.tags:
            fields["TAGS"] = draft.tags
        return fields

    def _task_merge_update_fields(self, draft: TaskDraft, description: str) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "DESCRIPTION": description,
        }
        if self._is_daily_plan_draft(draft) and self.settings.bitrix_daily_plan_accomplice_ids:
            fields["ACCOMPLICES"] = self.settings.bitrix_daily_plan_accomplice_ids
        if draft.tags:
            fields["TAGS"] = draft.tags
        return fields

    @staticmethod
    def _is_daily_plan_draft(draft: TaskDraft) -> bool:
        return bool(draft.meta.get("daily_plan"))

    def _get_task_description(self, task_id: int) -> str:
        task = self._get_task_payload(task_id, select=["ID", "TITLE", "DESCRIPTION"]) or {}
        return str(task.get("description") or task.get("DESCRIPTION") or "").strip()

    def _get_task_payload(self, task_id: int, select: list[str] | None = None) -> dict[str, Any] | None:
        try:
            data = self.bitrix.get_task(task_id, select=select or ["ID", "TITLE", "DESCRIPTION"])
        except Exception:
            return None
        result = data.get("result") or {}
        task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(task, dict):
            task = result if isinstance(result, dict) else {}
        task_id_value = task.get("id") or task.get("ID")
        return task if task_id_value else None

    def _merge_task_description(
        self,
        *,
        existing_description: str,
        draft: TaskDraft,
        source_type: str,
        source_key: str,
    ) -> tuple[str, int, bool]:
        existing = (existing_description or "").strip()
        start_marker = self._point_start_marker(source_type, source_key)
        end_marker = self._point_end_marker(source_type, source_key)
        point_replaced = start_marker in existing and end_marker in existing

        if point_replaced:
            point_number = self._extract_existing_point_number(existing, start_marker) or self._next_point_number(existing)
            new_point = self._build_description_point(point_number, draft, source_type, source_key)
            pattern = re.compile(
                re.escape(start_marker) + r".*?" + re.escape(end_marker),
                flags=re.DOTALL,
            )
            return pattern.sub(new_point, existing, count=1), point_number, True

        base = existing
        if existing and "=== MEETING_DIGEST_POINT START" not in existing and "=== MEETING_DIGEST_CONTEXT START ===" not in existing:
            base = "\n".join(
                [
                    "# MeetingDigestBot: накопительное описание",
                    "",
                    "=== MEETING_DIGEST_CONTEXT START ===",
                    "## Point 1. Предыдущий контекст задачи из CRM",
                    existing,
                    "=== MEETING_DIGEST_CONTEXT END ===",
                ]
            )

        point_number = self._next_point_number(base)
        new_point = self._build_description_point(point_number, draft, source_type, source_key)
        if not base:
            return new_point, point_number, False
        return f"{base.rstrip()}\n\n{new_point}", point_number, False

    def _build_description_point(
        self,
        point_number: int,
        draft: TaskDraft,
        source_type: str,
        source_key: str,
    ) -> str:
        lines = [
            self._point_start_marker(source_type, source_key),
            f"## Point {point_number}. {draft.title}",
        ]
        post_url = draft.meta.get("post_url")
        if post_url:
            lines.append(f"Telegram post: {post_url}")
        loom_video_id = draft.meta.get("loom_video_id")
        if loom_video_id:
            lines.append(f"Loom video ID: {loom_video_id}")
        if draft.meta.get("google_doc_url"):
            lines.append(f"Summary Doc: {draft.meta.get('google_doc_url')}")
        if draft.meta.get("transcript_doc_url"):
            lines.append(f"Transcript Doc: {draft.meta.get('transcript_doc_url')}")
        lines.extend(["", draft.description.strip(), self._point_end_marker(source_type, source_key)])
        return "\n".join(line for line in lines if line is not None).strip()

    @staticmethod
    def _point_start_marker(source_type: str, source_key: str) -> str:
        return f"=== MEETING_DIGEST_POINT START source_type={source_type} source_key={source_key} ==="

    @staticmethod
    def _point_end_marker(source_type: str, source_key: str) -> str:
        return f"=== MEETING_DIGEST_POINT END source_type={source_type} source_key={source_key} ==="

    @staticmethod
    def _extract_existing_point_number(description: str, start_marker: str) -> int | None:
        start = description.find(start_marker)
        if start < 0:
            return None
        fragment = description[start : start + 500]
        match = re.search(r"##\s+Point\s+(\d+)\.", fragment)
        return int(match.group(1)) if match else None

    @staticmethod
    def _next_point_number(description: str) -> int:
        numbers = [int(value) for value in re.findall(r"##\s+Point\s+(\d+)\.", description or "")]
        return max(numbers, default=0) + 1

    @staticmethod
    def _point_checklist_title(point_number: int, draft: TaskDraft) -> str:
        title = " ".join(draft.title.split())
        if len(title) > 80:
            title = title[:77].rstrip() + "..."
        return f"Point {point_number}: {title}"

    @staticmethod
    def _point_checklist_items(draft: TaskDraft) -> list[str]:
        result: list[str] = []
        for group in draft.checklist_groups:
            for item in group.items:
                text = MeetingDigestService._checklist_item_title(item)
                if text:
                    result.append(f"{group.title}: {text}")
        return result

    def _resolve_action(self, *, action: SyncAction, has_existing: bool) -> SyncAction:
        if action != SyncAction.auto:
            return action
        return SyncAction.append_comment if has_existing else SyncAction.create

    def _preview_task_draft(
        self,
        *,
        draft: TaskDraft,
        source_type: str,
        source_key: str,
        target_task_id: int | None,
        stale_binding_task_id: int | None = None,
    ) -> SyncResult:
        effective_if_auto = self._resolve_action(action=SyncAction.auto, has_existing=bool(target_task_id))
        checklist_summary: list[dict[str, Any]] = []
        checklist_read_error = ""
        existing_checklist_items: list[dict[str, Any]] = []
        if target_task_id:
            try:
                existing_checklist_items = self.bitrix.list_checklist_items(target_task_id)
            except Exception as exc:
                checklist_read_error = str(exc)
        for group in draft.checklist_groups:
            group_summary: dict[str, Any] = {
                "title": group.title,
                "items_count": len(group.items),
                "items_preview": [self._checklist_item_preview(item) for item in group.items[:5]],
            }
            if target_task_id and not checklist_read_error:
                dedupe = self.bitrix.preview_checklist_group_dedupe(
                    existing_checklist_items,
                    group.title,
                    group.items,
                )
                group_summary.update(
                    {
                        "would_add": dedupe["would_add"],
                        "would_skip": dedupe["would_skip"],
                        "existing_group_id": dedupe["parent_id"] or None,
                    }
                )
            elif target_task_id and checklist_read_error:
                group_summary.update(
                    {
                        "would_add": len(group.items),
                        "would_skip": None,
                        "existing_group_id": None,
                        "dedupe_unavailable": True,
                    }
                )
            checklist_summary.append(group_summary)
        details: dict[str, Any] = {
            "would_action_if_auto": effective_if_auto.value,
            "description_chars": len(draft.description),
            "comment_chars": len(draft.comment),
            "checklists": checklist_summary,
            "task_matches": self._find_task_matches(draft),
            "tags": draft.tags,
            "meta": draft.meta,
        }
        if checklist_read_error:
            details["checklist_read_error"] = checklist_read_error
        if stale_binding_task_id:
            details["stale_binding_task_id"] = stale_binding_task_id
            details["stale_binding_reset"] = True
        return SyncResult(
            action="preview",
            task_id=target_task_id,
            task_url=self._task_url(target_task_id) if target_task_id else None,
            title=draft.title,
            source_type=source_type,
            source_key=source_key,
            details=details,
        )

    def _task_url(self, task_id: int) -> str:
        return f"https://totiscrm.com/workgroups/group/{self.settings.bitrix_group_id}/tasks/task/view/{task_id}/"

    def _task_exists(self, task_id: int) -> bool:
        return self._get_task_payload(task_id, select=["ID", "TITLE"]) is not None

    def _find_task_matches(self, draft: TaskDraft) -> list[dict[str, Any]]:
        try:
            data = self.bitrix.list_tasks(
                filter_data={"GROUP_ID": self.settings.bitrix_group_id},
                order={"ID": "desc"},
                select=["ID", "TITLE", "GROUP_ID"],
            )
        except Exception:
            return []
        result = data.get("result") or {}
        if isinstance(result, dict):
            tasks = result.get("tasks") or result.get("items") or []
        else:
            tasks = result if isinstance(result, list) else []
        return find_task_matches(
            draft_title=draft.title,
            tasks=list(tasks)[: self.settings.matching_task_limit],
            group_id=self.settings.bitrix_group_id,
            threshold=self.settings.matching_score_threshold,
            limit=5,
        )

    def _build_weekly_rollup(self, week_from: date, week_to: date, meetings: list) -> WeeklyRollup:
        summaries: list[str] = []
        commitments: list[str] = []
        blockers: list[str] = []
        tech_debt: list[str] = []
        business_requests: list[str] = []
        meeting_ids: list[str] = []

        for meeting in meetings:
            meeting_ids.append(meeting.loom_video_id)
            artifacts = meeting.artifacts or {}
            summary = str(artifacts.get("summary") or "").strip()
            if summary:
                summaries.append(f"{meeting.title}: {summary}")
            commitments.extend(self._unique_extend(commitments, self._extract_action_titles(artifacts.get("action_items"))))
            blockers.extend(self._unique_extend(blockers, self._extract_string_list(artifacts.get("blockers"))))
            tech_debt.extend(self._unique_extend(tech_debt, self._extract_string_list(artifacts.get("remaining_tech_debt"))))
            business_requests.extend(
                self._unique_extend(
                    business_requests,
                    [str(item.get("title")).strip() for item in artifacts.get("business_requests_for_estimation", []) if str(item.get("title") or "").strip()],
                )
            )

        base_rollup = WeeklyRollup(
            week_from=week_from,
            week_to=week_to,
            source_meeting_ids=meeting_ids,
            summary="\n".join(summaries[:12]).strip(),
            commitments=commitments,
            blockers=blockers,
            tech_debt=tech_debt,
            business_requests=business_requests,
        )
        try:
            enhanced = self.weekly_llm.enhance(
                week_from=week_from,
                week_to=week_to,
                base_rollup=base_rollup,
                meetings=meetings,
            )
        except Exception:
            enhanced = None
        return enhanced or base_rollup

    def _build_daily_rollup(self, report_date: date, meetings: list) -> DailyRollup:
        summaries: list[str] = []
        commitments: list[str] = []
        blockers: list[str] = []
        tech_debt: list[str] = []
        business_requests: list[str] = []
        meeting_ids: list[str] = []

        for meeting in meetings:
            meeting_ids.append(meeting.loom_video_id)
            artifacts = meeting.artifacts or {}
            summary = str(artifacts.get("summary") or "").strip()
            if summary:
                summaries.append(f"{meeting.title}: {summary}")
            commitments.extend(self._unique_extend(commitments, self._extract_action_titles(artifacts.get("action_items"))))
            blockers.extend(self._unique_extend(blockers, self._extract_string_list(artifacts.get("blockers"))))
            tech_debt.extend(self._unique_extend(tech_debt, self._extract_string_list(artifacts.get("remaining_tech_debt"))))
            business_requests.extend(
                self._unique_extend(
                    business_requests,
                    [str(item.get("title")).strip() for item in artifacts.get("business_requests_for_estimation", []) if str(item.get("title") or "").strip()],
                )
            )

        return DailyRollup(
            report_date=report_date,
            source_meeting_ids=meeting_ids,
            summary="\n".join(summaries[:12]).strip(),
            commitments=commitments,
            blockers=blockers,
            tech_debt=tech_debt,
            business_requests=business_requests,
        )

    @staticmethod
    def _is_daily_plan_meeting(meeting) -> bool:
        title = str(getattr(meeting, "title", "") or "").casefold()
        meeting_type = str(getattr(meeting, "meeting_type", "") or "").casefold()
        artifacts = getattr(meeting, "artifacts", {}) or {}
        tags = artifacts.get("tags") or artifacts.get("hashtags") or []
        tag_text = " ".join(str(tag).casefold() for tag in tags) if isinstance(tags, list) else str(tags).casefold()
        return "#daily" in title or meeting_type == "daily" or "#daily" in tag_text or " daily " in f" {tag_text} "

    @staticmethod
    def _checklist_item_title(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            return str(item.get("title") or item.get("TITLE") or "").strip()
        return str(getattr(item, "title", "") or "").strip()

    @staticmethod
    def _checklist_item_preview(item: Any) -> dict[str, Any] | str:
        title = MeetingDigestService._checklist_item_title(item)
        members = []
        if isinstance(item, dict):
            members = item.get("members") or item.get("MEMBERS") or []
        elif not isinstance(item, str):
            members = getattr(item, "members", []) or []
        if members:
            return {"title": title, "members": list(members)}
        return title

    @staticmethod
    def _extract_action_titles(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("title") or "").strip()
            else:
                text = str(item).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _extract_string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _unique_extend(existing: list[str], candidates: list[str]) -> list[str]:
        additions: list[str] = []
        seen = set(existing)
        for item in candidates:
            if item not in seen:
                additions.append(item)
                seen.add(item)
        return additions
