from __future__ import annotations

import unittest

from meeting_digest_bot.models import ChecklistGroup, ChecklistItem, TaskDraft
from meeting_digest_bot.service import MeetingDigestService


class ServicePointMergeTests(unittest.TestCase):
    def test_point_checklist_items_works_without_service_instance(self) -> None:
        draft = TaskDraft(
            title="Test meeting",
            description="Description",
            checklist_groups=[
                ChecklistGroup(
                    title="QA",
                    items=[
                        "Plain item",
                        ChecklistItem(title="Structured item", members=[114736]),
                    ],
                )
            ],
        )

        items = MeetingDigestService._point_checklist_items(draft)

        self.assertEqual(items, ["QA: Plain item", "QA: Structured item"])


if __name__ == "__main__":
    unittest.main()
