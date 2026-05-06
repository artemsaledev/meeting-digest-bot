from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import PublicationRecord, PublicationRegistrationRequest


class StateRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_url TEXT NOT NULL UNIQUE,
                    telegram_chat_id TEXT,
                    telegram_message_id TEXT,
                    digest_type TEXT NOT NULL,
                    loom_video_id TEXT,
                    report_date TEXT,
                    week_from TEXT,
                    week_to TEXT,
                    meeting_title TEXT,
                    source_url TEXT,
                    google_doc_url TEXT,
                    transcript_doc_url TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_task_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    bitrix_task_id INTEGER NOT NULL,
                    external_task_number TEXT,
                    mode TEXT NOT NULL,
                    title TEXT,
                    meta_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_rollups (
                    week_from TEXT NOT NULL,
                    week_to TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    source_meeting_ids_json TEXT NOT NULL,
                    bitrix_task_id INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (week_from, week_to)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_url TEXT NOT NULL UNIQUE,
                    loom_video_id TEXT,
                    source_tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_runs (
                    run_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def register_publication(self, payload: PublicationRegistrationRequest) -> PublicationRecord:
        now = datetime.now(UTC).isoformat()
        payload_json = self._payload_with_source_tags(payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO publications (
                    post_url, telegram_chat_id, telegram_message_id, digest_type, loom_video_id,
                    report_date, week_from, week_to, meeting_title, source_url,
                    google_doc_url, transcript_doc_url, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_url) DO UPDATE SET
                    telegram_chat_id = COALESCE(excluded.telegram_chat_id, publications.telegram_chat_id),
                    telegram_message_id = COALESCE(excluded.telegram_message_id, publications.telegram_message_id),
                    digest_type = excluded.digest_type,
                    loom_video_id = COALESCE(excluded.loom_video_id, publications.loom_video_id),
                    report_date = COALESCE(excluded.report_date, publications.report_date),
                    week_from = COALESCE(excluded.week_from, publications.week_from),
                    week_to = COALESCE(excluded.week_to, publications.week_to),
                    meeting_title = COALESCE(excluded.meeting_title, publications.meeting_title),
                    source_url = COALESCE(excluded.source_url, publications.source_url),
                    google_doc_url = COALESCE(excluded.google_doc_url, publications.google_doc_url),
                    transcript_doc_url = COALESCE(excluded.transcript_doc_url, publications.transcript_doc_url),
                    payload_json = excluded.payload_json
                """,
                (
                    payload.post_url,
                    payload.telegram_chat_id,
                    payload.telegram_message_id,
                    payload.digest_type.value,
                    payload.loom_video_id,
                    payload.report_date.isoformat() if payload.report_date else None,
                    payload.week_from.isoformat() if payload.week_from else None,
                    payload.week_to.isoformat() if payload.week_to else None,
                    payload.meeting_title,
                    payload.source_url,
                    payload.google_doc_url,
                    payload.transcript_doc_url,
                    json.dumps(payload_json, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        record = self.get_publication_by_post_url(payload.post_url)
        if record is None:
            raise RuntimeError("Publication registration failed.")
        self._upsert_kb_candidate_if_needed(record)
        return record

    def get_publication_by_post_url(self, post_url: str) -> PublicationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, post_url, telegram_chat_id, telegram_message_id, digest_type, loom_video_id,
                       report_date, week_from, week_to, meeting_title, source_url,
                       google_doc_url, transcript_doc_url, payload_json, created_at
                FROM publications
                WHERE post_url = ?
                LIMIT 1
                """,
                (post_url,),
            ).fetchone()
        if not row:
            return None
        return self._publication_from_row(row)

    def list_publications(
        self,
        *,
        digest_type: str | None = None,
        limit: int | None = None,
    ) -> list[PublicationRecord]:
        query = """
            SELECT id, post_url, telegram_chat_id, telegram_message_id, digest_type, loom_video_id,
                   report_date, week_from, week_to, meeting_title, source_url,
                   google_doc_url, transcript_doc_url, payload_json, created_at
            FROM publications
        """
        params: list[Any] = []
        if digest_type:
            query += " WHERE digest_type = ?"
            params.append(digest_type)
        query += " ORDER BY id ASC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._publication_from_row(row) for row in rows]

    def _publication_from_row(self, row: sqlite3.Row | tuple) -> PublicationRecord:
        return PublicationRecord(
            id=row[0],
            post_url=row[1],
            telegram_chat_id=row[2],
            telegram_message_id=row[3],
            digest_type=row[4],
            loom_video_id=row[5],
            report_date=row[6],
            week_from=row[7],
            week_to=row[8],
            meeting_title=row[9],
            source_url=row[10],
            google_doc_url=row[11],
            transcript_doc_url=row[12],
            payload_json=self._safe_json_load(row[13]),
            created_at=row[14],
        )

    def upsert_task_binding(
        self,
        *,
        source_type: str,
        source_key: str,
        bitrix_task_id: int,
        mode: str,
        title: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(meta or {}, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crm_task_bindings (
                    source_type, source_key, bitrix_task_id, external_task_number,
                    mode, title, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                    bitrix_task_id = excluded.bitrix_task_id,
                    external_task_number = excluded.external_task_number,
                    mode = excluded.mode,
                    title = excluded.title,
                    meta_json = excluded.meta_json,
                    updated_at = excluded.updated_at
                """,
                (
                    source_type,
                    source_key,
                    bitrix_task_id,
                    str(bitrix_task_id),
                    mode,
                    title,
                    payload,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_task_binding(self, *, source_type: str, source_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_key, bitrix_task_id, external_task_number, mode, title, meta_json
                FROM crm_task_bindings
                WHERE source_type = ? AND source_key = ?
                LIMIT 1
                """,
                (source_type, source_key),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "source_type": row[1],
            "source_key": row[2],
            "bitrix_task_id": row[3],
            "external_task_number": row[4],
            "mode": row[5],
            "title": row[6],
            "meta": self._safe_json_load(row[7]),
        }

    def delete_task_binding(self, *, source_type: str, source_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM crm_task_bindings
                WHERE source_type = ? AND source_key = ?
                """,
                (source_type, source_key),
            )
            conn.commit()

    def get_latest_telegram_chat_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT telegram_chat_id
                FROM publications
                WHERE telegram_chat_id IS NOT NULL
                  AND telegram_chat_id != ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def save_weekly_rollup(
        self,
        *,
        week_from: str,
        week_to: str,
        summary: dict[str, Any],
        source_meeting_ids: list[str],
        bitrix_task_id: int | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_rollups (week_from, week_to, summary_json, source_meeting_ids_json, bitrix_task_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(week_from, week_to) DO UPDATE SET
                    summary_json = excluded.summary_json,
                    source_meeting_ids_json = excluded.source_meeting_ids_json,
                    bitrix_task_id = COALESCE(excluded.bitrix_task_id, weekly_rollups.bitrix_task_id),
                    updated_at = excluded.updated_at
                """,
                (
                    week_from,
                    week_to,
                    json.dumps(summary, ensure_ascii=False, default=str),
                    json.dumps(source_meeting_ids, ensure_ascii=False),
                    bitrix_task_id,
                    now,
                ),
            )
            conn.commit()

    def update_publication_payload(self, *, post_url: str, payload: dict[str, Any]) -> PublicationRecord | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE publications
                SET payload_json = ?
                WHERE post_url = ?
                """,
                (json.dumps(payload, ensure_ascii=False), post_url),
            )
            conn.commit()
        record = self.get_publication_by_post_url(post_url)
        if record:
            self._upsert_kb_candidate_if_needed(record)
        return record

    def list_kb_candidates(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT id, post_url, loom_video_id, source_tags_json, status, created_at, updated_at
            FROM kb_candidates
        """
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row[0],
                "post_url": row[1],
                "loom_video_id": row[2],
                "source_tags": self._safe_json_list(row[3]),
                "status": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
            for row in rows
        ]

    def update_kb_candidate_status(self, *, post_url: str, status: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE kb_candidates
                SET status = ?, updated_at = ?
                WHERE post_url = ?
                """,
                (status, now, post_url),
            )
            conn.commit()

    def write_kb_run(
        self,
        *,
        run_id: str,
        operation: str,
        status: str,
        summary: dict[str, Any],
        started_at: str,
        finished_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO kb_runs (run_id, operation, status, summary_json, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, operation, status, json.dumps(summary, ensure_ascii=False, default=str), started_at, finished_at),
            )
            conn.commit()

    def list_kb_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, operation, status, summary_json, started_at, finished_at
                FROM kb_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": row[0],
                "operation": row[1],
                "status": row[2],
                "summary": self._safe_json_load(row[3]),
                "started_at": row[4],
                "finished_at": row[5],
            }
            for row in rows
        ]

    @staticmethod
    def _safe_json_load(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _payload_with_source_tags(payload: PublicationRegistrationRequest) -> dict[str, Any]:
        result = dict(payload.payload or {})
        existing = result.get("source_tags") or []
        if not isinstance(existing, list):
            existing = [existing]
        tags: list[str] = []
        seen: set[str] = set()
        for tag in [*existing, *payload.source_tags]:
            normalized = str(tag or "").strip()
            if not normalized:
                continue
            if not normalized.startswith("#"):
                normalized = f"#{normalized}"
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(normalized)
        if tags:
            result["source_tags"] = tags
        return result

    def _upsert_kb_candidate_if_needed(self, record: PublicationRecord) -> None:
        tags = self._safe_json_list(json.dumps((record.payload_json or {}).get("source_tags") or []))
        normalized = {str(tag).casefold() for tag in tags}
        if "#daily" in normalized or not ({"#task_discussion", "#task_demo"} & normalized):
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kb_candidates (post_url, loom_video_id, source_tags_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_url) DO UPDATE SET
                    loom_video_id = excluded.loom_video_id,
                    source_tags_json = excluded.source_tags_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.post_url,
                    record.loom_video_id,
                    json.dumps(tags, ensure_ascii=False),
                    "pending",
                    now,
                    now,
                ),
            )
            conn.commit()

    @staticmethod
    def _safe_json_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if str(item).strip()]
