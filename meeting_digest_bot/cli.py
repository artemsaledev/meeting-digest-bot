from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .models import DailyPlanSyncRequest, DailyReportRequest, PostSyncRequest, PublicationRegistrationRequest, SyncAction, WeekSyncRequest, WeeklyReportRequest
from .service import MeetingDigestService
from .telegram_bot import TelegramBotFacade
from .telegram_poller import TelegramPollingWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-digest-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register-publication")
    register.add_argument("--post-url", required=True)
    register.add_argument("--digest-type", choices=["meeting", "daily", "weekly"], default="meeting")
    register.add_argument("--loom-video-id")
    register.add_argument("--meeting-title")
    register.add_argument("--source-url")
    register.add_argument("--google-doc-url")
    register.add_argument("--transcript-doc-url")
    register.add_argument("--telegram-chat-id")
    register.add_argument("--telegram-message-id")
    register.add_argument("--report-date")
    register.add_argument("--week-from")
    register.add_argument("--week-to")
    register.add_argument("--payload-json")

    sync_post = subparsers.add_parser("sync-post")
    sync_post.add_argument("--post-url", required=True)
    sync_post.add_argument("--action", choices=[item.value for item in SyncAction], default="auto")
    sync_post.add_argument("--task-id", type=int)

    sync_week = subparsers.add_parser("sync-week")
    sync_week.add_argument("--week-from", required=True)
    sync_week.add_argument("--week-to", required=True)
    sync_week.add_argument("--action", choices=[item.value for item in SyncAction], default="auto")
    sync_week.add_argument("--task-id", type=int)

    sync_day = subparsers.add_parser("sync-day")
    sync_day.add_argument("--report-date", required=True)
    sync_day.add_argument("--action", choices=[item.value for item in SyncAction], default="auto")
    sync_day.add_argument("--task-id", type=int)

    sync_daily_plan = subparsers.add_parser("sync-daily-plan")
    sync_daily_plan.add_argument("--report-date", required=True)
    sync_daily_plan.add_argument("--action", choices=[item.value for item in SyncAction], default="preview")
    sync_daily_plan.add_argument("--task-id", type=int)
    sync_daily_plan.add_argument("--team-name", default="Bitrix Develop Team")

    daily_report = subparsers.add_parser("daily-report")
    daily_report.add_argument("--report-date")
    daily_report.add_argument("--yesterday", action="store_true")
    daily_report.add_argument("--team-name", default="Bitrix Develop Team")
    daily_report.add_argument("--force", action="store_true")
    daily_report.add_argument("--no-telegram", action="store_true")

    weekly_report = subparsers.add_parser("weekly-report")
    weekly_report.add_argument("--week-from")
    weekly_report.add_argument("--week-to")
    weekly_report.add_argument("--current-week", action="store_true")
    weekly_report.add_argument("--team-name", default="Bitrix Develop Team")
    weekly_report.add_argument("--force", action="store_true")
    weekly_report.add_argument("--no-telegram", action="store_true")

    poll = subparsers.add_parser("poll-telegram")
    poll.add_argument("--once", action="store_true")
    poll.add_argument("--offset", type=int)
    poll.add_argument("--limit", type=int, default=20)
    poll.add_argument("--timeout", type=int, default=30)
    poll.add_argument("--drop-pending", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    service = MeetingDigestService(settings)

    if args.command == "register-publication":
        payload = PublicationRegistrationRequest(
            post_url=args.post_url,
            digest_type=args.digest_type,
            loom_video_id=args.loom_video_id,
            meeting_title=args.meeting_title,
            source_url=args.source_url,
            google_doc_url=args.google_doc_url,
            transcript_doc_url=args.transcript_doc_url,
            telegram_chat_id=args.telegram_chat_id,
            telegram_message_id=args.telegram_message_id,
            report_date=date.fromisoformat(args.report_date) if args.report_date else None,
            week_from=date.fromisoformat(args.week_from) if args.week_from else None,
            week_to=date.fromisoformat(args.week_to) if args.week_to else None,
            payload=json.loads(args.payload_json) if args.payload_json else {},
        )
        record = service.register_publication(payload)
        print(json.dumps(record.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-post":
        result = service.sync_post(
            PostSyncRequest(
                post_url=args.post_url,
                action=SyncAction(args.action),
                task_id=args.task_id,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-week":
        result = service.sync_week(
            WeekSyncRequest(
                week_from=date.fromisoformat(args.week_from),
                week_to=date.fromisoformat(args.week_to),
                action=SyncAction(args.action),
                task_id=args.task_id,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-day":
        from .models import DaySyncRequest

        result = service.sync_day(
            DaySyncRequest(
                report_date=date.fromisoformat(args.report_date),
                action=SyncAction(args.action),
                task_id=args.task_id,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-daily-plan":
        result = service.sync_daily_plan(
            DailyPlanSyncRequest(
                report_date=date.fromisoformat(args.report_date),
                action=SyncAction(args.action),
                task_id=args.task_id,
                team_name=args.team_name,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "daily-report":
        report_date = _report_date_arg(args.report_date, yesterday=args.yesterday)
        result = service.run_daily_report(
            DailyReportRequest(
                report_date=report_date,
                team_name=args.team_name,
                force=args.force,
                send_telegram=not args.no_telegram,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "weekly-report":
        week_from, week_to = _week_args(args.week_from, args.week_to, current_week=args.current_week)
        result = service.run_weekly_report(
            WeeklyReportRequest(
                week_from=week_from,
                week_to=week_to,
                team_name=args.team_name,
                force=args.force,
                send_telegram=not args.no_telegram,
            )
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "poll-telegram":
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
        bot = TelegramBotFacade(service=service, token=settings.telegram_bot_token)
        poller = TelegramPollingWorker(bot=bot, poll_timeout_seconds=args.timeout)
        if args.drop_pending:
            poller.drop_pending_updates()
        result = poller.run(once=args.once, start_offset=args.offset, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


def _kyiv_today() -> date:
    return datetime.now(ZoneInfo("Europe/Kyiv")).date()


def _report_date_arg(raw: str | None, *, yesterday: bool) -> date:
    if raw:
        return date.fromisoformat(raw)
    today = _kyiv_today()
    return today - timedelta(days=1) if yesterday else today


def _week_args(raw_from: str | None, raw_to: str | None, *, current_week: bool) -> tuple[date, date]:
    if raw_from and raw_to:
        return date.fromisoformat(raw_from), date.fromisoformat(raw_to)
    if current_week:
        today = _kyiv_today()
        monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)
        return monday, friday
    raise ValueError("Provide --week-from and --week-to, or use --current-week.")
