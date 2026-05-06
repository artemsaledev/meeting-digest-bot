from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Settings
from .knowledge_rag import KnowledgeVectorStore, client_from_env
from .knowledge_repo import KnowledgeRepository
from .models import DailyPlanSyncRequest, DaySyncRequest, PostSyncRequest, PublicationRegistrationRequest, WeekSyncRequest
from .service import MeetingDigestService
from .telegram_bot import TelegramBotFacade


settings = Settings.from_env()
service = MeetingDigestService(settings)
app = FastAPI(title="MeetingDigestBot", version="0.1.0")


class KnowledgeRevisionRequest(BaseModel):
    object_id: str
    correction: str


class KnowledgeRevisionStatusRequest(BaseModel):
    metadata_path: str
    status: str


class KnowledgeNotionImportRequest(BaseModel):
    database: str | None = None
    object_id: str | None = None


class KnowledgeRagQueryRequest(BaseModel):
    query: str
    limit: int = 5


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "meeting-digest-bot",
        "aicallorder_db": str(settings.aicallorder_db_path),
        "state_db": str(settings.state_db_path),
    }


def _knowledge_repo() -> KnowledgeRepository:
    return KnowledgeRepository(Path(os.environ.get("KNOWLEDGE_REPO_PATH", "company-knowledge")))


def _knowledge_vector_store() -> KnowledgeVectorStore:
    env = dict(os.environ)
    embeddings_model = env.get("KNOWLEDGE_EMBEDDINGS_MODEL") or env.get("OPENAI_EMBEDDINGS_MODEL") or "text-embedding-3-small"
    db_path = env.get("KNOWLEDGE_VECTOR_DB_PATH")
    return KnowledgeVectorStore(
        _knowledge_repo().root,
        db_path=Path(db_path) if db_path else None,
        embeddings_model=embeddings_model,
    )


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


@app.get("/knowledge/search")
def search_knowledge(q: str, limit: int = 5) -> dict:
    results = _knowledge_repo().search(q, limit=limit)
    return {"ok": True, "results": [item.model_dump() for item in results]}


@app.get("/knowledge/ask")
def ask_knowledge(q: str, limit: int = 5) -> dict:
    return {"ok": True, "result": _knowledge_repo().ask(q, limit=limit)}


@app.get("/knowledge/rag/search")
def search_knowledge_rag(q: str, limit: int = 5) -> dict:
    client = client_from_env(dict(os.environ))
    if not client:
        raise HTTPException(status_code=400, detail="KNOWLEDGE_RAG_API_KEY, OPENAI_API_KEY, or LLM_API_KEY is required.")
    results = _knowledge_vector_store().search(q, client=client, limit=limit)
    return {"ok": True, "results": results}


@app.post("/knowledge/rag/ask")
def ask_knowledge_rag(payload: KnowledgeRagQueryRequest) -> dict:
    client = client_from_env(dict(os.environ), require_llm=True)
    if not client:
        raise HTTPException(status_code=400, detail="KNOWLEDGE_RAG_API_KEY, OPENAI_API_KEY, or LLM_API_KEY is required.")
    result = _knowledge_vector_store().answer(
        payload.query,
        embedding_client=client,
        chat_client=client,
        limit=payload.limit,
    )
    return {"ok": True, "result": result}


@app.get("/knowledge/rag/stats")
def knowledge_rag_stats() -> dict:
    return {"ok": True, "stats": _knowledge_vector_store().stats()}


@app.get("/knowledge/object/{object_id}")
def get_knowledge_object(object_id: str) -> dict:
    repo = _knowledge_repo()
    for rel_dir in ("task_cases", "systems", "features", "instructions"):
        path = repo.root / "knowledge" / rel_dir / f"{object_id}.json"
        if path.exists():
            return {"ok": True, "object": repo._read_json(path)}
    raise HTTPException(status_code=404, detail="Knowledge object not found")


@app.get("/knowledge/machine-bundle/{object_id}")
def get_machine_bundle(object_id: str) -> dict:
    repo = _knowledge_repo()
    object_response = get_knowledge_object(object_id)
    return {
        "ok": True,
        "bundle": {
            "instruction": "Use this object as grounded context and cite object_id/source task cases.",
            "knowledge_object": object_response["object"],
            "search_index": str(repo.root / "indexes" / "knowledge_index.json"),
        },
    }


@app.post("/knowledge/revisions")
def create_knowledge_revision(payload: KnowledgeRevisionRequest) -> dict:
    proposal = _knowledge_repo().create_revision_proposal(
        object_id=payload.object_id,
        correction=payload.correction,
    )
    return {"ok": True, "proposal": proposal.model_dump()}


@app.post("/knowledge/revisions/status")
def set_knowledge_revision_status(payload: KnowledgeRevisionStatusRequest) -> dict:
    if payload.status not in {"draft", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid revision status")
    proposal = _knowledge_repo().set_revision_status(
        metadata_path=Path(payload.metadata_path),
        status=payload.status,
    )
    return {"ok": True, "proposal": proposal.model_dump()}


@app.post("/knowledge/revisions/apply")
def apply_knowledge_revision(payload: KnowledgeRevisionStatusRequest) -> dict:
    proposal = _knowledge_repo().apply_revision(metadata_path=Path(payload.metadata_path))
    return {"ok": True, "proposal": proposal.model_dump()}


@app.post("/knowledge/notion/apply")
def apply_notion_import(payload: KnowledgeRevisionStatusRequest) -> dict:
    proposal = _knowledge_repo().apply_notion_import(metadata_path=Path(payload.metadata_path))
    return {"ok": True, "proposal": proposal.model_dump()}


@app.post("/knowledge/notion/import")
def import_notion_edits(payload: KnowledgeNotionImportRequest) -> dict:
    result = _knowledge_repo().notion_import_proposals(
        env=dict(os.environ),
        database=payload.database,
        object_id=payload.object_id,
    )
    if not result.ready:
        raise HTTPException(status_code=400, detail=result.model_dump())
    return {"ok": True, "result": result.model_dump()}


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
