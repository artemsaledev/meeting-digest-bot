from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


WORD_RE = re.compile(r"[\wА-Яа-яЁёІіЇїЄєҐґ]+", re.UNICODE)


@dataclass(slots=True)
class TaskMatch:
    task_id: int
    title: str
    score: float
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "score": round(self.score, 3),
            "url": self.url,
        }


def find_task_matches(
    *,
    draft_title: str,
    tasks: list[dict[str, Any]],
    group_id: int,
    threshold: float,
    limit: int = 5,
) -> list[dict[str, Any]]:
    matches: list[TaskMatch] = []
    for raw_task in tasks:
        task_id = _task_id(raw_task)
        title = _task_title(raw_task)
        if not task_id or not title:
            continue
        score = _title_score(draft_title, title)
        if score >= threshold:
            matches.append(
                TaskMatch(
                    task_id=task_id,
                    title=title,
                    score=score,
                    url=f"https://totiscrm.com/workgroups/group/{group_id}/tasks/task/view/{task_id}/",
                )
            )
    matches.sort(key=lambda item: item.score, reverse=True)
    return [item.as_dict() for item in matches[:limit]]


def _task_id(raw_task: dict[str, Any]) -> int | None:
    value = raw_task.get("id") or raw_task.get("ID")
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_title(raw_task: dict[str, Any]) -> str:
    return str(raw_task.get("title") or raw_task.get("TITLE") or "").strip()


def _title_score(left: str, right: str) -> float:
    left_norm = _normalize(left)
    right_norm = _normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(_tokens(left_norm))
    right_tokens = set(_tokens(right_norm))
    if not left_tokens or not right_tokens:
        return sequence_score
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    subset = len(left_tokens & right_tokens) / max(min(len(left_tokens), len(right_tokens)), 1)
    return (sequence_score * 0.45) + (overlap * 0.35) + (subset * 0.20)


def _normalize(value: str) -> str:
    text = value.casefold()
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokens(value: str) -> list[str]:
    return [token for token in WORD_RE.findall(value) if len(token) > 2]
