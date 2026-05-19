from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.models import TaskExtractorAction, TaskExtractorRequest
from meeting_digest_bot.state_db import StateRepository
from meeting_digest_bot.task_extractor import TaskExtractorService


class _FakeAIcallorder:
    def get_meeting(self, loom_video_id: str):
        return None


class _FakeBitrix:
    def list_task_comments(self, task_id: int):
        return []

    def list_checklist_items(self, task_id: int):
        return []


class _FakeSettings:
    bitrix_tags = ["meeting-digest", "loom-digest"]
    bitrix_group_id = 512


class _FakeService:
    def __init__(self, db_path: Path) -> None:
        self.state = StateRepository(db_path)
        self.aicallorder = _FakeAIcallorder()
        self.bitrix = _FakeBitrix()
        self.settings = _FakeSettings()

    def _task_url(self, task_id: int) -> str:
        return f"https://totiscrm.com/workgroups/group/512/tasks/task/view/{task_id}/"

    def _get_task_payload(self, task_id: int, select=None):
        return {"id": task_id, "title": f"Task {task_id}", "description": "Existing task context"}


class TaskExtractorTests(unittest.TestCase):
    def test_collects_task_context_post_and_exports_notebook_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                service = TaskExtractorService(_FakeService(Path(tmp) / "state.db"))
                collect = service.handle(
                    TaskExtractorRequest(
                        action=TaskExtractorAction.collect,
                        chat_id="chat1",
                        message_id="10",
                        user_id="u1",
                        text=(
                            "Collect context for new feature. "
                            "https://totiscrm.com/workgroups/group/512/tasks/task/view/168334/ "
                            "Manual fact-check has priority over AI summary."
                        ),
                    )
                )
                self.assertEqual(collect.action, "collected")
                self.assertGreaterEqual(len(collect.sources), 2)
                self.assertTrue(any(item.source_type == "task_context_post" for item in collect.sources))
                self.assertTrue(any(item.bitrix_task_id == 168334 for item in collect.sources))

                exported = service.handle(TaskExtractorRequest(action=TaskExtractorAction.export, chat_id="chat1"))
                self.assertEqual(exported.action, "exported")
                self.assertTrue(Path(exported.zip_path).exists())
                manifest_path = Path(exported.export_dir) / "machine_bundle" / "handoff_manifest.json"
                self.assertTrue(manifest_path.exists())
                self.assertIn("Task Extractor export ready", exported.text)
            finally:
                os.chdir(previous_cwd)

    def test_clear_closes_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = TaskExtractorService(_FakeService(Path(tmp) / "state.db"))
            service.handle(
                TaskExtractorRequest(
                    action=TaskExtractorAction.collect,
                    chat_id="chat2",
                    message_id="1",
                    text="Manual context for a future functional task.",
                )
            )
            cleared = service.handle(TaskExtractorRequest(action=TaskExtractorAction.clear, chat_id="chat2"))
            self.assertEqual(cleared.action, "cleared")
            status = service.handle(TaskExtractorRequest(action=TaskExtractorAction.status, chat_id="chat2"))
            self.assertIn("no active session", status.text)


if __name__ == "__main__":
    unittest.main()
