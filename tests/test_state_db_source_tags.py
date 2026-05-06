from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.models import PublicationRegistrationRequest
from meeting_digest_bot.state_db import StateRepository


class StateRepositorySourceTagsTests(unittest.TestCase):
    def test_register_publication_merges_source_tags_into_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = StateRepository(Path(tmp) / "state.db")
            record = repo.register_publication(
                PublicationRegistrationRequest(
                    post_url="https://t.me/c/1/2",
                    loom_video_id="loom123",
                    source_tags=["task_discussion", "#task_demo"],
                    payload={"source_tags": ["#task_discussion"]},
                )
            )
            self.assertEqual(record.payload_json["source_tags"], ["#task_discussion", "#task_demo"])

    def test_register_publication_marks_knowledge_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = StateRepository(Path(tmp) / "state.db")
            repo.register_publication(
                PublicationRegistrationRequest(
                    post_url="https://t.me/c/1/3",
                    loom_video_id="loom123",
                    source_tags=["#task_discussion"],
                )
            )
            candidates = repo.list_kb_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["post_url"], "https://t.me/c/1/3")
            self.assertEqual(candidates[0]["status"], "pending")

            repo.update_kb_candidate_status(post_url="https://t.me/c/1/3", status="indexed")
            self.assertEqual(repo.list_kb_candidates()[0]["status"], "indexed")

    def test_kb_run_log_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = StateRepository(Path(tmp) / "state.db")
            repo.write_kb_run(
                run_id="run1",
                operation="process_knowledge_pipeline",
                status="success",
                summary={"objects": 1},
                started_at="2026-05-05T00:00:00Z",
                finished_at="2026-05-05T00:00:01Z",
            )
            runs = repo.list_kb_runs()
            self.assertEqual(runs[0]["run_id"], "run1")
            self.assertEqual(runs[0]["summary"]["objects"], 1)


if __name__ == "__main__":
    unittest.main()
