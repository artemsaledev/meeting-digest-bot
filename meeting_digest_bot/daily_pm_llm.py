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
    "focus_of_day": 7,
    "people_plan": 12,
    "pm_checklist": 12,
    "dependencies": 8,
    "needs_verification": 7,
    "in_progress": 10,
    "blockers_and_risks": 8,
    "dont_lose_today": 6,
    "source_conflicts": 6,
}


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
        analysis = self._normalize_analysis(parsed)
        self._canonicalize_people_plan(analysis)
        self._apply_domain_watchlist(analysis, payload)
        self._dedupe_cross_sections(analysis)
        self._limit_analysis_sections(analysis)
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
Ты анализируешь daily meeting для создания задачи дня для ПМа.

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
Сформировать не протокол встречи, а операционный PM daily checklist:
что ПМ должен сегодня проверить, дожать, синхронизировать, зафиксировать, проконтролировать и не потерять.

Выдели:
1. Главные темы дня.
2. Явные задачи по людям.
3. Неявные PM-follow-up задачи.
4. Зависимости и последовательности действий.
5. Пункты, требующие подтверждения или ручной проверки.
6. То, что в работе и не закрыто.
7. Реальные блокеры и риски.
8. Темы, которые легко потерять, но по ним нужен follow-up сегодня.

Типовые PM-follow-up действия:
- проверить статус;
- синхронизироваться;
- дождаться результата;
- проконтролировать зависимость;
- собрать материалы;
- посмотреть демо;
- зафиксировать решение;
- запросить оценку;
- уточнить владельца;
- обновить задачу;
- проверить, что проблема закрыта не только со слов, но и по факту.

Ограничения объема и дублей:
- `pm_checklist` — главный исполнимый список ПМа, максимум 12 пунктов.
- `needs_verification` — только отдельные ручные проверки, максимум 7 пунктов; не копируй туда дословно пункты из `pm_checklist`.
- `dont_lose_today` — короткие напоминания, максимум 6 пунктов; не копируй туда то же действие, что уже есть в `pm_checklist`.
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
  "focus_of_day": ["3-7 управленческих фокусов дня"],
  "people_plan": [
    {
      "person": "имя человека",
      "task": "нормализованная задача",
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
    def _generation_prompt() -> str:
        return """
На основе структурированного анализа daily сформируй финальную задачу дня для ПМа в Markdown.

Требования:
1. Это рабочий PM-чеклист, а не протокол встречи.
2. Используй чекбоксы только для действий ПМа и ручных проверок.
3. Сохрани отдельный план по людям.
4. Отдельно выдели зависимости и последовательности.
5. Отдельно выдели пункты, требующие подтверждения.
6. Не помещай в "Сделано" то, что имеет статус in_progress, needs_verification или waiting_dependency.
7. Формулируй действия так, чтобы их можно было выполнить в течение дня.
8. Не теряй мелкие follow-up: записи, комментарии, обещания "скину", "найду", "после daily обсудим".
9. Не используй Markdown-таблицы: Bitrix плохо отображает таблицы в задачах.
10. Пиши кратко, но с достаточным контекстом для контроля дня.
11. Не дублируй одни и те же действия в "Чеклист ПМа", "Требует подтверждения" и "Не потерять сегодня".
12. Соблюдай лимиты: Фокус дня до 7 пунктов, Чеклист ПМа до 12, Требует подтверждения до 7, Не потерять сегодня до 6.

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
- **Статус:** ...
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
3. Не попали ли пункты с "вроде", "надо проверить", "посмотрим", "скину", "сегодня уточню" в done?
4. Выделены ли зависимости вида "после X -> Y -> Z"?
5. Не потеряны ли темы из transcript, которых нет или мало в summary?
6. Есть ли отдельный блок "Требует подтверждения / ручной проверки"?
7. Есть ли отдельный блок "Не закрыто / в работе"?
8. Задача читается как рабочий план дня, а не как протокол встречи?
9. Нет ли смешения статусов done / in_progress / needs_verification / waiting_dependency?
10. Все ли важные follow-up вынесены в чеклист или "Не потерять сегодня"?
11. Нет ли Markdown-таблиц?

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
        tokens = [
            token
            for token in text.split()
            if len(token) > 2
            and token
            not in {
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
            }
        ]
        return " ".join(tokens)

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
        return notes
