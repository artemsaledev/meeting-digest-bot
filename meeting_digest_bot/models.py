from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DigestType(str, Enum):
    meeting = "meeting"
    daily = "daily"
    weekly = "weekly"


class SyncAction(str, Enum):
    auto = "auto"
    preview = "preview"
    create = "create"
    update_description = "update_description"
    append_comment = "append_comment"
    append_checklists = "append_checklists"
    append_to_weekly = "append_to_weekly"


class PublicationRegistrationRequest(BaseModel):
    post_url: str
    telegram_chat_id: str | None = None
    telegram_message_id: str | None = None
    digest_type: DigestType = DigestType.meeting
    loom_video_id: str | None = None
    report_date: date | None = None
    week_from: date | None = None
    week_to: date | None = None
    meeting_title: str | None = None
    source_url: str | None = None
    google_doc_url: str | None = None
    transcript_doc_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PostSyncRequest(BaseModel):
    post_url: str
    action: SyncAction = SyncAction.auto
    task_id: int | None = None


class WeekSyncRequest(BaseModel):
    week_from: date
    week_to: date
    action: SyncAction = SyncAction.auto
    task_id: int | None = None


class DaySyncRequest(BaseModel):
    report_date: date
    action: SyncAction = SyncAction.auto
    task_id: int | None = None


class ChecklistGroup(BaseModel):
    title: str
    items: list[str] = Field(default_factory=list)


class TaskDraft(BaseModel):
    title: str
    description: str
    comment: str = ""
    checklist_groups: list[ChecklistGroup] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class PublicationRecord(BaseModel):
    id: int
    post_url: str
    telegram_chat_id: str | None = None
    telegram_message_id: str | None = None
    digest_type: str
    loom_video_id: str | None = None
    report_date: str | None = None
    week_from: str | None = None
    week_to: str | None = None
    meeting_title: str | None = None
    source_url: str | None = None
    google_doc_url: str | None = None
    transcript_doc_url: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MeetingRecord(BaseModel):
    loom_video_id: str
    source_url: str
    title: str
    meeting_type: str
    recorded_at: str | None = None
    transcript_text: str
    artifacts: dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    action: str
    task_id: int | None = None
    task_url: str | None = None
    title: str = ""
    source_type: str = ""
    source_key: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class WeeklyRollup(BaseModel):
    week_from: date
    week_to: date
    source_meeting_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    commitments: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    tech_debt: list[str] = Field(default_factory=list)
    business_requests: list[str] = Field(default_factory=list)


class DailyRollup(BaseModel):
    report_date: date
    source_meeting_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    commitments: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    tech_debt: list[str] = Field(default_factory=list)
    business_requests: list[str] = Field(default_factory=list)


class TelegramCommand(BaseModel):
    post_url: str | None = None
    task_id: int | None = None
    action: SyncAction = SyncAction.auto
    report_date: date | None = None
    week_from: date | None = None
    week_to: date | None = None


class TelegramResponse(BaseModel):
    ok: bool
    text: str
    payload: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
