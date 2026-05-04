from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from .config import Settings
from .models import DailyPlanSyncRequest, DaySyncRequest, PostSyncRequest, PublicationRegistrationRequest, WeekSyncRequest
from .service import MeetingDigestService
from .telegram_bot import TelegramBotFacade


settings = Settings.from_env()
service = MeetingDigestService(settings)
app = FastAPI(title="MeetingDigestBot", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "meeting-digest-bot",
        "aicallorder_db": str(settings.aicallorder_db_path),
        "state_db": str(settings.state_db_path),
    }


@app.post("/publications/register")
def register_publication(
    payload: PublicationRegistrationRequest,
    x_meeting_digest_secret: str | None = Header(default=None),
) -> dict:
    expected = settings.meeting_digest_shared_secret
    if expected and x_meeting_digest_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid meeting digest shared secret")
    record = service.register_publication(payload)
    return {"ok": True, "record": record.model_dump()}


@app.post("/sync/post")
def sync_post(payload: PostSyncRequest) -> dict:
    result = service.sync_post(payload)
    return {"ok": True, "result": result.model_dump()}


@app.post("/sync/week")
def sync_week(payload: WeekSyncRequest) -> dict:
    result = service.sync_week(payload)
    return {"ok": True, "result": result.model_dump()}


@app.post("/sync/day")
def sync_day(payload: DaySyncRequest) -> dict:
    result = service.sync_day(payload)
    return {"ok": True, "result": result.model_dump()}


@app.post("/sync/daily-plan")
def sync_daily_plan(payload: DailyPlanSyncRequest) -> dict:
    result = service.sync_daily_plan(payload)
    return {"ok": True, "result": result.model_dump()}


@app.post("/telegram/webhook")
def telegram_webhook(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    expected = settings.telegram_webhook_secret
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not configured")
    bot = TelegramBotFacade(service=service, token=settings.telegram_bot_token)
    result = bot.process_update(update)
    return {"ok": result.ok, "response": result.model_dump()}
