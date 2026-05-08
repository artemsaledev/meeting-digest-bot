from __future__ import annotations

import unittest

from meeting_digest_bot.daily_pm_llm import DailyPMChecklistLLM
from meeting_digest_bot.people import PeopleDirectory


class DailyPMChecklistLLMTests(unittest.TestCase):
    def test_daily_pm_analysis_dedupes_cross_sections_and_limits(self) -> None:
        analysis = {
            "pm_checklist": [
                "Проконтролировать проведение демо по табелю с Милей",
                "Проконтролировать проведение демо по табелю с Милей",
                "Собрать материалы и подготовить недельный дайджест с Эмилем Смолиным",
            ],
            "needs_verification": [
                "Проконтролировать проведение демо по табелю с Милей",
                "Проверить запись демо по табелям",
            ],
            "dont_lose_today": [
                "Собрать материалы и подготовить недельный дайджест с Эмилем Смолиным",
                "Не потерять запись демо по табелям",
            ],
        }

        DailyPMChecklistLLM._dedupe_cross_sections(analysis)

        self.assertEqual(
            analysis["pm_checklist"],
            [
                "Проконтролировать проведение демо по табелю с Милей",
                "Собрать материалы и подготовить недельный дайджест с Эмилем Смолиным",
            ],
        )
        self.assertEqual(analysis["needs_verification"], ["Проверить запись демо по табелям"])
        self.assertEqual(analysis["dont_lose_today"], [])

    def test_daily_pm_analysis_limits_pm_sections(self) -> None:
        analysis = {
            "focus_of_day": [f"focus {index}" for index in range(20)],
            "people_plan": [{"person": "x", "task": str(index)} for index in range(20)],
            "pm_checklist": [f"pm {index}" for index in range(20)],
            "dependencies": [f"dep {index}" for index in range(20)],
            "needs_verification": [f"verify {index}" for index in range(20)],
            "in_progress": [f"progress {index}" for index in range(20)],
            "blockers_and_risks": [f"risk {index}" for index in range(20)],
            "dont_lose_today": [f"lose {index}" for index in range(20)],
            "source_conflicts": [f"conflict {index}" for index in range(20)],
        }

        DailyPMChecklistLLM._limit_analysis_sections(analysis)

        self.assertEqual(len(analysis["pm_checklist"]), 8)
        self.assertEqual(len(analysis["needs_verification"]), 5)
        self.assertEqual(len(analysis["dont_lose_today"]), 4)

    def test_daily_pm_compacts_people_plan_to_assignable_commitments(self) -> None:
        llm = DailyPMChecklistLLM.__new__(DailyPMChecklistLLM)
        llm.people = PeopleDirectory.from_file()
        analysis = {
            "people_plan": [
                {
                    "person": "Ваня",
                    "task": "Ну проверить и исправить проставление направлений при разделении заказов",
                    "status": "in_progress",
                    "dependency": "",
                    "comment": "разговорный контекст",
                },
                {
                    "person": "Иван Карповец",
                    "task": "Проверить и исправить проставление направлений при разделении заказов",
                    "status": "in_progress",
                    "dependency": "",
                    "comment": "дубль",
                },
                {
                    "person": "Мили",
                    "task": "Провести демо по табелю",
                    "status": "todo",
                    "dependency": "",
                    "comment": "",
                },
                {
                    "person": "Артем",
                    "task": "Формат по дейли, каждый озвучивал",
                    "status": "todo",
                    "dependency": "",
                    "comment": "",
                },
            ],
            "pm_checklist": [],
        }

        llm._compact_people_plan(analysis)

        self.assertEqual(
            analysis["people_plan"],
            [
                {
                    "person": "Иван Карповец",
                    "task": "проверить и исправить проставление направлений при разделении заказов",
                    "status": "in_progress",
                    "dependency": "",
                    "comment": "разговорный контекст",
                }
            ],
        )
        self.assertEqual(
            analysis["pm_checklist"],
            ["Уточнить владельца и статус: Провести демо по табелю (упомянут Мили)."],
        )

    def test_daily_pm_removes_pm_control_duplicates_with_people_plan(self) -> None:
        analysis = {
            "people_plan": [
                {
                    "person": "Иван Карповец",
                    "task": "Проверить и исправить проставление направлений при разделении заказов",
                    "status": "in_progress",
                    "dependency": "",
                    "comment": "",
                }
            ],
            "pm_checklist": [
                "Проконтролировать исправления по направлениям заказов у Ивана Карповца",
                "Организовать демо по табелю для HR и финотдела",
            ],
            "needs_verification": [
                "Проверить корректность проставления направлений при разделении заказов",
                "Проверить запись демо по табелям",
            ],
            "dont_lose_today": ["Не потерять запись демо по табелям"],
        }

        llm = DailyPMChecklistLLM.__new__(DailyPMChecklistLLM)
        llm._remove_pm_duplicates_with_people_plan(analysis)

        self.assertEqual(analysis["pm_checklist"], ["Организовать демо по табелю для HR и финотдела"])
        self.assertEqual(analysis["needs_verification"], ["Проверить запись демо по табелям"])
        self.assertEqual(analysis["dont_lose_today"], ["Не потерять запись демо по табелям"])


if __name__ == "__main__":
    unittest.main()
