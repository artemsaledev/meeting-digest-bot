from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.notebooklm_agent import NotebookLMAgent


class NotebookLMAgentTests(unittest.TestCase):
    def test_prepare_package_validates_sources_and_writes_run_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "exports" / "task_extractor" / "session1"
            (root / "source_bundle").mkdir(parents=True)
            (root / "prompt_workspace").mkdir()
            (root / "machine_bundle").mkdir()
            (root / "source_bundle" / "00_readme.md").write_text("# Readme\n", encoding="utf-8")
            (root / "source_bundle" / "01_task_context.md").write_text("# Context\n", encoding="utf-8")
            (root / "prompt_workspace" / "prompt_for_notebooklm.md").write_text("Prompt\n", encoding="utf-8")
            (root / "machine_bundle" / "handoff_manifest.json").write_text(
                json.dumps(
                    {
                        "session_id": "session1",
                        "notebooklm_project_title": "Task 123 - Test",
                        "source_bundle_files": [
                            "source_bundle/00_readme.md",
                            "source_bundle/01_task_context.md",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            agent = NotebookLMAgent(exports_root=Path(tmp) / "exports" / "task_extractor")
            package = agent.prepare_package(session_id="session1")

            self.assertEqual(package.title, "Task 123 - Test")
            self.assertEqual(len(package.source_files), 2)
            run_path = root / "machine_bundle" / "notebooklm_run.json"
            self.assertTrue(run_path.exists())
            run = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "prepared")
            self.assertEqual(run["source_count"], 2)

    def test_prepare_package_fails_on_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "exports" / "task_extractor" / "session1"
            (root / "prompt_workspace").mkdir(parents=True)
            (root / "machine_bundle").mkdir()
            (root / "prompt_workspace" / "prompt_for_notebooklm.md").write_text("Prompt\n", encoding="utf-8")
            (root / "machine_bundle" / "handoff_manifest.json").write_text(
                json.dumps({"source_bundle_files": ["source_bundle/missing.md"]}),
                encoding="utf-8",
            )

            agent = NotebookLMAgent(exports_root=Path(tmp) / "exports" / "task_extractor")
            with self.assertRaises(FileNotFoundError):
                agent.prepare_package(session_id="session1")

    def test_prepare_knowledge_package_and_queue_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp) / "company-knowledge"
            export = knowledge / "exports" / "notebooklm"
            source = export / "object1" / "source.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Object 1\n", encoding="utf-8")
            (export / "manifest.json").write_text(
                json.dumps(
                    {
                        "objects": [
                            {
                                "object_id": "object__one",
                                "title": "Object One",
                                "path": str(source),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            agent = NotebookLMAgent(exports_root=Path(tmp) / "exports" / "knowledge_notebooklm")
            package = agent.prepare_knowledge_package(knowledge_dir=knowledge, session_id="company-knowledge")
            self.assertEqual(package.session_id, "company-knowledge")
            self.assertEqual(package.title, "Company Knowledge Base")
            self.assertGreaterEqual(len(package.source_files), 2)
            manifest = json.loads(package.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "company_knowledge")

            prompt_path = agent.queue_prompt(session_id="company-knowledge", prompt="Проверь Payments Pro", kind="rag_followup")
            self.assertTrue(prompt_path.exists())
            self.assertIn("Проверь Payments Pro", prompt_path.read_text(encoding="utf-8"))

            manifest["notebooklm_project_url"] = "https://notebooklm.google.com/notebook/abc"
            manifest["status"] = "notebook_created"
            package.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            package_again = agent.prepare_knowledge_package(knowledge_dir=knowledge, session_id="company-knowledge")
            manifest_again = json.loads(package_again.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest_again["notebooklm_project_url"], "https://notebooklm.google.com/notebook/abc")
            self.assertEqual(manifest_again["status"], "notebook_created")
            self.assertTrue(prompt_path.exists())


if __name__ == "__main__":
    unittest.main()
