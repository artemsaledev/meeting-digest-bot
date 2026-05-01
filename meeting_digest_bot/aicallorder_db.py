from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import MeetingRecord


class AIcallorderRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get_meeting(self, loom_video_id: str) -> MeetingRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT loom_video_id, source_url, title, meeting_type, recorded_at, transcript_text, artifacts_json
                FROM meetings
                WHERE loom_video_id = ?
                LIMIT 1
                """,
                (loom_video_id,),
            ).fetchone()
        if not row:
            return None
        artifacts = self._safe_json_load(row[6])
        return MeetingRecord(
            loom_video_id=row[0],
            source_url=row[1],
            title=row[2],
            meeting_type=row[3],
            recorded_at=row[4],
            transcript_text=row[5] or "",
            artifacts=artifacts or {},
        )

    def list_meetings_between(self, week_from: date, week_to: date) -> list[MeetingRecord]:
        start = week_from.isoformat()
        finish = (week_to + timedelta(days=1)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT loom_video_id, source_url, title, meeting_type, recorded_at, transcript_text, artifacts_json
                FROM meetings
                WHERE recorded_at >= ?
                  AND recorded_at < ?
                  AND artifacts_json IS NOT NULL
                ORDER BY recorded_at ASC
                """,
                (start, finish),
            ).fetchall()
        result: list[MeetingRecord] = []
        for row in rows:
            result.append(
                MeetingRecord(
                    loom_video_id=row[0],
                    source_url=row[1],
                    title=row[2],
                    meeting_type=row[3],
                    recorded_at=row[4],
                    transcript_text=row[5] or "",
                    artifacts=self._safe_json_load(row[6]) or {},
                )
            )
        return result

    @staticmethod
    def _safe_json_load(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
