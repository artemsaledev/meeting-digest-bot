from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.aicallorder_archive import (
    archive_block_to_knowledge_object,
    import_aicallorder_archive,
    parse_aicallorder_archive,
)


ARCHIVE_TEXT = """[[LOOM_VIDEO_ID:abc123]]
Meeting Note: #task_demo 16.04 Yavdokimenko AssetPayments Общий платеж

Metadata
- Loom video ID: abc123
- Meeting type: discord-sync
- Recorded at: 2026-04-16T14:42:48.780279
- Source URL: https://www.loom.com/share/abc123
- Summary Doc: https://docs.google.com/document/d/source/edit
- Transcript Doc: https://docs.google.com/document/d/transcript/edit

Summary
Обсуждалась реализация единого платежа с распределением по группам товаров.

Decisions
- Создавать единый консолидированный платеж.
- Контролировать платежи по site-id и union-id.

Action Items
- Провести тестирование реализации консолидированного платежа
  Owner: Миша
  Due: -
  Status: В процессе

Blockers
- Неясности с поддержкой нескольких форм оплаты.

Technical Spec Draft
Title: Реализация единого консолидированного платежа
Goal: Обеспечить единый платеж.

Scope
- Создание единого платежа.

Functional Requirements
- Возможность создавать единый платеж вручную и автоматически.

Acceptance Criteria
- Единый платеж создается корректно.

Open Questions
- Как разделять товары по способам оплаты?
[[/LOOM_VIDEO_ID:abc123]]
"""


class AicallorderArchiveTests(unittest.TestCase):
    def test_parse_archive_block(self) -> None:
        blocks = parse_aicallorder_archive(ARCHIVE_TEXT)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].loom_id, "abc123")
        self.assertIn("#task_demo", blocks[0].title)
        self.assertEqual(blocks[0].metadata["Recorded at"], "2026-04-16T14:42:48.780279")
        self.assertIn("Functional Requirements", blocks[0].sections)

    def test_archive_block_to_knowledge_object(self) -> None:
        item = archive_block_to_knowledge_object(parse_aicallorder_archive(ARCHIVE_TEXT)[0])

        self.assertEqual(item.system, "bitrix")
        self.assertEqual(item.feature_area, "payments")
        self.assertEqual(item.source_tags, ["#task_demo"])
        self.assertEqual(item.linked_loom_ids, ["abc123"])
        self.assertIn("Возможность создавать единый платеж вручную и автоматически.", item.current_requirements)
        self.assertIn("Единый платеж создается корректно.", item.acceptance_criteria)
        self.assertEqual(item.source_events[0].event_type, "demo")

    def test_import_archive_writes_kb_objects_and_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "archive.txt"
            source.write_text(ARCHIVE_TEXT, encoding="utf-8")

            result = import_aicallorder_archive(source_file=source, knowledge_dir=root / "kb")

            self.assertEqual(result.objects_count, 1)
            self.assertTrue((root / "kb" / "knowledge" / "task_cases").exists())
            self.assertTrue((root / "kb" / "indexes" / "knowledge_index.json").exists())
            self.assertTrue((root / "kb" / "indexes" / "knowledge_chunks.json").exists())


if __name__ == "__main__":
    unittest.main()
