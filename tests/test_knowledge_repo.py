from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.kb_intake import KnowledgeObject, KnowledgeSourceEvent
from meeting_digest_bot.knowledge_repo import KnowledgeRepository


def knowledge_object(object_id: str = "task_case__bitrix_123") -> KnowledgeObject:
    return KnowledgeObject(
        object_id=object_id,
        title="Bitrix checklist sync",
        system="bitrix",
        feature_area="checklists",
        source_tags=["#task_discussion"],
        linked_bitrix_tasks=[123],
        current_summary="Checklist sync for Bitrix tasks.",
        current_requirements=["Create checklist groups from Loom artifacts"],
        acceptance_criteria=["Duplicate checklist items are skipped"],
        decisions=["Use deduped checklist group creation"],
        source_events=[
            KnowledgeSourceEvent(
                event_id="discussion__loom123",
                event_type="discussion",
                title="Checklist discussion",
                loom_video_id="loom123",
                loom_url="https://www.loom.com/share/loom123",
                telegram_post_url="https://t.me/c/1/2",
            )
        ],
    )


class KnowledgeRepositoryTests(unittest.TestCase):
    def test_init_creates_repo_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = KnowledgeRepository(Path(tmp)).init()
            self.assertTrue((Path(tmp) / "knowledge" / "task_cases").exists())
            self.assertTrue((Path(tmp) / "meta" / "notion_mapping.json").exists())
            self.assertTrue(result.written_files)

    def test_upsert_writes_task_case_and_notion_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            result = repo.upsert_objects([knowledge_object()])
            self.assertEqual(result.objects_count, 1)
            object_path = Path(tmp) / "knowledge" / "task_cases" / "task_case__bitrix_123.json"
            notion_path = Path(tmp) / "knowledge" / "task_cases" / "task_case__bitrix_123.notion.json"
            self.assertTrue(object_path.exists())
            self.assertTrue(notion_path.exists())
            data = json.loads(object_path.read_text(encoding="utf-8"))
            self.assertEqual(data["system"], "bitrix")

    def test_index_search_and_ask_use_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            results = repo.search("Bitrix checklist", limit=3)
            self.assertEqual(results[0].object_id, "task_case__bitrix_123")
            answer = repo.ask("Bitrix checklist")
            self.assertTrue(answer["sources"])
            self.assertIn("Найденные подтвержденные источники", answer["answer"])

    def test_derive_catalogs_writes_systems_features_and_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            result = repo.derive_catalogs()

            self.assertEqual(result.objects_count, 3)
            self.assertTrue((Path(tmp) / "knowledge" / "systems" / "system__bitrix.json").exists())
            self.assertTrue((Path(tmp) / "knowledge" / "features" / "feature__bitrix__checklists.json").exists())
            self.assertTrue((Path(tmp) / "knowledge" / "instructions" / "instruction__bitrix__checklists.json").exists())

            repo.build_index()
            results = repo.search("instruction checklist", limit=5)
            self.assertTrue(any(item.object_id == "instruction__bitrix__checklists" for item in results))

    def test_revision_proposal_is_prompt_workspace_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            proposal = repo.create_revision_proposal(
                object_id="task_case__bitrix_123",
                correction="Уточнить поведение demo для дублей.",
            )
            text = Path(proposal.proposal_path).read_text(encoding="utf-8")
            self.assertIn("proposal only", text)
            self.assertIn("discussion__loom123", text)

    def test_revision_can_be_approved_and_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            proposal = repo.create_revision_proposal(
                object_id="task_case__bitrix_123",
                correction="Уточнить demo.",
            )
            repo.set_revision_status(metadata_path=Path(proposal.metadata_path), status="approved")
            applied = repo.apply_revision(metadata_path=Path(proposal.metadata_path))
            self.assertEqual(applied.status, "applied")
            data = json.loads((Path(tmp) / "knowledge" / "task_cases" / "task_case__bitrix_123.json").read_text(encoding="utf-8"))
            self.assertEqual(data["revision_history"][0]["status"], "applied")

    def test_chunk_index_external_export_and_notion_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            chunks = repo.build_chunk_index()
            self.assertTrue((Path(tmp) / "indexes" / "knowledge_chunks.json").exists())
            self.assertGreaterEqual(chunks.objects_count, 1)

            export = repo.export_external_bundle(target="notebooklm")
            self.assertTrue((Path(tmp) / "exports" / "notebooklm.zip").exists())
            self.assertEqual(export.objects_count, 1)

            repo.derive_catalogs()
            profile_export = repo.export_external_bundle(target="agents", system="bitrix", object_type="instruction")
            self.assertTrue((Path(tmp) / "exports" / "agents__system_bitrix__type_instruction.zip").exists())
            self.assertEqual(profile_export.object_ids, ["instruction__bitrix__checklists"])

            notion = repo.notion_sync_plan(apply=True, env={})
            self.assertFalse(notion.ready)
            self.assertIn("NOTION_API_KEY", notion.missing_env)

            dry = repo.notion_sync_plan(
                apply=False,
                env={
                    "NOTION_API_KEY": "secret",
                    "NOTION_DATA_SOURCE_TASK_CASES": "ds-task",
                    "NOTION_DATA_SOURCE_SYSTEMS": "ds-systems",
                    "NOTION_DATA_SOURCE_FEATURES": "ds-features",
                    "NOTION_DATA_SOURCE_INSTRUCTIONS": "ds-instructions",
                },
            )
            self.assertTrue(dry.ready)
            self.assertEqual(dry.mode, "dry-run")

            repo.derive_catalogs()
            full_dry = repo.notion_sync_plan(
                apply=False,
                env={
                    "NOTION_API_KEY": "secret",
                    "NOTION_DATA_SOURCE_TASK_CASES": "ds-task",
                    "NOTION_DATA_SOURCE_SYSTEMS": "ds-systems",
                    "NOTION_DATA_SOURCE_FEATURES": "ds-features",
                    "NOTION_DATA_SOURCE_INSTRUCTIONS": "ds-instructions",
                },
            )
            self.assertTrue(full_dry.ready)
            self.assertEqual(len(full_dry.planned_pages), 4)

    def test_notion_import_proposal_detects_manual_markdown_edits(self) -> None:
        class FakeClient:
            def query_pages(self) -> list[dict]:
                return [{"id": "page1", "url": "https://notion.local/page1"}]

            def page_to_projection(self, page: dict, *, database: str) -> dict:
                return {
                    "database": database,
                    "page_id": page["id"],
                    "url": page["url"],
                    "properties": {"ID": "task_case__bitrix_123", "Title": "Bitrix checklist sync"},
                    "content_markdown": "# Functional Spec: Bitrix checklist sync\n\n## Requirements\n\n- Edited in Notion\n",
                }

        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            result = repo.notion_import_proposals(
                env={},
                database="Task Cases",
                clients={"Task Cases": FakeClient()},
            )
            self.assertTrue(result.ready)
            self.assertEqual(result.proposals_count, 1)
            self.assertTrue((Path(tmp) / "knowledge" / "drafts" / "notion_import").exists())
            proposal_text = Path(result.written_files[0]).read_text(encoding="utf-8")
            self.assertIn("Edited in Notion", proposal_text)
            metadata = json.loads(Path(result.written_files[1]).read_text(encoding="utf-8"))
            self.assertTrue(metadata["diff_hash"])

            repo.set_revision_status(metadata_path=Path(result.written_files[1]), status="rejected")
            second_result = repo.notion_import_proposals(
                env={},
                database="Task Cases",
                clients={"Task Cases": FakeClient()},
            )
            self.assertEqual(second_result.proposals_count, 0)
            self.assertTrue(any(item["action"] == "ignored_known_diff" for item in second_result.planned_pages))

    def test_apply_notion_import_updates_task_case_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            result = repo.notion_import_proposals(
                env={},
                database="Task Cases",
                clients={
                    "Task Cases": type(
                        "FakeClient",
                        (),
                        {
                            "query_pages": lambda self: [{"id": "page1", "url": "https://notion.local/page1"}],
                            "page_to_projection": lambda self, page, database: {
                                "database": database,
                                "page_id": page["id"],
                                "url": page["url"],
                                "properties": {"ID": "task_case__bitrix_123", "Title": "Bitrix checklist sync"},
                                "content_markdown": "# Functional Spec: Bitrix checklist sync\n\n## Requirements\n\n- Edited in Notion\n",
                            },
                        },
                    )()
                },
            )
            metadata_path = Path(result.written_files[1])
            repo.set_revision_status(metadata_path=metadata_path, status="approved")
            applied = repo.apply_notion_import(metadata_path=metadata_path)
            self.assertEqual(applied.status, "applied")
            data = json.loads((Path(tmp) / "knowledge" / "task_cases" / "task_case__bitrix_123.json").read_text(encoding="utf-8"))
            self.assertEqual(data["current_requirements"], ["Edited in Notion"])

    def test_generate_document_writes_grounded_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            generated = repo.generate_document(object_id="task_case__bitrix_123", kind="technical_spec")
            text = Path(generated.output_path).read_text(encoding="utf-8")
            self.assertIn("Technical Spec", text)
            self.assertIn("Create checklist groups", text)


if __name__ == "__main__":
    unittest.main()
