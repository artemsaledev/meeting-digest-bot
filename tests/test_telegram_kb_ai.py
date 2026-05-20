from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from io import BytesIO
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


class FakeCorrectionClient:
    embeddings_model = "text-embedding-3-large"

    def complete_messages(self, messages: list[dict[str, str]], *, model: str | None = None, temperature: float = 0.1) -> str:
        return (
            '{"cleaned_query":"исправь знание: бонусы зависят от процента распределения по сумме заказа и товарному составу",'
            '"instruction_summary":"Бонусы нужно считать по проценту распределения, сумме заказа и товарному составу.",'
            '"replacements":[{"old":"Исправдания","new":"исправление","reason":"speech transcript typo"}],'
            '"notes":["очищено из голосовой транскрипции"],"confidence":"medium"}'
        )


class FakeCommandReplacementClient:
    embeddings_model = "text-embedding-3-large"

    def complete_messages(self, messages: list[dict[str, str]], *, model: str | None = None, temperature: float = 0.1) -> str:
        return (
            '{"cleaned_query":"Обнови знание по проверенной инструкции Google Doc",'
            '"instruction_summary":"Нужно обновить правила работы функциональности по содержанию проверенной инструкции из Google Doc. Источник должен быть использован как основание для связанных task cases, features, systems и instructions.",'
            '"replacements":[{"old":"правка","new":"обнови знание","reason":"service words, should be ignored"}],'
            '"notes":[],"confidence":"medium"}'
        )


class FakeHTTPResponse:
    def __init__(self, text: str = "", status_code: int = 200, payload: dict | None = None) -> None:
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


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

    def test_kb_export_command_sends_zip_without_action_keyboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KNOWLEDGE_REPO_PATH": tmp}, clear=False):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.derive_catalogs()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 111,
                        "text": "@LLMeets_bot kb export notebooklm",
                        "chat": {"id": 123},
                        "from": {"id": 7},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "export_bundle")
            self.assertTrue(bot.documents)
            self.assertTrue(bot.documents[0]["path"].endswith(".zip"))
            self.assertFalse(bot.messages)

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

    def test_unrecognized_direct_voice_does_not_queue_notebooklm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "KNOWLEDGE_REPO_PATH": tmp,
                "KNOWLEDGE_NOTEBOOKLM_EXPORTS_ROOT": str(Path(tmp) / "notebooklm"),
                "KNOWLEDGE_RAG_API_KEY": "",
                "OPENAI_API_KEY": "",
                "LLM_API_KEY": "",
            },
            clear=False,
        ):
            bot = FakeTelegramBot()
            with patch.object(bot, "_transcribe_telegram_audio", return_value=""):
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 13,
                            "chat": {"id": 123},
                            "voice": {"file_id": "voice-empty"},
                        }
                    }
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.payload["intent"], "voice_transcription_failed")
            self.assertFalse(result.payload["notebooklm_queued"])
            self.assertTrue(bot.messages)
            self.assertFalse((Path(tmp) / "notebooklm").exists())

    def test_empty_notebooklm_followup_is_not_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_NOTEBOOKLM_EXPORTS_ROOT": str(Path(tmp) / "notebooklm")},
            clear=False,
        ):
            path = TelegramBotFacade._queue_notebooklm_followup(
                query="",
                answer="answer",
                sources=[],
                answer_mode="general",
                telegram_chat_id=123,
            )

            self.assertEqual(path, "")
            self.assertFalse((Path(tmp) / "notebooklm").exists())

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
            self.assertIn("Показать эффект", keyboard_text)
            self.assertIn("Применить", keyboard_text)
            self.assertIn("Отклонить", keyboard_text)

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
            self.assertIn("правки на проверку", result.text)
            self.assertNotIn("kb approve", result.text)
            keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
            self.assertIn("Показать эффект", keyboard_text)
            self.assertIn("Применить", keyboard_text)

    def test_revision_term_correction_targets_related_objects_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            item = knowledge_object().model_copy(
                update={
                    "current_summary": "Сборочный документ может быть в системе ВМС или ИГРА.",
                    "current_requirements": ["Поддержать сборочный документ для ВМС и ИГРА."],
                    "feature_area": "assembly",
                }
            )
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([item])
            repo.derive_catalogs()
            repo.build_index()
            repo.build_chunk_index()

            bot = FakeTelegramBot()
            result = bot.process_update(
                {
                    "message": {
                        "message_id": 231,
                        "text": '@LLMeets_bot исправь знание: сборочный документ, может быть в системе 1С, WMS (замени аббревиатуру вместо "ВМС"), CRM (замени аббревиатуру вместо "ИГРА")',
                        "chat": {"id": 123},
                        "from": {"id": 7},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertGreater(len(result.payload["proposals"]), 1)
            self.assertIn("Эффект правки", result.text)
            self.assertIn("`ВМС` будет заменено на `WMS`", result.text)
            self.assertIn("`ИГРА` будет заменено на `CRM`", result.text)
            keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
            self.assertIn("Применить все", keyboard_text)
            self.assertIn("Отклонить все", keyboard_text)
            self.assertNotIn("1 Показать эффект", keyboard_text)
            self.assertNotIn("1 Применить", keyboard_text)

            applied = bot.process_update(
                {
                    "callback_query": {
                        "id": "cb-apply-all",
                        "data": "kb:proposal:apply_all:0",
                        "from": {"id": 7},
                        "message": {"chat": {"id": 123}, "text": result.text},
                    }
                }
            )
            self.assertTrue(applied.ok)
            self.assertIn("Применил правки", applied.text)
            updated = (Path(tmp) / "knowledge" / "task_cases" / f"{item.object_id}.json").read_text(encoding="utf-8")
            self.assertIn("WMS", updated)
            self.assertIn("CRM", updated)

    def test_revision_voice_like_correction_is_normalized_before_proposal(self) -> None:
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
            with patch.object(
                bot,
                "_answer_from_knowledge",
                return_value={"answer": "ok", "sources": [{"object_id": knowledge_object().object_id, "title": "Bitrix checklist", "snippets": ["bonus typo"]}]},
            ), patch("meeting_digest_bot.telegram_bot.client_from_env", return_value=FakeCorrectionClient()):
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 232,
                            "text": "@LLMeets_bot Исправдания бонусы зависят от процента распределения по сумме заказа",
                            "chat": {"id": 123},
                            "from": {"id": 7},
                        }
                    }
                )

            self.assertTrue(result.ok)
            self.assertIn("Как я понял инструкцию", result.text)
            self.assertIn("исправь знание: бонусы зависят", result.payload["query"])
            proposal = result.payload["proposal"]
            self.assertIn("исправь знание: бонусы зависят", proposal["correction"])
            self.assertEqual(result.payload["normalization"]["confidence"], "medium")

    def test_revision_uses_task_extractor_notebooklm_bundle_as_trusted_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"KNOWLEDGE_REPO_PATH": tmp, "KNOWLEDGE_RAG_API_KEY": "", "OPENAI_API_KEY": "", "LLM_API_KEY": ""},
            clear=False,
        ):
            repo = KnowledgeRepository(Path(tmp))
            repo.upsert_objects([knowledge_object()])
            repo.build_index()
            repo.build_chunk_index()
            buffer = BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("source_bundle/01_instruction.md", "# Проверенная инструкция\nБонусы распределяются пропорционально.")

            bot = FakeTelegramBot()
            with patch.object(bot, "_download_telegram_file", return_value=buffer.getvalue()), patch.object(
                bot,
                "_answer_from_knowledge",
                return_value={"answer": "ok", "sources": [{"object_id": knowledge_object().object_id, "title": "Bitrix checklist", "snippets": []}]},
            ), patch("meeting_digest_bot.telegram_bot.client_from_env", return_value=FakeCorrectionClient()):
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 233,
                            "text": "@LLMeets_bot правка: исправь знание, учти проверенную инструкцию",
                            "chat": {"id": 123},
                            "from": {"id": 7},
                            "document": {"file_id": "bundle1", "file_name": "notebooklm.zip"},
                        }
                    }
                )

            self.assertTrue(result.ok)
            trusted_sources = result.payload["normalization"]["trusted_sources"]
            self.assertEqual(trusted_sources[0]["type"], "notebooklm_bundle")
            self.assertEqual(trusted_sources[0]["status"], "fetched")
            self.assertIn("Проверенная инструкция", trusted_sources[0]["text"])
            metadata = KnowledgeRepository(Path(tmp))._read_json(Path(result.payload["proposal"]["metadata_path"]))
            self.assertEqual(metadata["trusted_sources"][0]["type"], "notebooklm_bundle")

    def test_revision_fetches_google_doc_link_as_trusted_source(self) -> None:
        bot = FakeTelegramBot()
        with patch("meeting_digest_bot.telegram_bot.requests.get", return_value=FakeHTTPResponse("Проверенная инструкция из Google Doc")) as mocked_get:
            sources = bot._extract_trusted_revision_sources("исправь по https://docs.google.com/document/d/doc123/edit")

        self.assertEqual(sources[0]["type"], "google_doc")
        self.assertEqual(sources[0]["status"], "fetched")
        self.assertIn("Проверенная инструкция", sources[0]["text"])
        self.assertIn("/document/d/doc123/export?format=txt", mocked_get.call_args.args[0])

    def test_revision_preview_shows_understanding_and_filters_command_replacements(self) -> None:
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
            with patch.object(
                bot,
                "_answer_from_knowledge",
                return_value={"answer": "ok", "sources": [{"object_id": knowledge_object().object_id, "title": "Bitrix checklist", "snippets": ["bonus typo"]}]},
            ), patch(
                "meeting_digest_bot.telegram_bot.client_from_env",
                return_value=FakeCommandReplacementClient(),
            ), patch(
                "meeting_digest_bot.telegram_bot.requests.get",
                return_value=FakeHTTPResponse("Проверенная инструкция: обновить правило по функциональности."),
            ):
                result = bot.process_update(
                    {
                        "message": {
                            "message_id": 234,
                            "text": "@LLMeets_bot правка: Обнови знание по проверенной инструкции https://docs.google.com/document/d/doc123/edit",
                            "chat": {"id": 123},
                            "from": {"id": 7},
                        }
                    }
                )

            self.assertTrue(result.ok)
            self.assertIn("Как я понял инструкцию", result.text)
            self.assertIn("проверенной инструкции из Google Doc", result.text)
            self.assertNotIn("`правка` будет заменено", result.text)
            self.assertEqual(result.payload["replacements"], [])
            metadata = KnowledgeRepository(Path(tmp))._read_json(Path(result.payload["proposal"]["metadata_path"]))
            self.assertIn("проверенной инструкции из Google Doc", metadata["instruction_summary"])

    def test_revision_replacement_filter_ignores_command_words(self) -> None:
        bot = FakeTelegramBot()
        replacements = bot._filter_revision_replacements([("правка", "обнови знание"), ("ВМС", "WMS"), ("ИГРА", "CRM")])

        self.assertEqual(replacements, [("ВМС", "WMS"), ("ИГРА", "CRM")])

    def test_mention_health_routes_to_knowledge_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"KNOWLEDGE_REPO_PATH": tmp}, clear=False):
            KnowledgeRepository(Path(tmp)).init()

            bot = FakeTelegramBot()
            result = bot.process_update({"message": {"message_id": 24, "text": "@LLMeets_bot health", "chat": {"id": 123}, "from": {"id": 7}}})

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "health")
            self.assertIn("Статус базы знаний", result.text)

    def test_plain_mention_question_routes_to_knowledge_ask(self) -> None:
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
                        "message_id": 25,
                        "text": "@LLMeets_bot что обсуждали в последней встрече по демо процесса брифа?",
                        "chat": {"id": 123},
                        "from": {"id": 7},
                    }
                }
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.payload["intent"], "ask")
            self.assertNotIn("Не удалось распознать ссылку", result.text)

    def test_rag_answer_shows_notebooklm_queue_status_and_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notebook_root = Path(tmp) / "exports" / "task_extractor"
            session_root = notebook_root / "company-knowledge"
            (session_root / "machine_bundle").mkdir(parents=True)
            (session_root / "prompt_workspace").mkdir()
            (session_root / "machine_bundle" / "handoff_manifest.json").write_text("{}", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "KNOWLEDGE_REPO_PATH": tmp,
                    "KNOWLEDGE_RAG_API_KEY": "",
                    "OPENAI_API_KEY": "",
                    "LLM_API_KEY": "",
                    "KNOWLEDGE_NOTEBOOKLM_EXPORTS_ROOT": str(notebook_root),
                    "KNOWLEDGE_NOTEBOOKLM_SESSION_ID": "company-knowledge",
                },
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
                            "message_id": 26,
                            "text": "@LLMeets_bot Bitrix checklist",
                            "chat": {"id": 123},
                            "from": {"id": 7},
                        }
                    }
                )

                self.assertTrue(result.ok)
                self.assertIn("NotebookLM", result.text)
                self.assertTrue(result.payload["notebooklm_prompt_path"])
                keyboard_text = [button["text"] for row in bot.messages[0]["reply_markup"]["inline_keyboard"] for button in row]
                self.assertIn("NotebookLM проверка", keyboard_text)

                callback = bot.process_update(
                    {
                        "callback_query": {
                            "id": "cb3",
                            "data": "kb:notebooklm",
                            "from": {"id": 7},
                            "message": {"chat": {"id": 123}, "text": result.text},
                        }
                    }
                )
                self.assertTrue(callback.ok)
                self.assertEqual(callback.payload["intent"], "notebooklm_check")
                self.assertIn("фоновую очередь", callback.text)


if __name__ == "__main__":
    unittest.main()
