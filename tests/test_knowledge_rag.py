from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_digest_bot.knowledge_rag import KnowledgeVectorStore
from tests.test_knowledge_repo import knowledge_object
from meeting_digest_bot.knowledge_repo import KnowledgeRepository


class FakeAIClient:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lower = text.casefold()
            vectors.append(
                [
                    1.0 if "bitrix" in lower else 0.0,
                    1.0 if "checklist" in lower or "checklists" in lower else 0.0,
                    1.0 if "duplicate" in lower else 0.0,
                ]
            )
        return vectors

    def answer(self, *, query: str, contexts: list[dict], model: str | None = None, answer_mode: str = "general") -> str:
        return f"{answer_mode} answer for {query}: {contexts[0]['object_id']} / {contexts[0]['chunk_id']}"


class KnowledgeRagTests(unittest.TestCase):
    def test_vector_store_build_search_and_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = KnowledgeRepository(root)
            repo.upsert_objects([knowledge_object()])
            repo.build_chunk_index()

            store = KnowledgeVectorStore(root)
            client = FakeAIClient()
            result = store.build(client=client)
            self.assertTrue(result["ready"])
            self.assertGreaterEqual(result["embedded_count"], 1)
            self.assertTrue((root / "indexes" / "knowledge_vectors.sqlite").exists())

            second = store.build(client=client)
            self.assertEqual(second["embedded_count"], 0)
            self.assertGreaterEqual(second["reused_count"], 1)

            results = store.search("Bitrix checklist duplicate", client=client, limit=2)
            self.assertTrue(results)
            self.assertEqual(results[0]["object_id"], "task_case__bitrix_123")
            self.assertIn("score", results[0])
            self.assertIn("vector_score", results[0])
            self.assertIn("lexical_score", results[0])

            filtered = store.search("Bitrix checklist duplicate", client=client, limit=2, object_type="task_case")
            self.assertTrue(filtered)
            self.assertTrue(all(item["object_type"] == "task_case" for item in filtered))

            answer = store.answer(
                "How does checklist sync work?",
                embedding_client=client,
                chat_client=client,
                answer_mode="technical_spec",
            )
            self.assertEqual(answer["mode"], "technical_spec")
            self.assertTrue(answer["sources"])
            self.assertIn("task_case__bitrix_123", answer["answer"])
            self.assertIn("technical_spec answer", answer["answer"])
            self.assertGreater(store.usage_stats()["events"], 0)

            refused = store.answer(
                "duplicate",
                embedding_client=client,
                chat_client=client,
                min_score=99.0,
            )
            self.assertEqual(refused["confidence"], "low")
            self.assertIn("below the configured threshold", refused["answer"])


if __name__ == "__main__":
    unittest.main()
