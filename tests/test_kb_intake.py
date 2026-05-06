from __future__ import annotations

import json
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.kb_intake import KnowledgeIntake, KnowledgeObject, KnowledgeSourceEvent
from meeting_digest_bot.models import MeetingRecord, PublicationRecord
from meeting_digest_bot.models import PublicationRegistrationRequest
from meeting_digest_bot.state_db import StateRepository


def meeting(title: str, tags: list[str] | None = None) -> MeetingRecord:
    return MeetingRecord(
        loom_video_id="loom123",
        source_url="https://www.loom.com/share/loom123",
        title=title,
        meeting_type="meeting",
        transcript_text="",
        artifacts={"tags": tags or []},
    )


def publication(title: str) -> PublicationRecord:
    return PublicationRecord(
        id=1,
        post_url="https://t.me/c/1/2",
        digest_type="meeting",
        loom_video_id="loom123",
        meeting_title=title,
        created_at="2026-05-05T00:00:00",
    )


class KnowledgeIntakeTests(unittest.TestCase):
    def test_task_discussion_is_candidate(self) -> None:
        self.assertTrue(
            KnowledgeIntake.is_knowledge_candidate(
                meeting=meeting("#task_discussion CRM checklist sync"),
                publication=publication("CRM checklist sync"),
            )
        )

    def test_task_demo_is_candidate_from_artifact_tags(self) -> None:
        self.assertTrue(
            KnowledgeIntake.is_knowledge_candidate(
                meeting=meeting("CRM checklist sync", tags=["#task_demo"]),
                publication=publication("CRM checklist sync"),
            )
        )

    def test_task_discussion_is_candidate_from_publication_payload_tags(self) -> None:
        record = publication("CRM checklist sync")
        record.payload_json["source_tags"] = ["#task_discussion"]
        self.assertTrue(
            KnowledgeIntake.is_knowledge_candidate(
                meeting=meeting("CRM checklist sync"),
                publication=record,
            )
        )

    def test_daily_is_excluded_even_with_task_tag(self) -> None:
        self.assertFalse(
            KnowledgeIntake.is_knowledge_candidate(
                meeting=meeting("#daily #task_discussion team plan"),
                publication=publication("team plan"),
            )
        )

    def test_object_bundle_contains_source_machine_and_prompt_workspaces(self) -> None:
        item = KnowledgeObject(
            object_id="task_case__bitrix_123",
            title="Bitrix checklist sync",
            system="bitrix",
            feature_area="checklists",
            source_tags=["#task_discussion", "#task_demo"],
            current_summary="Sync checklists from Loom task discussions.",
            current_requirements=["Create checklist groups"],
            acceptance_criteria=["Checklist items are deduped"],
            decisions=["Use Bitrix checklist API"],
            demo_feedback=["Demo confirmed dedupe behavior"],
            source_events=[
                KnowledgeSourceEvent(
                    event_id="discussion__loom123",
                    event_type="discussion",
                    title="Discussion",
                    loom_video_id="loom123",
                    loom_url="https://www.loom.com/share/loom123",
                    telegram_post_url="https://t.me/c/1/2",
                    raw_tags=["#task_discussion"],
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            KnowledgeIntake._write_object_bundle(output_dir=Path(tmp), item=item)
            object_dir = Path(tmp) / item.object_id

            self.assertTrue((object_dir / "source_bundle" / "01_overview.md").exists())
            self.assertTrue((object_dir / "source_bundle" / "07_sources.md").exists())
            self.assertTrue((object_dir / "machine_bundle" / "ai_context.json").exists())
            self.assertTrue((object_dir / "prompt_workspace" / "revise_knowledge_object.md").exists())
            self.assertTrue((object_dir / "prompt_workspace" / "object_context.md").exists())

            machine_payload = json.loads((object_dir / "machine_bundle" / "ai_context.json").read_text(encoding="utf-8"))
            self.assertFalse(machine_payload["contracts"]["direct_mutation_allowed"])
            self.assertEqual(machine_payload["contracts"]["conflict_priority"], ["demo", "discussion"])
            prompt = (object_dir / "prompt_workspace" / "generate_technical_spec.md").read_text(encoding="utf-8")
            self.assertIn("source event IDs", prompt)

    def test_backfill_source_tags_updates_old_publication_and_candidate_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = StateRepository(Path(tmp) / "state.db")
            state.register_publication(
                PublicationRegistrationRequest(
                    post_url="https://t.me/c/1/10",
                    loom_video_id="loom123",
                    meeting_title="Old post without explicit tags",
                )
            )
            fake_service = SimpleNamespace(
                state=state,
                aicallorder=SimpleNamespace(get_meeting=lambda _loom_id: meeting("#task_discussion CRM checklist sync")),
            )
            result = KnowledgeIntake(fake_service).backfill_source_tags()
            record = state.get_publication_by_post_url("https://t.me/c/1/10")

            self.assertEqual(result.updated, 1)
            self.assertEqual(record.payload_json["source_tags"], ["#task_discussion"])
            self.assertEqual(len(state.list_kb_candidates()), 1)


if __name__ == "__main__":
    unittest.main()
