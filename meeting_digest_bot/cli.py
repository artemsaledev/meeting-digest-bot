from __future__ import annotations

import argparse
import json
from datetime import date

from .config import Settings
from .models import PostSyncRequest, PublicationRegistrationRequest, SyncAction, WeekSyncRequest
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
