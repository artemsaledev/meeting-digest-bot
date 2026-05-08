from __future__ import annotations

import unittest

from meeting_digest_bot.daily_pm_llm import DailyPMChecklistLLM


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

        self.assertEqual(len(analysis["pm_checklist"]), 12)
        self.assertEqual(len(analysis["needs_verification"]), 7)
        self.assertEqual(len(analysis["dont_lose_today"]), 6)


if __name__ == "__main__":
    unittest.main()
