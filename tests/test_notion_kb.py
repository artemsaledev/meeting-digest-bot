from __future__ import annotations

import unittest

from meeting_digest_bot.notion_kb import NotionKnowledgeClient, NotionTarget


class NotionKnowledgeTests(unittest.TestCase):
    def test_target_prefers_data_source_id(self) -> None:
        target = NotionTarget.from_env(
            {
                "NOTION_API_KEY": "secret",
                "NOTION_DATA_SOURCE_TASK_CASES": "data-source-id",
                "NOTION_DB_TASK_CASES": "database-id",
            }
        )
        self.assertEqual(target.parent_type, "data_source_id")
        self.assertEqual(target.api_version, "2025-09-03")

    def test_target_supports_catalog_database_keys(self) -> None:
        target = NotionTarget.from_env({"NOTION_DB_SYSTEMS": "systems-db"}, key="SYSTEMS")
        self.assertEqual(target.parent_id, "systems-db")
        self.assertEqual(target.parent_type, "database_id")

    def test_markdown_to_blocks_splits_supported_blocks(self) -> None:
        blocks = NotionKnowledgeClient.markdown_to_blocks("# Title\n\n- Item\n\nParagraph")
        self.assertEqual(blocks[0]["type"], "heading_1")
        self.assertEqual(blocks[1]["type"], "bulleted_list_item")
        self.assertEqual(blocks[2]["type"], "paragraph")

    def test_blocks_to_markdown_roundtrip_shape(self) -> None:
        blocks = NotionKnowledgeClient.markdown_to_blocks("# Title\n\n## Section\n\n- Item\n\nParagraph")
        markdown = NotionKnowledgeClient.blocks_to_markdown(blocks)
        self.assertIn("# Title", markdown)
        self.assertIn("## Section", markdown)
        self.assertIn("- Item", markdown)
        self.assertIn("Paragraph", markdown)

    def test_upsert_skips_body_rewrite_when_markdown_is_unchanged(self) -> None:
        class FakeClient(NotionKnowledgeClient):
            def __init__(self) -> None:
                super().__init__(token="secret", target=NotionTarget("parent", "database_id", "2022-06-28"))
                self.replaced = False

            def find_page_by_object_id(self, object_id: str) -> dict | None:
                return {"id": "page1", "url": "https://notion.local/page1"}

            def update_page(self, page_id: str, properties: dict) -> dict:
                return {"id": page_id}

            def list_block_children(self, block_id: str) -> list[dict]:
                return self.markdown_to_blocks("# Title\n\n- Item")

            def replace_page_blocks(self, page_id: str, blocks: list[dict]) -> None:
                self.replaced = True

        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            projection_path = Path(tmp) / "projection.json"
            projection_path.write_text(
                json.dumps(
                    {
                        "database": "Task Cases",
                        "properties": {"ID": "obj1", "Title": "Title"},
                        "content_markdown": "# Title\n\n- Item\n",
                    }
                ),
                encoding="utf-8",
            )
            client = FakeClient()
            result = client.upsert_projection(projection_path)
            self.assertEqual(result["action"], "update_page_metadata")
            self.assertFalse(client.replaced)


if __name__ == "__main__":
    unittest.main()
