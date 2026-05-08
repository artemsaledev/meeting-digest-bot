from __future__ import annotations

from datetime import date
import unittest

from meeting_digest_bot.completion_reports import CompletionReportBuilder


def _parent(row_id: str, title: str) -> dict:
    return {"ID": row_id, "TITLE": title, "PARENT_ID": "0"}


def _child(row_id: str, parent_id: str, title: str, *, complete: bool = False, members: dict | None = None) -> dict:
    return {
        "ID": row_id,
        "TITLE": title,
        "PARENT_ID": parent_id,
        "IS_COMPLETE": "Y" if complete else "N",
        "MEMBERS": members or {},
    }


class CompletionReportTests(unittest.TestCase):
    def test_daily_report_keeps_pm_groups_out_of_person_bucket(self) -> None:
        builder = CompletionReportBuilder()
        report = builder.build_daily(
            report_date=date(2026, 5, 7),
            team_name="Bitrix Develop Team",
            task_id=169611,
            task_url="https://totiscrm.com/workgroups/group/512/tasks/task/view/169611/",
            checklist_rows=[
                _parent("1", "Чеклист ПМа"),
                _child("2", "1", "Проконтролировать демо по табелю", members={"114736": {}}),
                _parent("3", "PM: Не потерять сегодня"),
                _child("4", "3", "Не потерять запись демо по табелям", members={"114736": {}}),
                _parent("5", "Иван Карповец"),
                _child("6", "5", "Проверить направления заказов", members={"51977": {}}),
            ],
        )

        telegram = builder.format_daily_telegram(report)

        self.assertIn("Не закрыто по людям:", telegram)
        self.assertIn("Иван Карповец @karpovets90", telegram)
        self.assertIn("PM-контроль @artsaledev", telegram)
        self.assertIn("Чеклист ПМа: 1 открыто", telegram)
        self.assertNotIn("Артем Явдокименко @artsaledev\n- Проконтролировать демо", telegram)

    def test_daily_report_truncates_long_telegram_messages(self) -> None:
        builder = CompletionReportBuilder()
        rows = [_parent("1", "Чеклист ПМа")]
        for index in range(120):
            rows.append(_child(str(index + 2), "1", f"Очень длинный PM пункт номер {index} " + "x" * 80))
        report = builder.build_daily(
            report_date=date(2026, 5, 7),
            team_name="Bitrix Develop Team",
            task_id=169611,
            task_url="https://totiscrm.com/workgroups/group/512/tasks/task/view/169611/",
            checklist_rows=rows,
        )

        telegram = builder.format_daily_telegram(report)

        self.assertLessEqual(len(telegram), 3900)
        self.assertTrue("Полный список" in telegram or "Сообщение сокращено" in telegram)


if __name__ == "__main__":
    unittest.main()
