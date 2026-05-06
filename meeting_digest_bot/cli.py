from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .kb_intake import KnowledgeIntake
from .knowledge_alerts import format_notion_import_alert, read_knowledge_alert_chat_id, send_knowledge_alert, write_knowledge_alert_chat_id
from .knowledge_rag import KnowledgeVectorStore, client_from_env
from .knowledge_repo import KnowledgeRepository
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
    register.add_argument("--source-tag", action="append", default=[])
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

    export_knowledge = subparsers.add_parser("export-knowledge")
    export_knowledge.add_argument("--post-url")
    export_knowledge.add_argument("--date-from")
    export_knowledge.add_argument("--date-to")
    export_knowledge.add_argument("--limit", type=int)
    export_knowledge.add_argument("--output-dir", default="exports/knowledge")
    export_knowledge.add_argument("--bundle", choices=["all", "source", "machine", "prompts"], default="all")

    init_knowledge = subparsers.add_parser("init-knowledge-repo")
    init_knowledge.add_argument("--knowledge-dir", default="company-knowledge")

    upsert_knowledge = subparsers.add_parser("upsert-knowledge")
    upsert_knowledge.add_argument("--knowledge-dir", default="company-knowledge")
    upsert_knowledge.add_argument("--post-url")
    upsert_knowledge.add_argument("--date-from")
    upsert_knowledge.add_argument("--date-to")
    upsert_knowledge.add_argument("--limit", type=int)
    upsert_knowledge.add_argument("--draft", action="store_true")

    index_knowledge = subparsers.add_parser("index-knowledge")
    index_knowledge.add_argument("--knowledge-dir", default="company-knowledge")

    chunk_index = subparsers.add_parser("chunk-index-knowledge")
    chunk_index.add_argument("--knowledge-dir", default="company-knowledge")

    rag_index = subparsers.add_parser("build-knowledge-rag-index")
    rag_index.add_argument("--knowledge-dir", default="company-knowledge")
    rag_index.add_argument("--force", action="store_true")

    rag_search = subparsers.add_parser("rag-search-knowledge")
    rag_search.add_argument("query")
    rag_search.add_argument("--knowledge-dir", default="company-knowledge")
    rag_search.add_argument("--limit", type=int, default=5)

    rag_ask = subparsers.add_parser("rag-knowledge")
    rag_ask.add_argument("query")
    rag_ask.add_argument("--knowledge-dir", default="company-knowledge")
    rag_ask.add_argument("--limit", type=int, default=5)

    derive_catalogs = subparsers.add_parser("derive-knowledge-catalogs")
    derive_catalogs.add_argument("--knowledge-dir", default="company-knowledge")

    search_knowledge = subparsers.add_parser("search-knowledge")
    search_knowledge.add_argument("query")
    search_knowledge.add_argument("--knowledge-dir", default="company-knowledge")
    search_knowledge.add_argument("--limit", type=int, default=5)

    ask_knowledge = subparsers.add_parser("ask-knowledge")
    ask_knowledge.add_argument("query")
    ask_knowledge.add_argument("--knowledge-dir", default="company-knowledge")
    ask_knowledge.add_argument("--limit", type=int, default=5)

    revise_knowledge = subparsers.add_parser("revise-knowledge")
    revise_knowledge.add_argument("--knowledge-dir", default="company-knowledge")
    revise_knowledge.add_argument("--object-id", required=True)
    revise_knowledge.add_argument("--correction", required=True)
    revise_knowledge.add_argument("--output-dir")

    revision_status = subparsers.add_parser("set-revision-status")
    revision_status.add_argument("--metadata-path", required=True)
    revision_status.add_argument("--status", choices=["draft", "approved", "rejected"], required=True)

    apply_revision = subparsers.add_parser("apply-revision")
    apply_revision.add_argument("--metadata-path", required=True)

    apply_notion_import = subparsers.add_parser("apply-notion-import")
    apply_notion_import.add_argument("--metadata-path", required=True)

    generate_doc = subparsers.add_parser("generate-knowledge-doc")
    generate_doc.add_argument("--knowledge-dir", default="company-knowledge")
    generate_doc.add_argument("--object-id", required=True)
    generate_doc.add_argument("--kind", choices=["user_instruction", "technical_spec", "implementation_spec", "acceptance_checklist", "support_faq"], required=True)

    export_external = subparsers.add_parser("export-external-knowledge")
    export_external.add_argument("--knowledge-dir", default="company-knowledge")
    export_external.add_argument("--target", choices=["notebooklm", "agents"], default="notebooklm")
    export_external.add_argument("--output-dir")
    export_external.add_argument("--system")
    export_external.add_argument("--feature-area")
    export_external.add_argument("--object-type")

    notion_sync = subparsers.add_parser("sync-knowledge-notion")
    notion_sync.add_argument("--knowledge-dir", default="company-knowledge")
    notion_sync.add_argument("--apply", action="store_true")

    notion_import = subparsers.add_parser("import-knowledge-notion")
    notion_import.add_argument("--knowledge-dir", default="company-knowledge")
    notion_import.add_argument("--database", choices=["Task Cases", "Systems", "Features", "Instructions"])
    notion_import.add_argument("--object-id")
    notion_import.add_argument("--notify-proposals", action="store_true")

    health = subparsers.add_parser("knowledge-health")
    health.add_argument("--knowledge-dir", default="company-knowledge")

    alert_chat = subparsers.add_parser("set-knowledge-alert-chat")
    alert_chat.add_argument("--chat-id", required=True)

    pipeline = subparsers.add_parser("process-knowledge-pipeline")
    pipeline.add_argument("--knowledge-dir", default="company-knowledge")
    pipeline.add_argument("--limit", type=int)
    pipeline.add_argument("--skip-backfill", action="store_true")
    pipeline.add_argument("--export-target", choices=["none", "notebooklm", "agents"], default="none")

    backfill_tags = subparsers.add_parser("backfill-knowledge-tags")
    backfill_tags.add_argument("--limit", type=int)

    candidates = subparsers.add_parser("list-knowledge-candidates")
    candidates.add_argument("--status")

    runs = subparsers.add_parser("list-knowledge-runs")
    runs.add_argument("--limit", type=int, default=20)

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
            source_tags=args.source_tag,
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

    if args.command == "export-knowledge":
        intake = KnowledgeIntake(service)
        result = intake.export(
            output_dir=Path(args.output_dir),
            bundle=args.bundle,
            post_url=args.post_url,
            date_from=date.fromisoformat(args.date_from) if args.date_from else None,
            date_to=date.fromisoformat(args.date_to) if args.date_to else None,
            limit=args.limit,
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "init-knowledge-repo":
        result = KnowledgeRepository(Path(args.knowledge_dir)).init()
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "upsert-knowledge":
        intake = KnowledgeIntake(service)
        objects = intake.collect(
            post_url=args.post_url,
            date_from=date.fromisoformat(args.date_from) if args.date_from else None,
            date_to=date.fromisoformat(args.date_to) if args.date_to else None,
            limit=args.limit,
        )
        result = KnowledgeRepository(Path(args.knowledge_dir)).upsert_objects(objects, draft=args.draft)
        if not args.draft:
            for item in objects:
                for post_url in item.linked_telegram_posts:
                    service.state.update_kb_candidate_status(post_url=post_url, status="exported")
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "index-knowledge":
        result = KnowledgeRepository(Path(args.knowledge_dir)).build_index()
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "chunk-index-knowledge":
        result = KnowledgeRepository(Path(args.knowledge_dir)).build_chunk_index()
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "build-knowledge-rag-index":
        import os

        client = client_from_env(dict(os.environ))
        if not client:
            print(
                json.dumps(
                    {
                        "ready": False,
                        "missing_env": ["KNOWLEDGE_RAG_API_KEY or OPENAI_API_KEY or LLM_API_KEY"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        store = KnowledgeVectorStore(
            Path(args.knowledge_dir),
            db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
            embeddings_model=client.embeddings_model,
        )
        result = store.build(client=client, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "rag-search-knowledge":
        import os

        client = client_from_env(dict(os.environ))
        if not client:
            print(json.dumps({"ready": False, "missing_env": ["KNOWLEDGE_RAG_API_KEY or OPENAI_API_KEY or LLM_API_KEY"]}, ensure_ascii=False, indent=2))
            return 2
        store = KnowledgeVectorStore(
            Path(args.knowledge_dir),
            db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
            embeddings_model=client.embeddings_model,
        )
        print(json.dumps({"ready": True, "results": store.search(args.query, client=client, limit=args.limit)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "rag-knowledge":
        import os

        client = client_from_env(dict(os.environ), require_llm=True)
        if not client:
            print(json.dumps({"ready": False, "missing_env": ["KNOWLEDGE_RAG_API_KEY or OPENAI_API_KEY or LLM_API_KEY"]}, ensure_ascii=False, indent=2))
            return 2
        store = KnowledgeVectorStore(
            Path(args.knowledge_dir),
            db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
            embeddings_model=client.embeddings_model,
        )
        print(json.dumps(store.answer(args.query, embedding_client=client, chat_client=client, limit=args.limit), ensure_ascii=False, indent=2))
        return 0

    if args.command == "derive-knowledge-catalogs":
        result = KnowledgeRepository(Path(args.knowledge_dir)).derive_catalogs()
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "search-knowledge":
        results = KnowledgeRepository(Path(args.knowledge_dir)).search(args.query, limit=args.limit)
        print(json.dumps([item.model_dump() for item in results], ensure_ascii=False, indent=2))
        return 0

    if args.command == "ask-knowledge":
        result = KnowledgeRepository(Path(args.knowledge_dir)).ask(args.query, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "revise-knowledge":
        result = KnowledgeRepository(Path(args.knowledge_dir)).create_revision_proposal(
            object_id=args.object_id,
            correction=args.correction,
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "set-revision-status":
        result = KnowledgeRepository(Path(".")).set_revision_status(
            metadata_path=Path(args.metadata_path),
            status=args.status,
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "apply-revision":
        result = KnowledgeRepository(Path(".")).apply_revision(metadata_path=Path(args.metadata_path))
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "apply-notion-import":
        result = KnowledgeRepository(Path(".")).apply_notion_import(metadata_path=Path(args.metadata_path))
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "generate-knowledge-doc":
        result = KnowledgeRepository(Path(args.knowledge_dir)).generate_document(object_id=args.object_id, kind=args.kind)
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-external-knowledge":
        result = KnowledgeRepository(Path(args.knowledge_dir)).export_external_bundle(
            target=args.target,
            output_dir=Path(args.output_dir) if args.output_dir else None,
            system=args.system,
            feature_area=args.feature_area,
            object_type=args.object_type,
        )
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "sync-knowledge-notion":
        import os

        result = KnowledgeRepository(Path(args.knowledge_dir)).notion_sync_plan(apply=args.apply, env=dict(os.environ))
        if args.apply and not result.ready:
            print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "import-knowledge-notion":
        import os

        result = KnowledgeRepository(Path(args.knowledge_dir)).notion_import_proposals(
            env=dict(os.environ),
            database=args.database,
            object_id=args.object_id,
        )
        if not result.ready:
            print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
            return 2
        output = result.model_dump()
        if args.notify_proposals and result.proposals_count:
            output["telegram_alert"] = send_knowledge_alert(settings, format_notion_import_alert(result))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    if args.command == "knowledge-health":
        repo = KnowledgeRepository(Path(args.knowledge_dir))
        notion_import_dir = repo.root / "knowledge" / "drafts" / "notion_import"
        pending_notion_imports = 0
        for metadata_path in notion_import_dir.glob("*__notion_import.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get("status") in {"draft", "approved"}:
                pending_notion_imports += 1
        health = {
            "knowledge_dir": str(repo.root),
            "counts": {
                name: len(list((repo.root / rel).glob("*.json"))) - len(list((repo.root / rel).glob("*.notion.json")))
                for name, rel in {
                    "task_cases": "knowledge/task_cases",
                    "systems": "knowledge/systems",
                    "features": "knowledge/features",
                    "instructions": "knowledge/instructions",
                }.items()
            },
            "latest_runs": service.state.list_kb_runs(limit=3),
            "exports": {
                "notebooklm_zip": str(repo.root / "exports" / "notebooklm.zip"),
                "notebooklm_zip_exists": (repo.root / "exports" / "notebooklm.zip").exists(),
            },
            "pending_notion_import_proposals": pending_notion_imports,
            "knowledge_alert_chat_id": settings.knowledge_alert_chat_id or read_knowledge_alert_chat_id(),
            "rag": KnowledgeVectorStore(repo.root).stats(),
        }
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 0

    if args.command == "set-knowledge-alert-chat":
        value = write_knowledge_alert_chat_id(args.chat_id)
        result = send_knowledge_alert(settings, "Knowledge Base alert test: группа подключена.")
        print(json.dumps({"knowledge_alert_chat_id": value, "telegram_alert": result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "process-knowledge-pipeline":
        started_at = datetime.now(UTC).isoformat()
        run_id = "kb_run_" + started_at.replace(":", "").replace("-", "").replace(".", "")
        status = "success"
        summary = {}
        try:
            intake = KnowledgeIntake(service)
            if not args.skip_backfill:
                summary["backfill"] = intake.backfill_source_tags(limit=args.limit).model_dump()
            objects = intake.collect(limit=args.limit)
            repo = KnowledgeRepository(Path(args.knowledge_dir))
            summary["upsert"] = repo.upsert_objects(objects).model_dump()
            for item in objects:
                for post_url in item.linked_telegram_posts:
                    service.state.update_kb_candidate_status(post_url=post_url, status="exported")
            summary["derive_catalogs"] = repo.derive_catalogs().model_dump()
            summary["index"] = repo.build_index().model_dump()
            summary["chunk_index"] = repo.build_chunk_index().model_dump()
            import os

            if str(os.environ.get("KNOWLEDGE_RAG_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
                rag_client = client_from_env(dict(os.environ))
                if rag_client:
                    summary["rag_index"] = KnowledgeVectorStore(
                        repo.root,
                        db_path=Path(os.environ["KNOWLEDGE_VECTOR_DB_PATH"]) if os.environ.get("KNOWLEDGE_VECTOR_DB_PATH") else None,
                        embeddings_model=rag_client.embeddings_model,
                    ).build(client=rag_client)
                else:
                    summary["rag_index"] = {
                        "ready": False,
                        "missing_env": ["KNOWLEDGE_RAG_API_KEY or OPENAI_API_KEY or LLM_API_KEY"],
                    }
            for item in objects:
                for post_url in item.linked_telegram_posts:
                    service.state.update_kb_candidate_status(post_url=post_url, status="indexed")
            if args.export_target != "none":
                summary["external_export"] = repo.export_external_bundle(target=args.export_target).model_dump()
        except Exception as exc:
            status = "error"
            summary["error"] = str(exc)
        finished_at = datetime.now(UTC).isoformat()
        service.state.write_kb_run(
            run_id=run_id,
            operation="process_knowledge_pipeline",
            status=status,
            summary=summary,
            started_at=started_at,
            finished_at=finished_at,
        )
        output = {"run_id": run_id, "status": status, "summary": summary}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if status == "success" else 1

    if args.command == "backfill-knowledge-tags":
        result = KnowledgeIntake(service).backfill_source_tags(limit=args.limit)
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-knowledge-candidates":
        print(json.dumps(service.state.list_kb_candidates(status=args.status), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-knowledge-runs":
        print(json.dumps(service.state.list_kb_runs(limit=args.limit), ensure_ascii=False, indent=2))
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
