from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meeting_digest_bot.knowledge_repo import KnowledgeRepository
from meeting_digest_bot.telegram_bot import TelegramBotFacade
from tests.test_knowledge_repo import knowledge_object


class FakeTelegramBot(TelegramBotFacade):
    def __init__(self) -> None:
        super().__init__(service=object(), token="test-token")  # type: ignore[arg-type]
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.callbacks: list[str] = []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
        }
        self.messages.append(payload)
        return {"ok": True, "result": payload}

    def send_document(self, chat_id: int | str, path: str, *, caption: str = "") -> dict:
        payload = {"chat_id": chat_id, "path": path, "caption": caption}
        self.documents.append(payload)
        return {"ok": True, "result": payload}

    def _answer_callback_query(self, callback_query_id: str) -> None:
        self.callbacks.append(callback_query_id)


class TelegramKnowledgeAiTests(unittest.TestCase):
    def test_natural_mention_runs_kb_ai_with_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 10,
                        "text": "@LLMeets_bot как работает база знаний по Bitrix checklist?",
                        "chat": {"id": 123},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "ask")
            self.assertTrue(bot.messages)
            self.assertEqual(bot.messages[0]["reply_to_message_id"], 10)
            self.assertIn("inline_keyboard", bot.messages[0]["reply_markup"])

    def test_kb_instruction_uses_instruction_answer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            result = FakeTelegramBot().process_update(
                {
                    "message": {
                        "text": "kb instruction Bitrix checklist",
                        "chat": {},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["answer_mode"], "user_instruction")

    def test_callback_uses_original_replied_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KNOWLEDGE_REPO_PATH": tmp}, clear=False):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "callback_query": {
                        "id": "cb1",
                        "data": "kb:spec",
                        "message": {
                            "chat": {"id": 123},
                            "reply_to_message": {"text": "Сформируй ТЗ по Bitrix checklist"},
                        },
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["answer_mode"], "technical_spec")
            self.assertEqual(bot.callbacks, ["cb1"])
            self.assertTrue(bot.messages)

    def test_export_intent_sends_zip_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KNOWLEDGE_REPO_PATH": tmp}, clear=False):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.derive_catalogs()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 11,
                        "text": "@LLMeets_bot собери export bundle для NotebookLM",
                        "chat": {"id": 123},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "export_bundle")
            self.assertTrue(bot.documents)
            self.assertTrue(bot.documents[0]["path"].endswith(".zip"))

    def test_mention_only_reply_to_voice_uses_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            with patch.object(bot, "_transcribe_telegram_audio", return_value="kb? Bitrix checklist"):
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 12,
                            "text": "@LLMeets_bot",
                            "chat": {"id": 123},
                            "reply_to_message": {"voice": {"file_id": "voice1"}},
                        }
                    }
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "ask")
            self.assertTrue(bot.messages)

    def test_kb_menu_uses_russian_buttons(self) -> None:
        bot = FakeTelegramBot()
        result = bot.process_update({"message": {"message_id": 20, "text": "@LLMeets_bot kb", "chat": {"id": 123}, "from": {"id": 7}}})

        self.assertTrue(result.ok)
        self.assertIn("База знаний", result.text)
        self.assertNotIn("kb health |", result.text)
        keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn("Спросить", keyboard_text)
        self.assertIn("Инструкция", keyboard_text)
        self.assertIn("ТЗ", keyboard_text)
        self.assertIn("Правки", keyboard_text)

    def test_callback_instruction_uses_last_answer_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            bot.process_update(
                {
                    "message": {
                        "message_id": 21,
                        "text": "@LLMeets_bot ask Bitrix checklist",
                        "chat": {"id": 123},
                        "from": {"id": 7},
                    }
                }
            )
            result = bot.process_update(
                {
                    "callback_query": {
                        "id": "cb2",
                        "data": "kb:instruction",
                        "from": {"id": 7},
                        "message": {"chat": {"id": 123}, "text": "Ответ бота по базе знаний"},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["answer_mode"], "user_instruction")
            self.assertEqual(result.payload["query"], "Bitrix checklist")

    def test_proposals_are_reviewed_with_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KNOWLEDGE_REPO_PATH": tmp}, clear=False):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.create_revision_proposal(object_id=knowledge_object().object_id, correction="исправь знание")

            bot = FakeTelegramBot()
            result = bot.process_update({"message": {"message_id": 22, "text": "kb proposals", "chat": {"id": 123}, "from": {"id": 7}}})

            self.assertTrue(result.ok)
            self.assertIn("Правки на проверке", result.text)
            keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
            self.assertIn("1 Показать", keyboard_text)
            self.assertIn("1 Принять", keyboard_text)
            self.assertIn("1 Применить", keyboard_text)
            self.assertIn("1 Отклонить", keyboard_text)

    def test_revision_creation_returns_buttons_not_manual_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 23,
                        "text": "@LLMeets_bot исправь знание: checklist работает иначе",
                        "chat": {"id": 123},
                        "from": {"id": 7},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertIn("черновик правки", result.text)
            self.assertNotIn("kb approve", result.text)
            keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
            self.assertIn("Показать diff", keyboard_text)
            self.assertIn("Применить", keyboard_text)


if __name__ == "__main__":
    unittest.main()
