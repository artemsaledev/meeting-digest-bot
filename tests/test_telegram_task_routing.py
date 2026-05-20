from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meeting_digest_bot.knowledge_repo import KnowledgeRepository
from meeting_digest_bot.models import SyncResult, TaskExtractorResult
from meeting_digest_bot.telegram_bot import TelegramBotFacade
from tests.test_knowledge_repo import knowledge_object


class _FakeTaskExtractor:
    def __init__(self) -> None:
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return TaskExtractorResult(action=request.action.value, text=f"task extractor {request.action.value}")


class _FakeService:
    def __init__(self) -> None:
        self.task_extractor = _FakeTaskExtractor()
        self.sync_post_requests = []

    def sync_post(self, request):
        self.sync_post_requests.append(request)
        return SyncResult(
            action=request.action.value,
            task_id=123,
            task_url="https://example.test/tasks/123",
            title="Meeting task",
            details={"post_url": request.post_url},
        )


class _FakeBot(TelegramBotFacade):
    def __init__(self) -> None:
        self.fake_service = _FakeService()
        super().__init__(service=self.fake_service, token="test-token")  # type: ignore[arg-type]
        self.messages = []

    def send_message(self, chat_id, text, *, reply_to_message_id=None, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
        }
        self.messages.append(payload)
        return {"ok": True, "result": payload}


class TelegramTaskRoutingTests(unittest.TestCase):
    def test_llmeets_meeting_commands_route_to_post_sync_not_kb(self) -> None:
        cases = [
            ("@LLMeets_bot preview", "preview", None),
            ("@LLMeets_bot предпросмотр", "preview", None),
            ("@LLMeets_bot показать", "preview", None),
            ("@LLMeets_bot проверить", "preview", None),
            ("@LLMeets_bot create", "create", None),
            ("@LLMeets_bot создать", "create", None),
            ("@LLMeets_bot comment 147721", "append_comment", 147721),
            ("@LLMeets_bot коммент 147721", "append_comment", 147721),
            ("@LLMeets_bot комментарий 147721", "append_comment", 147721),
            ("@LLMeets_bot checklist 147721", "append_checklists", 147721),
            ("@LLMeets_bot чеклист 147721", "append_checklists", 147721),
            ("@LLMeets_bot чек-лист 147721", "append_checklists", 147721),
            ("@LLMeets_bot update 147721", "update_description", 147721),
            ("@LLMeets_bot обновить 147721", "update_description", 147721),
            ("@LLMeets_bot заменить 147721", "update_description", 147721),
        ]
        for index, (text, expected_action, expected_task_id) in enumerate(cases, start=1):
            with self.subTest(text=text):
                bot = _FakeBot()
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 100 + index,
                            "text": text,
                            "chat": {"id": -100123},
                            "from": {"id": 7},
                            "reply_to_message": {
                                "message_id": 90 + index,
                                "text": "Meeting: #task_demo 20.05 Loom https://loom.com/share/abc",
                            },
                        }
                    }
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.payload["action"], expected_action)
                self.assertNotIn("intent", result.payload)
                self.assertEqual(bot.fake_service.task_extractor.requests, [])
                self.assertEqual(len(bot.fake_service.sync_post_requests), 1)
                self.assertEqual(bot.fake_service.sync_post_requests[0].task_id, expected_task_id)
                self.assertEqual(bot.fake_service.sync_post_requests[0].post_url, f"https://t.me/c/123/{90 + index}")

    def test_llmeets_russian_comment_reply_to_meeting_routes_to_post_sync(self) -> None:
        bot = _FakeBot()
        result = bot.process_update(
            {
                "message": {
                    "message_id": 103,
                    "text": "@LLMeets_bot комментарий 147721",
                    "chat": {"id": -100123},
                    "from": {"id": 7},
                    "reply_to_message": {
                        "message_id": 91,
                        "text": "Встреча: #task_demo 20.05 Вопросы по базе знаний https://loom.com/share/abc",
                    },
                }
            }
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "append_comment")
        self.assertEqual(bot.fake_service.task_extractor.requests, [])
        self.assertEqual(len(bot.fake_service.sync_post_requests), 1)
        self.assertEqual(bot.fake_service.sync_post_requests[0].task_id, 147721)
        self.assertEqual(bot.fake_service.sync_post_requests[0].post_url, "https://t.me/c/123/91")

    def test_llmeets_kb_question_does_not_route_to_post_sync_or_task_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()

            bot = _FakeBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 104,
                        "text": "@LLMeets_bot как работает общий платеж Payments Pro?",
                        "chat": {"id": -100777},
                        "from": {"id": 7},
                    }
                }
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload.get("intent"), "ask")
        self.assertEqual(bot.fake_service.task_extractor.requests, [])
        self.assertEqual(bot.fake_service.sync_post_requests, [])

    def test_kb_chat_allowlist_prevents_kb_intercept_in_meeting_chat(self) -> None:
        bot = _FakeBot()
        with patch.dict("os.environ", {"KNOWLEDGE_ALLOWED_CHAT_IDS": "-100777"}, clear=False):
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 105,
                        "text": "@LLMeets_bot как работает общий платеж Payments Pro?",
                        "chat": {"id": -100123},
                        "from": {"id": 7},
                    }
                }
            )

        self.assertFalse(result.ok)
        self.assertNotEqual(result.payload.get("intent"), "ask")
        self.assertEqual(bot.fake_service.task_extractor.requests, [])
        self.assertEqual(bot.fake_service.sync_post_requests, [])

    def test_meeting_chat_allowlist_prevents_meeting_sync_in_kb_chat(self) -> None:
        bot = _FakeBot()
        with patch.dict("os.environ", {"MEETING_ALLOWED_CHAT_IDS": "-100123"}, clear=False):
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 106,
                        "text": "@LLMeets_bot создать",
                        "chat": {"id": -100777},
                        "from": {"id": 7},
                        "reply_to_message": {
                            "message_id": 96,
                            "text": "Meeting: #task_demo 20.05 Loom https://loom.com/share/abc",
                        },
                    }
                }
            )

        self.assertFalse(result.ok)
        self.assertIn("не включена", result.text)
        self.assertEqual(bot.fake_service.task_extractor.requests, [])
        self.assertEqual(bot.fake_service.sync_post_requests, [])

    def test_llmeets_create_without_meeting_context_does_not_route_to_task_extractor(self) -> None:
        bot = _FakeBot()
        result = bot.process_update(
            {
                "message": {
                    "message_id": 101,
                    "text": "@LLMeets_bot create",
                    "chat": {"id": -100},
                    "from": {"id": 7},
                }
            }
        )

        self.assertFalse(result.ok)
        self.assertNotEqual(result.payload.get("intent"), "task_extractor")
        self.assertEqual(bot.fake_service.task_extractor.requests, [])
        self.assertEqual(bot.fake_service.sync_post_requests, [])

    def test_task_extractor_bot_still_uses_task_extractor(self) -> None:
        bot = _FakeBot()
        result = bot.process_update(
            {
                "message": {
                    "message_id": 102,
                    "text": "@Task_Extractor_Bot create",
                    "chat": {"id": -100},
                    "from": {"id": 7},
                }
            }
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "create")
        self.assertEqual(bot.fake_service.task_extractor.requests[0].action.value, "create")
        self.assertEqual(bot.fake_service.sync_post_requests, [])


if __name__ == "__main__":
    unittest.main()
