from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from .models import DailyPlan, MeetingRecord
from .people import PeopleDirectory


DAILY_PM_STATUSES = {
    "todo",
    "in_progress",
    "done",
    "needs_verification",
    "blocked",
    "waiting_dependency",
    "needs_estimation",
}

ANALYSIS_LIMITS = {
    "focus_of_day": 5,
    "people_plan": 18,
    "pm_checklist": 8,
    "dependencies": 8,
    "needs_verification": 5,
    "in_progress": 8,
    "blockers_and_risks": 6,
    "dont_lose_today": 4,
    "source_conflicts": 6,
}

PEOPLE_TASKS_PER_PERSON_LIMIT = 3
QUALITY_TOTAL_CHECKLIST_LIMIT = 18
QUALITY_PEOPLE_PLAN_LIMIT = 14
QUALITY_PM_SOFT_LIMIT = 6


@dataclass(slots=True)
class DailyPMLLMConfig:
    enabled: bool
    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int = 180

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)


@dataclass(slots=True)
class DailyPMResult:
    markdown: str
    people_plan: list[dict[str, str]]
    pm_checklist: list[str]
    needs_verification: list[str]
    dont_lose_today: list[str]
    notes: list[str]
    analysis: dict[str, Any]


class DailyPMChecklistLLM:
    """Generate a PM operating checklist from daily transcript and AI summary.

    DailyPlanV2 extracts explicit commitments. This layer adds the PM view:
    follow-ups, dependencies, verification points, and "do not lose today".
    """

    def __init__(self, config: DailyPMLLMConfig, people: PeopleDirectory | None = None) -> None:
        self.config = config
        self.people = people or PeopleDirectory.from_file()

    def enhance(
        self,
        *,
        report_date: date,
        team_name: str,
        base_plan: DailyPlan,
        meetings: list[MeetingRecord],
    ) -> DailyPMResult | None:
        if not self.config.usable:
            return None

        payload = {
            "date": report_date.isoformat(),
            "team": team_name,
            "source": {
                "meeting_ids": base_plan.source_meeting_ids,
                "meetings": [self._meeting_payload(meeting) for meeting in meetings],
            },
            "people_directory": self._people_directory_payload(),
            "base_plan": base_plan.model_dump(mode="json"),
        }
        analysis = self._analysis_step(payload)
        if not analysis:
            return None
        markdown = self._generation_step(payload=payload, analysis=analysis)
        if not markdown:
            return None
        repaired = self._self_check_step(payload=payload, analysis=analysis, markdown=markdown)
        final_markdown = repaired or markdown
        final_markdown = self._normalize_markdown(final_markdown, report_date=report_date, team_name=team_name)

        return DailyPMResult(
            markdown=final_markdown,
            people_plan=list(analysis.get("people_plan") or []),
            pm_checklist=self._clean_list(analysis.get("pm_checklist")),
            needs_verification=self._clean_list(analysis.get("needs_verification")),
            dont_lose_today=self._clean_list(analysis.get("dont_lose_today")),
            notes=self._notes(analysis),
            analysis=analysis,
        )

    def _analysis_step(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        content = self._chat_completion(
            messages=[
                {"role": "system", "content": self._analysis_prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        parsed = self._parse_json_object(content)
        if not parsed:
            return None
        analysis = self._postprocess_analysis(parsed, payload=payload, apply_quality_gate=False)
        try:
            edited = self._editor_step(payload=payload, analysis=analysis)
        except Exception as exc:
            edited = None
            analysis["editor_notes"] = self._clean_list(analysis.get("editor_notes")) + [
                f"Checklist editor LLM failed; quality gate fallback used: {type(exc).__name__}."
            ]
        if edited:
            analysis = self._postprocess_analysis(edited, payload=payload, apply_quality_gate=True)
        else:
            self._apply_quality_gate(analysis)
        return analysis

    def _editor_step(self, *, payload: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any] | None:
        content = self._chat_completion(
            messages=[
                {"role": "system", "content": self._editor_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "date": payload["date"],
                            "team": payload["team"],
                            "source_meeting_ids": payload["source"]["meeting_ids"],
                            "people_directory": payload["people_directory"],
                            "quality_limits": {
                                "total_checklist_items": QUALITY_TOTAL_CHECKLIST_LIMIT,
                                "people_plan": QUALITY_PEOPLE_PLAN_LIMIT,
                                "pm_sections_soft": QUALITY_PM_SOFT_LIMIT,
                                "people_tasks_per_person": PEOPLE_TASKS_PER_PERSON_LIMIT,
                            },
                            "draft_analysis": analysis,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        parsed = self._parse_json_object(content)
        if not parsed:
            return None
        edited = parsed.get("analysis") if isinstance(parsed.get("analysis"), dict) else parsed
        if not isinstance(edited, dict):
            return None
        notes = edited.setdefault("editor_notes", [])
        if isinstance(notes, list):
            notes.append("Checklist editor LLM pass applied.")
        return edited

    def _postprocess_analysis(
        self,
        parsed: dict[str, Any],
        *,
        payload: dict[str, Any],
        apply_quality_gate: bool,
    ) -> dict[str, Any]:
        analysis = self._normalize_analysis(parsed)
        self._canonicalize_people_plan(analysis)
        self._compact_people_plan(analysis)
        self._apply_domain_watchlist(analysis, payload)
        self._filter_low_signal_pm_sections(analysis)
        self._remove_pm_duplicates_with_people_plan(analysis)
        self._dedupe_cross_sections(analysis)
        self._limit_analysis_sections(analysis)
        if apply_quality_gate:
            self._apply_quality_gate(analysis)
        return analysis

    def _generation_step(self, *, payload: dict[str, Any], analysis: dict[str, Any]) -> str:
        return self._chat_completion(
            messages=[
                {"role": "system", "content": self._generation_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "date": payload["date"],
                            "team": payload["team"],
                            "source_meeting_ids": payload["source"]["meeting_ids"],
                            "analysis": analysis,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        ).strip()

    def _self_check_step(self, *, payload: dict[str, Any], analysis: dict[str, Any], markdown: str) -> str:
        return self._chat_completion(
            messages=[
                {"role": "system", "content": self._self_check_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "date": payload["date"],
                            "team": payload["team"],
                            "source_meeting_ids": payload["source"]["meeting_ids"],
                            "analysis": analysis,
                            "markdown": markdown,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        ).strip()

    def _chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
    ) -> str:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.1,
            "messages": messages,
        }
        if response_format:
            body["response_format"] = response_format
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.config.timeout_seconds,
        )
        if response.status_code >= 400 and "response_format" in body:
            body.pop("response_format", None)
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.config.timeout_seconds,
            )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    @staticmethod
    def _meeting_payload(meeting: MeetingRecord) -> dict[str, Any]:
        artifacts = meeting.artifacts or {}
        return {
            "loom_video_id": meeting.loom_video_id,
            "title": meeting.title,
            "source_url": meeting.source_url,
            "recorded_at": meeting.recorded_at,
            "transcript": meeting.transcript_text[:30000],
            "summary": artifacts.get("summary"),
            "decisions": artifacts.get("decisions"),
            "action_items": artifacts.get("action_items"),
            "blockers": artifacts.get("blockers"),
            "completed_today": artifacts.get("completed_today"),
            "remaining_tech_debt": artifacts.get("remaining_tech_debt"),
            "business_requests_for_estimation": artifacts.get("business_requests_for_estimation"),
            "open_questions": artifacts.get("open_questions"),
        }

    def _people_directory_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "full_name": person.full_name,
                "bitrix_user_id": person.bitrix_user_id,
                "aliases": list(person.aliases),
            }
            for person in self.people.people
        ]

    @staticmethod
    def _analysis_prompt() -> str:
        return """
Ты анализируешь #daily технической команды и создаешь основу для задачи "План дня".

Входные данные:
1. TRANSCRIPT - полный транскрипт встречи. Это источник истины.
2. SUMMARY / AI artifacts - предварительная AI-выжимка. Это вспомогательная структура.
3. BASE_PLAN - текущий машинный разбор явных обязательств по людям. Используй его как черновик, но не доверяй слепо.
4. PEOPLE_DIRECTORY - справочник людей. Если человек найден в справочнике, используй только canonical `full_name`.

Главное правило:
Если TRANSCRIPT и SUMMARY расходятся, выбирай TRANSCRIPT. Не считай задачу завершенной только потому, что summary так сказала.
Фразы "вроде", "кажется", "надо проверить", "посмотрим", "скину", "найду", "после daily обсудим" означают неопределенность,
follow-up или зависимость. Такие пункты нельзя относить к done.

Цель:
Сформировать не протокол встречи, а компактный план обязательств дня:
1. `people_plan` - что конкретный участник команды обязался сделать / проверить / выкатить / протестировать / подготовить.
2. `pm_checklist` - только управленческие действия ПМа, которые не являются прямой задачей исполнителя.
3. `needs_verification` - ручные проверки результата, а не повтор задач людей.
4. `dont_lose_today` - короткие напоминания, которые не дублируют другие разделы.

Ключевой принцип:
Чеклист человека = технический результат исполнителя.
Чеклист ПМа = контроль стыков, зависимостей, демо, коммуникаций, фиксации решений.
Не превращай каждую реплику ПМа "проверю / проконтролирую / уточню у X" в отдельный PM-пункт, если уже есть задача X.

Выдели:
1. Главные темы дня.
2. Явные обязательства по людям.
3. Только настоящие PM-follow-up задачи.
4. Зависимости и последовательности действий.
5. Пункты, требующие подтверждения или ручной проверки.
6. То, что в работе и не закрыто.
7. Реальные блокеры и риски.
8. Темы, которые легко потерять, но по ним нужен follow-up сегодня.

Как формулировать `people_plan`:
- Пиши как исполнимое обязательство результата: "Доработать...", "Проверить...", "Протестировать...", "Перенести на прод...", "Подготовить инструкцию...".
- Не копируй разговорные хвосты: "ну", "там", "вроде", "посмотрим", "если успею".
- Не ставь одному человеку 5-10 микропунктов по одной теме. Объединяй до 1-3 содержательных задач на человека.
- Если реплика звучит как план ПМа контролировать чужую работу, не назначай ее ПМу как персональное обязательство.
- Если человек не найден в PEOPLE_DIRECTORY, не создавай ему отдельный чеклист. Перенеси это в PM-follow-up как уточнение владельца.
- Если задача уже сделана на встрече, не добавляй ее в daily checklist; вынеси в `in_progress` или вообще пропусти, если это просто факт.

Как формулировать `pm_checklist`:
- Максимум 8 пунктов.
- Только действия, которые должен сделать ПМ: организовать демо, согласовать окно релиза, собрать недельный дайджест, зафиксировать решение, синхронизировать владельцев, проверить критичный результат.
- Не дублируй задачи людей в форме "проконтролировать работу Иванa". Если нужна проверка, сформулируй один агрегированный пункт по теме, а не по каждому человеку.
- Избегай пустых пунктов вроде "созвониться с ответственными", "не потерять обсуждения", "обновить задачи" без конкретной темы.

Ограничения объема и дублей:
- `focus_of_day` — максимум 5 тем.
- `people_plan` — максимум 18 задач суммарно и максимум 3 задачи на одного человека.
- `pm_checklist` — максимум 8 пунктов.
- `needs_verification` — максимум 5 пунктов; не копируй туда дословно пункты из `pm_checklist`.
- `dont_lose_today` — максимум 4 пункта; не копируй туда то же действие, что уже есть в `pm_checklist`.
- Не создавай отдельный PM-пункт для каждого упоминания темы. Объединяй близкие действия в один контрольный пункт.
- Не дублируй одну тему в трех разделах: если она есть в `pm_checklist`, в дополнительных разделах оставляй только уточнение другого типа.

Допустимые статусы:
todo, in_progress, done, needs_verification, blocked, waiting_dependency, needs_estimation.

Правила по именам:
- Не придумывай фамилии и не исправляй их на слух.
- Если в transcript звучит короткое имя или алиас, сопоставь его с PEOPLE_DIRECTORY.
- В `people_plan.person` возвращай canonical `full_name` из PEOPLE_DIRECTORY.
- Не сопоставляй похожие, но разные имена только по похожести: например, "Валентина" не равно "Валентин Семенихин".
- Если человека нет в PEOPLE_DIRECTORY, оставь имя как прозвучало.

Доменные темы, которые нельзя потерять, если они есть в transcript:
- DemoPaymentsPro / Payments Pro: демо, тестирование, инструкция.
- Заказы без резерва: новость -> открытие кнопки -> обработка менеджерами.
- Направления / акции / ограничения изменения направления.
- Фильтры: если звучит "вроде починили", "ломаются", "массовая проблема", статус должен быть needs_verification или in_progress.
- База данных для Кристины.
- ПВРА / правки ПВРА.
- Запись демо по табелям.
- ФОП / Стас / Николай: проверить кейс, дождаться описания/подтверждения.
- БПП прием / задача от Виктора Толе.
- Большая задача после daily.

Верни только JSON object без Markdown и пояснений:
{
  "focus_of_day": ["3-5 фокусов дня"],
  "people_plan": [
    {
      "person": "имя человека",
      "task": "краткое обязательство технического результата",
      "status": "todo | in_progress | done | needs_verification | blocked | waiting_dependency | needs_estimation",
      "dependency": "от кого или чего зависит, если есть",
      "comment": "короткий контекст из transcript"
    }
  ],
  "pm_checklist": ["действия ПМа в форме, пригодной для выполнения"],
  "dependencies": ["цепочки вида X -> Y -> Z"],
  "needs_verification": ["пункты ручной проверки"],
  "in_progress": ["незакрыто / в работе"],
  "blockers_and_risks": ["препятствия и риски"],
  "dont_lose_today": ["короткие follow-up пункты, которые легко потерять"],
  "source_conflicts": ["если summary и transcript расходятся"]
}
""".strip()

    @staticmethod
    def _editor_prompt() -> str:
        return """
Ты второй проход обработки daily: редактор чеклистов перед созданием CRM-задачи.

На входе уже есть `draft_analysis`. Твоя задача — не пересказывать встречу заново, а ужать и очистить результат до рабочего плана дня.

Что удалить:
1. Шум стенограммы, разговорные хвосты, "ну/там/посмотрим/если успею".
2. Дубли между `people_plan`, `pm_checklist`, `needs_verification`, `dont_lose_today`.
3. Планы не на сегодня: "к концу месяца", "на следующей неделе", "потом", если это не требует действия ПМа именно сегодня.
4. PM-псевдозадачи: "проконтролировать X", "уточнить статус у X", "не потерять X", если у X уже есть нормальная задача в `people_plan`.
5. Пункты без исполнимого результата: "созвониться", "обсудить", "посмотреть", если не указан конкретный артефакт или решение.
6. Неизвестных людей как отдельные чеклисты. Если владелец не из PEOPLE_DIRECTORY, оставь один PM-пункт "уточнить владельца..." только если это реально важно сегодня.

Что оставить:
1. Конкретные обязательства участников на сегодня, 1-3 пункта на человека.
2. Реальные зависимости: когда один пункт блокирует другой.
3. PM-действия только там, где без ПМа процесс не сдвинется: демо, релизное окно, согласование, фиксация решения, критичная ручная проверка.
4. Ручные проверки результата, которые не повторяют задачу исполнителя.

Quality gate:
- Целевой общий объем чеклистов: 12-16 пунктов.
- Жесткий максимум чеклистов: 18 пунктов суммарно по `people_plan` + PM-разделам.
- Если PM-пунктов больше, чем задач людей, сократи PM-разделы. В норме PM-пунктов должно быть меньше или равно задачам людей.
- `pm_checklist` + `needs_verification` + `dont_lose_today` вместе — максимум 6, если только людей нет вообще.
- Сначала сохраняй задачи людей, потом PM-контроль. `dont_lose_today` — самый низкий приоритет.

Верни только JSON object без Markdown:
{
  "focus_of_day": ["до 5 фокусов"],
  "people_plan": [
    {
      "person": "canonical full_name из PEOPLE_DIRECTORY",
      "task": "короткое обязательство результата на сегодня",
      "status": "todo | in_progress | done | needs_verification | blocked | waiting_dependency | needs_estimation",
      "dependency": "если есть",
      "comment": "короткий контекст"
    }
  ],
  "pm_checklist": ["только PM-действия"],
  "dependencies": ["цепочки X -> Y -> Z"],
  "needs_verification": ["ручные проверки результата"],
  "in_progress": ["важное незакрыто / в работе, без дублей чеклистов"],
  "blockers_and_risks": ["риски"],
  "dont_lose_today": ["1-2 критичных напоминания, если они не дублируют другое"],
  "source_conflicts": ["только реальные конфликты источников"],
  "editor_notes": ["кратко что было очищено"]
}
""".strip()

    @staticmethod
    def _generation_prompt() -> str:
        return """
На основе структурированного анализа daily сформируй финальную задачу дня для ПМа в Markdown.

Требования:
1. Это рабочий PM-чеклист, а не протокол встречи.
2. Используй чекбоксы только для действий ПМа и ручных проверок.
3. Сохрани отдельный план по людям: 1-3 коротких обязательства на человека.
4. Отдельно выдели зависимости и последовательности.
5. Отдельно выдели пункты, требующие подтверждения.
6. Не помещай в "Сделано" то, что имеет статус in_progress, needs_verification или waiting_dependency.
7. Формулируй действия так, чтобы их можно было выполнить в течение дня.
8. Не теряй важные follow-up, но не превращай каждую реплику в отдельную задачу.
9. Не используй Markdown-таблицы: Bitrix плохо отображает таблицы в задачах.
10. Пиши кратко: задача должна быть емкой к обязательствам, а не длинной стенограммой.
11. Не дублируй одни и те же действия в "Чеклист ПМа", "Требует подтверждения" и "Не потерять сегодня".
12. Соблюдай лимиты: общий объем чеклистов до 18 пунктов; PM-разделы вместе до 6 пунктов; "Не потерять сегодня" только для 1-2 критичных напоминаний.
13. Если задача человека уже есть в "План по людям", не повторяй ее как PM-контроль, кроме случаев реальной зависимости или демо.

Структура Markdown:

# План дня ПМ по daily: {{date}}

**Команда:** {{team}}
**Источник:** #daily встреча / {{source}}

---

## 1. Фокус дня
- ...

---

## 2. Чеклист ПМа
- [ ] ...

---

## 3. План по людям
### {{person}}
- **Задача:** ...
- **Зависит от:** ...
- **Комментарий:** ...

---

## 4. Зависимости / последовательности
- ...

---

## 5. Требует подтверждения / ручной проверки
- [ ] ...

---

## 6. Не закрыто / в работе
- ...

---

## 7. Блокеры / риски
- ...

---

## 8. Не потерять сегодня
- [ ] ...

Верни только Markdown без пояснений.
""".strip()

    @staticmethod
    def _self_check_prompt() -> str:
        return """
Проверь финальную задачу дня перед созданием в CRM.

Checklist самопроверки:
1. Есть ли отдельный блок "Чеклист ПМа"?
2. Есть ли там действия ПМа, а не только задачи исполнителей?
3. Не дублирует ли "Чеклист ПМа" персональные задачи людей?
4. Выделены ли зависимости вида "после X -> Y -> Z"?
5. Не потеряны ли темы из transcript, которых нет или мало в summary?
6. Есть ли отдельный блок "Требует подтверждения / ручной проверки"?
7. Есть ли отдельный блок "Не закрыто / в работе"?
8. Задача читается как рабочий план дня, а не как протокол встречи?
9. Нет ли смешения статусов done / in_progress / needs_verification / waiting_dependency?
10. Не попали ли пункты с "вроде", "надо проверить", "посмотрим", "скину", "сегодня уточню" в done?
11. Нет ли Markdown-таблиц?
12. План по людям не раздут: максимум 1-3 обязательства на человека, без разговорных фрагментов?
13. PM-разделы не раздуты: нет ли пустых пунктов "созвониться", "не потерять", "обновить задачи" без конкретной темы?
14. Не превышает ли общий объем чеклистов 18 пунктов и не больше ли PM-пунктов, чем задач людей?

Если есть ошибки, исправь Markdown.
Верни только исправленную финальную версию задачи в Markdown.
""".strip()

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any] | None:
        text = content.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _normalize_analysis(cls, parsed: dict[str, Any]) -> dict[str, Any]:
        return {
            "focus_of_day": cls._clean_list(parsed.get("focus_of_day")),
            "people_plan": cls._clean_people_plan(parsed.get("people_plan")),
            "pm_checklist": cls._clean_list(parsed.get("pm_checklist")),
            "dependencies": cls._clean_list(parsed.get("dependencies")),
            "needs_verification": cls._clean_list(parsed.get("needs_verification")),
            "in_progress": cls._clean_list(parsed.get("in_progress")),
            "blockers_and_risks": cls._clean_list(parsed.get("blockers_and_risks")),
            "dont_lose_today": cls._clean_list(parsed.get("dont_lose_today")),
            "source_conflicts": cls._clean_list(parsed.get("source_conflicts")),
            "editor_notes": cls._clean_list(parsed.get("editor_notes")),
            "quality_gate_notes": cls._clean_list(parsed.get("quality_gate_notes")),
        }

    def _canonicalize_people_plan(self, analysis: dict[str, Any]) -> None:
        people_plan = analysis.get("people_plan")
        if not isinstance(people_plan, list):
            return
        for item in people_plan:
            if not isinstance(item, dict):
                continue
            person = self.people.find(str(item.get("person") or ""))
            if person:
                item["person"] = person.full_name

    def _compact_people_plan(self, analysis: dict[str, Any]) -> None:
        """Keep only assignable technical commitments, grouped compactly by person."""

        people_plan = analysis.get("people_plan")
        if not isinstance(people_plan, list):
            analysis["people_plan"] = []
            return

        compact: list[dict[str, str]] = []
        seen_by_person: dict[str, list[str]] = {}
        count_by_person: dict[str, int] = {}
        for item in people_plan:
            if not isinstance(item, dict):
                continue
            raw_person = self._text(item.get("person"))
            task = self._compact_task_title(item.get("task"))
            if not task:
                continue
            person = self.people.find(raw_person)
            if not person:
                if raw_person and not self._is_low_signal_person_task(task):
                    self._append_unique(
                        analysis,
                        "pm_checklist",
                        f"Уточнить владельца и статус: {task} (упомянут {raw_person}).",
                    )
                continue
            if self._is_low_signal_person_task(task):
                continue

            person_key = person.full_name
            if count_by_person.get(person_key, 0) >= PEOPLE_TASKS_PER_PERSON_LIMIT:
                continue
            task_key = self._similarity_key(task)
            if not task_key:
                continue
            existing_keys = seen_by_person.setdefault(person_key, [])
            if any(self._is_similar_key(task_key, existing_key) for existing_key in existing_keys):
                continue
            existing_keys.append(task_key)
            count_by_person[person_key] = count_by_person.get(person_key, 0) + 1

            status = self._text(item.get("status")) or "todo"
            if status not in DAILY_PM_STATUSES:
                status = "needs_verification"
            compact.append(
                {
                    "person": person.full_name,
                    "task": task,
                    "status": status,
                    "dependency": self._compact_task_title(item.get("dependency")),
                    "comment": self._compact_task_title(item.get("comment")),
                }
            )

        analysis["people_plan"] = compact

    def _filter_low_signal_pm_sections(self, analysis: dict[str, Any]) -> None:
        for key in ("pm_checklist", "needs_verification", "dont_lose_today"):
            values = analysis.get(key)
            if not isinstance(values, list):
                analysis[key] = []
                continue
            analysis[key] = [item for item in self._clean_list(values) if not self._is_low_signal_pm_item(item)]

    def _remove_pm_duplicates_with_people_plan(self, analysis: dict[str, Any]) -> None:
        people_keys = [
            self._similarity_key(item.get("task"))
            for item in analysis.get("people_plan", [])
            if isinstance(item, dict) and item.get("task")
        ]
        people_keys = [key for key in people_keys if key]
        if not people_keys:
            return

        for section in ("pm_checklist", "needs_verification", "dont_lose_today"):
            filtered: list[str] = []
            for item in self._clean_list(analysis.get(section)):
                item_key = self._similarity_key(item)
                if not item_key:
                    continue
                if self._is_pm_control_duplicate(item, item_key, people_keys):
                    continue
                filtered.append(item)
            analysis[section] = filtered

    def _apply_domain_watchlist(self, analysis: dict[str, Any], payload: dict[str, Any]) -> None:
        text = self._payload_text(payload)

        if "payments pro" in text or "demopaymentspro" in text:
            self._append_unique(analysis, "focus_of_day", "Payments Pro / DemoPaymentsPro: демо, тестирование и инструкция.")
            self._append_unique(analysis, "pm_checklist", "Проконтролировать демо, тестирование и инструкцию по Payments Pro.")

        if "заказ" in text and "резерв" in text:
            self._append_unique(
                analysis,
                "dependencies",
                "Новость о заказах без резерва -> открытие кнопки «Отправить в 1С» -> обработка заказов менеджерами.",
            )
            self._append_unique(
                analysis,
                "pm_checklist",
                "Проконтролировать цепочку по заказам без резерва: новость -> кнопка -> обработка менеджерами.",
            )

        if "направлен" in text and ("акци" in text or "резерв" in text):
            self._append_unique(
                analysis,
                "needs_verification",
                "Проверить доработки по направлениям, акциям и резервам на реальных кейсах.",
            )

        if "фильтр" in text:
            self._append_unique(
                analysis,
                "needs_verification",
                "Проверить, что фильтры реально работают стабильно, а не только «вроде починили».",
            )

        if "кристин" in text:
            self._append_unique(
                analysis,
                "in_progress",
                "База данных для Кристины в работе: уточнить остаток работ и срок.",
            )
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Не потерять статус базы данных для Кристины.",
            )

        if "пвра" in text:
            self._append_unique(
                analysis,
                "in_progress",
                "Правки ПВРА в работе: проконтролировать владельца и следующий результат.",
            )
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Проверить статус правок ПВРА.",
            )

        if "табел" in text and ("демо" in text or "запис" in text):
            self._append_unique(
                analysis,
                "needs_verification",
                "Получить или проверить запись демо по табелям и зафиксировать комментарий в задаче.",
            )
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Не потерять запись демо по табелям.",
            )

        if "фоп" in text and ("стас" in text or "никол" in text):
            self._append_unique(
                analysis,
                "needs_verification",
                "Дождаться описания Стаса по ФОПам со стороны сайта и проверить кейс Николая.",
            )
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Не потерять ФОП-кейс Николая / описание Стаса.",
            )

        if "бпп" in text and "при" in text:
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Проверить, что Виктор передал Анатолию задачу по БПП приему.",
            )

        if "после дейли" in text or "после daily" in text:
            self._append_unique(
                analysis,
                "dont_lose_today",
                "Зафиксировать результат обсуждений, запланированных после daily.",
            )

    @classmethod
    def _dedupe_cross_sections(cls, analysis: dict[str, Any]) -> None:
        analysis["pm_checklist"] = cls._dedupe_similar_list(analysis.get("pm_checklist"))
        pm_checklist = analysis.get("pm_checklist") or []
        analysis["needs_verification"] = cls._dedupe_similar_list(
            analysis.get("needs_verification"),
            existing=pm_checklist,
        )
        analysis["dont_lose_today"] = cls._dedupe_similar_list(
            analysis.get("dont_lose_today"),
            existing=pm_checklist + (analysis.get("needs_verification") or []),
        )
        for key in ("focus_of_day", "dependencies", "in_progress", "blockers_and_risks", "source_conflicts"):
            analysis[key] = cls._dedupe_similar_list(analysis.get(key))

    @classmethod
    def _limit_analysis_sections(cls, analysis: dict[str, Any]) -> None:
        for key, limit in ANALYSIS_LIMITS.items():
            value = analysis.get(key)
            if isinstance(value, list):
                analysis[key] = value[:limit]

    @classmethod
    def _apply_quality_gate(cls, analysis: dict[str, Any]) -> None:
        """Keep the final CRM checklist small enough to be operational."""

        notes: list[str] = []
        people_plan = cls._clean_people_plan(analysis.get("people_plan"))
        if len(people_plan) > QUALITY_PEOPLE_PLAN_LIMIT:
            notes.append(f"People plan trimmed from {len(people_plan)} to {QUALITY_PEOPLE_PLAN_LIMIT}.")
            people_plan = people_plan[:QUALITY_PEOPLE_PLAN_LIMIT]
        analysis["people_plan"] = people_plan

        people_count = len([item for item in people_plan if item.get("status") != "done"])
        original_pm_count = sum(
            len(cls._clean_list(analysis.get(section)))
            for section in ("pm_checklist", "needs_verification", "dont_lose_today")
        )
        remaining_capacity = max(0, QUALITY_TOTAL_CHECKLIST_LIMIT - people_count)
        pm_capacity = min(QUALITY_PM_SOFT_LIMIT, remaining_capacity)
        if people_count:
            pm_capacity = min(pm_capacity, people_count)

        cls._trim_pm_sections_for_quality_gate(analysis, pm_capacity)

        final_pm_count = sum(
            len(cls._clean_list(analysis.get(section)))
            for section in ("pm_checklist", "needs_verification", "dont_lose_today")
        )
        final_total = people_count + final_pm_count
        if original_pm_count > final_pm_count:
            notes.append(f"PM sections trimmed from {original_pm_count} to {final_pm_count}.")
        if final_total > QUALITY_TOTAL_CHECKLIST_LIMIT:
            notes.append(f"Checklist total still high after trimming: {final_total}.")
        if notes:
            analysis["quality_gate_notes"] = cls._clean_list(analysis.get("quality_gate_notes")) + notes

    @classmethod
    def _trim_pm_sections_for_quality_gate(cls, analysis: dict[str, Any], pm_capacity: int) -> None:
        if pm_capacity <= 0:
            analysis["pm_checklist"] = []
            analysis["needs_verification"] = []
            analysis["dont_lose_today"] = []
            return

        pm_checklist = cls._clean_list(analysis.get("pm_checklist"))
        needs_verification = cls._clean_list(analysis.get("needs_verification"))
        dont_lose_today = cls._clean_list(analysis.get("dont_lose_today"))

        pm_limit = min(len(pm_checklist), min(4, pm_capacity))
        analysis["pm_checklist"] = pm_checklist[:pm_limit]
        remaining = pm_capacity - pm_limit

        verification_limit = min(len(needs_verification), min(2, remaining))
        analysis["needs_verification"] = needs_verification[:verification_limit]
        remaining -= verification_limit

        dont_lose_limit = min(len(dont_lose_today), min(1, remaining))
        analysis["dont_lose_today"] = dont_lose_today[:dont_lose_limit]

    @classmethod
    def _dedupe_similar_list(cls, values: object, existing: list[str] | None = None) -> list[str]:
        result: list[str] = []
        seen: list[str] = []
        for item in existing or []:
            key = cls._similarity_key(item)
            if key:
                seen.append(key)
        iterable = values if isinstance(values, list) else []
        for value in iterable:
            text = cls._text(value)
            if not text:
                continue
            key = cls._similarity_key(text)
            if not key:
                continue
            if any(cls._is_similar_key(key, other) for other in seen):
                continue
            seen.append(key)
            result.append(text)
        return result

    @staticmethod
    def _payload_text(payload: dict[str, Any]) -> str:
        chunks: list[str] = []
        for meeting in ((payload.get("source") or {}).get("meetings") or []):
            if isinstance(meeting, dict):
                for key in ("title", "transcript", "summary"):
                    value = meeting.get(key)
                    if value:
                        chunks.append(str(value))
        return "\n".join(chunks).casefold()

    @classmethod
    def _append_unique(cls, analysis: dict[str, Any], key: str, value: str) -> None:
        current = analysis.setdefault(key, [])
        if not isinstance(current, list):
            analysis[key] = current = []
        normalized_value = cls._text(value).casefold()
        for item in current:
            if cls._text(item).casefold() == normalized_value:
                return
            if cls._is_similar_key(cls._similarity_key(item), cls._similarity_key(value)):
                return
        current.append(value)

    @classmethod
    def _compact_task_title(cls, value: object, *, limit: int = 220) -> str:
        text = cls._text(value)
        if not text:
            return ""
        text = re.sub(r"^(?:ну|а|так|окей|добро|да|угу)[,:\s]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:типа|короче|в принципе|как бы)\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" -;,.")
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip(" ,.;") + "..."

    @classmethod
    def _is_low_signal_person_task(cls, value: object) -> bool:
        text = cls._text(value).casefold()
        if len(text) < 8:
            return True
        if any(
            phrase in text
            for phrase in (
                "формат по дейли",
                "формат дейли",
                "каждый озвучивал",
                "можем разбегаться",
                "давайте порядок",
                "на этом все",
                "если какие-то вопросы",
                "хорошего дня",
            )
        ):
            return True
        action_verbs = (
            "доработ",
            "сдел",
            "законч",
            "провер",
            "тест",
            "подготов",
            "выл",
            "перенес",
            "напис",
            "исправ",
            "настро",
            "созда",
            "скин",
            "отда",
            "собра",
            "соглас",
            "провест",
            "показ",
            "разобра",
            "продолж",
            "поднят",
            "запуст",
            "дожд",
            "уточн",
            "перев",
            "розіб",
            "дороб",
            "перевір",
            "підгот",
            "протест",
            "виправ",
        )
        if not any(verb in text for verb in action_verbs):
            return True
        return False

    @classmethod
    def _is_low_signal_pm_item(cls, value: object) -> bool:
        text = cls._text(value)
        folded = text.casefold()
        if len(folded) < 12:
            return True
        low_signal_phrases = (
            "зафиксировать результат обсуждений, запланированных после daily",
            "зафиксировать результат обсуждений после daily",
            "обновить задачи с учетом новых чек-листов",
            "перегенерировать описания",
            "дозвониться до ответственных по доработкам",
            "дозвониться до ответственных",
            "обеспечить дозвон",
            "поддерживать формат дейли",
            "поддержать внедрение формата дейли",
            "формата дейликов",
            "формат дейликов",
            "чек-листами для лучшей фиксации",
            "использовать формат с чек-листами",
            "перегенерации описания задачи",
            "не забыть провести демо",
            "не потерять сегодня",
        )
        if any(phrase in folded for phrase in low_signal_phrases):
            return True
        if folded.startswith("не потерять") and not cls._has_domain_signal(folded):
            return True
        generic_verbs = (
            "проверить статус",
            "проконтролировать статус",
            "уточнить статус",
            "синхронизироваться",
            "дождаться результата",
            "обновить задачу",
        )
        if any(folded.startswith(verb) for verb in generic_verbs) and not cls._has_domain_signal(folded):
            return True
        return False

    @classmethod
    def _has_domain_signal(cls, text: str) -> bool:
        folded = cls._text(text).casefold()
        domain_tokens = (
            "payments",
            "payment",
            "резерв",
            "направлен",
            "табел",
            "пвра",
            "вра",
            "бп",
            "бпп",
            "фоп",
            "база",
            "кристин",
            "крон",
            "cron",
            "демо",
            "инструкц",
            "прод",
            "продакш",
            "тест",
            "баг",
            "заказ",
            "фильтр",
            "холд",
            "обмен",
            "интеграц",
            "send pulse",
            "manychat",
            "esputnik",
            "табель",
            "отчет",
        )
        if any(token in folded for token in domain_tokens):
            return True
        return bool(re.search(r"\b(?:иван|анатол|михаил|миша|виктор|витя|андрей|эмиль|валентин|игорь|николай|василий)\b", folded))

    @classmethod
    def _is_pm_control_duplicate(cls, item: str, item_key: str, people_keys: list[str]) -> bool:
        folded = item.casefold()
        pm_control = any(
            verb in folded
            for verb in (
                "проконтрол",
                "проверить",
                "уточнить",
                "синхрониз",
                "контрол",
                "обеспеч",
                "подготов",
                "собра",
                "дождаться",
                "не потерять",
            )
        )
        if not pm_control:
            return False
        for person_key in people_keys:
            if cls._is_similar_key(item_key, person_key):
                return True
            if cls._token_overlap(item_key, person_key) >= 0.45:
                return True
        return False

    @staticmethod
    def _token_overlap(left: str, right: str) -> float:
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))

    @classmethod
    def _clean_people_plan(cls, values: object) -> list[dict[str, str]]:
        if not isinstance(values, list):
            return []
        result: list[dict[str, str]] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            status = cls._text(value.get("status"))
            if status and status not in DAILY_PM_STATUSES:
                status = "needs_verification"
            item = {
                "person": cls._text(value.get("person")),
                "task": cls._text(value.get("task")),
                "status": status or "todo",
                "dependency": cls._text(value.get("dependency")),
                "comment": cls._text(value.get("comment")),
            }
            if item["person"] or item["task"]:
                result.append(item)
        return result

    @classmethod
    def _clean_list(cls, values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = cls._text(value)
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(text)
        return result

    @staticmethod
    def _text(value: object) -> str:
        text = str(value or "").replace("\u00a0", " ").strip()
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)]|\[[ xX]\])\s*", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" -;")

    @classmethod
    def _similarity_key(cls, value: object) -> str:
        text = cls._text(value).casefold()
        text = re.sub(r"\[[^\]]+\]", "", text)
        text = re.sub(r"\([^)]*\)", "", text)
        text = re.sub(r"[^\wА-Яа-яІіЇїЄєҐґ]+", " ", text, flags=re.UNICODE)
        stopwords = {
                "что",
                "как",
                "для",
                "или",
                "это",
                "над",
                "при",
                "про",
                "после",
                "сегодня",
                "проверить",
                "проконтролировать",
                "уточнить",
                "зафиксировать",
                "дождаться",
                "контролировать",
                "не",
                "забыть",
                "провести",
                "организовать",
                "обеспечить",
                "своевременный",
                "сбор",
                "анализ",
                "новым",
                "правкам",
                "участием",
                "иван",
                "ивана",
                "карповец",
                "карповца",
                "анатолий",
                "анатолия",
                "михаил",
                "миша",
                "виктор",
                "витя",
                "андрей",
                "андрея",
                "эмиль",
                "эмиля",
                "валентин",
                "игорь",
                "николай",
                "василий",
            }
        tokens = [
            cls._stem_similarity_token(token)
            for token in text.split()
            if len(token) > 2 and token not in stopwords
        ]
        return " ".join(tokens)

    @staticmethod
    def _stem_similarity_token(token: str) -> str:
        for prefix in (
            "исправ",
            "направ",
            "провер",
            "простав",
            "раздел",
            "резерв",
            "табел",
            "подтверж",
            "демо",
            "платеж",
            "оплат",
            "заказ",
            "инструк",
            "тест",
            "подготов",
            "собра",
            "недель",
            "дайджест",
            "доработ",
            "перенос",
            "продакш",
            "отчет",
        ):
            if token.startswith(prefix):
                return prefix
        return token[:8] if len(token) > 8 else token

    @staticmethod
    def _is_similar_key(left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right or left in right or right in left:
            return True
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        if not left_tokens or not right_tokens:
            return False
        overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
        return overlap >= 0.72

    @staticmethod
    def _normalize_markdown(markdown: str, *, report_date: date, team_name: str) -> str:
        cleaned = markdown.strip()
        cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        if "## 2. Чеклист ПМа" not in cleaned:
            cleaned += "\n\n---\n\n## 2. Чеклист ПМа\n- [ ] Проверить и дополнить PM-чеклист вручную."
        if not cleaned.startswith("# "):
            cleaned = (
                f"# План дня ПМ по daily: {report_date.isoformat()}\n\n"
                f"**Команда:** {team_name}\n\n"
                f"{cleaned}"
            )
        return cleaned.strip()

    @staticmethod
    def _notes(analysis: dict[str, Any]) -> list[str]:
        notes: list[str] = ["PM daily checklist generated by LLM."]
        conflicts = analysis.get("source_conflicts")
        if isinstance(conflicts, list) and conflicts:
            notes.append(f"Source conflicts: {len(conflicts)}")
        for key in ("editor_notes", "quality_gate_notes"):
            values = analysis.get(key)
            if isinstance(values, list):
                notes.extend(str(item) for item in values if item)
        return notes
