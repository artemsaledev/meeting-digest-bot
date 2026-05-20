from __future__ import annotations

import unittest

from meeting_digest_bot.models import TaskExtractorResult
from meeting_digest_bot.telegram_bot import TelegramBotFacade


class _FakeTaskExtractor:
    def __init__(self) -> None:
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return TaskExtractorResult(action=request.action.value, text=f"task extractor {request.action.value}")


class _FakeService:
    def __init__(self) -> None:
        self.task_extractor = _FakeTaskExtractor()


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
    def test_llmeets_create_reply_to_meeting_routes_to_task_extractor(self) -> None:
        bot = _FakeBot()
        result = bot.process_update(
            {
                "message": {
                    "message_id": 100,
                    "text": "@LLMeets_bot создать",
                    "chat": {"id": -100},
                    "from": {"id": 7},
                    "reply_to_message": {
                        "message_id": 90,
                        "text": "Встреча: #daily 20.05 План работ по автоматизации Loom https://loom.com/share/abc",
                    },
                }
            }
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.payload["action"], "create")
        self.assertEqual(bot.fake_service.task_extractor.requests[0].action.value, "create")
        self.assertEqual(bot.fake_service.task_extractor.requests[0].reply_text.startswith("Встреча:"), True)
        self.assertEqual(bot.messages[0]["reply_to_message_id"], 100)

    def test_llmeets_create_without_meeting_context_does_not_route_to_task_extractor(self) -> None:
        bot = _FakeBot()
        result = bot.process_update(
            {
                "message": {
                    "message_id": 101,
                    "text": "@LLMeets_bot создать",
                    "chat": {"id": -100},
                    "from": {"id": 7},
                }
            }
        )

        self.assertNotEqual(result.payload.get("intent"), "task_extractor")
        self.assertEqual(bot.fake_service.task_extractor.requests, [])


if __name__ == "__main__":
    unittest.main()
