from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class BitrixClientError(RuntimeError):
    pass


@dataclass(slots=True)
class BitrixClient:
    legacy_base_url: str
    modern_base_url: str
    use_json_suffix: bool = False
    timeout_seconds: int = 30

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.legacy_base_url:
            raise BitrixClientError("BITRIX_WEBHOOK_BASE is not configured.")

        payload = payload or {}
        url = self._build_url(method)
        response = requests.post(url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise BitrixClientError(f"{data.get('error')}: {data.get('error_description')}")
        return data

    def _build_url(self, method: str) -> str:
        if method.startswith("tasks.task.chat."):
            base = self.modern_base_url or self.legacy_base_url
            return base + method
        suffix = ".json" if self.use_json_suffix else ""
        return self.legacy_base_url + method + suffix

    def list_tasks(
        self,
        *,
        filter_data: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        select: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            return self.call(
                "tasks.task.list",
                {
                    "filter": filter_data or {},
                    "order": order or {"ID": "desc"},
                    "select": select or ["ID", "TITLE", "GROUP_ID", "DESCRIPTION"],
                },
            )
        except Exception:
            return self.call(
                "task.items.getlist",
                {
                    "filter": filter_data or {},
                    "order": order or {"ID": "desc"},
                    "select": select or ["ID", "TITLE", "GROUP_ID", "DESCRIPTION"],
                },
            )

    def get_task(self, task_id: int, select: list[str] | None = None) -> dict[str, Any]:
        return self.call(
            "tasks.task.get",
            {
                "taskId": task_id,
                "select": select or ["ID", "TITLE", "DESCRIPTION", "GROUP_ID", "CHAT_ID"],
            },
        )

    def create_task(self, fields: dict[str, Any]) -> int:
        try:
            data = self.call("tasks.task.add", {"fields": fields})
            result = data.get("result") or {}
            if isinstance(result, dict):
                item = result.get("task") or result.get("item") or result
                task_id = item.get("id") or item.get("ID")
            else:
                task_id = result
        except Exception:
            data = self.call("task.item.add", {"fields": fields})
            task_id = data.get("result")
        if not task_id:
            raise BitrixClientError("Task creation did not return task id.")
        return int(task_id)

    def update_task(self, task_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.call("tasks.task.update", {"taskId": task_id, "fields": fields})
        except Exception:
            return self.call("task.item.update", {"taskId": task_id, "fields": fields})

    def add_checklist_item(
        self,
        task_id: int,
        title: str,
        parent_id: int = 0,
        members: list[int] | None = None,
    ) -> int | None:
        fields: dict[str, Any] = {
            "TITLE": title,
            "PARENT_ID": parent_id,
            "IS_COMPLETE": "N",
        }
        if members:
            fields["MEMBERS"] = [int(member) for member in members]
        data = self.call(
            "task.checklistitem.add",
            {
                "TASKID": task_id,
                "FIELDS": fields,
            },
        )
        result = data.get("result")
        if isinstance(result, dict):
            checklist_id = result.get("ID") or result.get("id")
        else:
            checklist_id = result
        return int(checklist_id) if checklist_id else None

    def list_checklist_items(self, task_id: int) -> list[dict[str, Any]]:
        data = self.call("task.checklistitem.getlist", {"TASKID": task_id})
        result = data.get("result") or []
        return list(result) if isinstance(result, list) else []

    def add_checklist_group(self, task_id: int, title: str, items: list[str]) -> None:
        parent_id = self.add_checklist_item(task_id, title, parent_id=0)
        for item in items:
            if item.strip():
                self.add_checklist_item(task_id, item.strip(), parent_id=parent_id or 0)

    def add_checklist_group_deduped(self, task_id: int, title: str, items: list[str]) -> dict[str, Any]:
        existing = self.list_checklist_items(task_id)
        diff = self.preview_checklist_group_dedupe(existing, title, items)
        parent_id = int(diff.get("parent_id") or 0)

        if not parent_id:
            created_parent_id = self.add_checklist_item(task_id, title, parent_id=0)
            parent_id = created_parent_id or 0

        existing_item_titles = set(diff.get("existing_item_titles") or [])
        added = 0
        skipped = 0
        for item in items:
            text = item.strip()
            if not text:
                continue
            normalized = self._normalize_checklist_text(text)
            if normalized in existing_item_titles:
                skipped += 1
                continue
            self.add_checklist_item(task_id, text, parent_id=parent_id)
            existing_item_titles.add(normalized)
            added += 1

        return {
            "group": title,
            "parent_id": parent_id,
            "added": added,
            "skipped": skipped,
        }

    def preview_checklist_group_dedupe(
        self,
        existing: list[dict[str, Any]],
        title: str,
        items: list[str],
    ) -> dict[str, Any]:
        normalized_title = self._normalize_checklist_text(title)
        parent_id = 0
        existing_item_titles: set[str] = set()

        for row in existing:
            row_title = self._normalize_checklist_text(str(row.get("TITLE") or ""))
            row_parent_id = str(row.get("PARENT_ID") or "0")
            if row_parent_id in {"0", ""} and row_title == normalized_title:
                parent_id = int(row.get("ID") or 0)
                break

        if parent_id:
            for row in existing:
                if str(row.get("PARENT_ID") or "0") == str(parent_id):
                    existing_item_titles.add(self._normalize_checklist_text(str(row.get("TITLE") or "")))

        would_add = 0
        would_skip = 0
        seen = set(existing_item_titles)
        for item in items:
            text = item.strip()
            if not text:
                continue
            normalized = self._normalize_checklist_text(text)
            if normalized in seen:
                would_skip += 1
                continue
            seen.add(normalized)
            would_add += 1

        return {
            "group": title,
            "parent_id": parent_id,
            "would_add": would_add,
            "would_skip": would_skip,
            "existing_item_titles": sorted(existing_item_titles),
        }

    def send_task_comment(self, task_id: int, text: str, author_id: int | None = None) -> dict[str, Any]:
        if author_id:
            return self.call(
                "task.commentitem.add",
                {
                    "TASKID": task_id,
                    "FIELDS": {
                        "POST_MESSAGE": text,
                        "AUTHOR_ID": author_id,
                    },
                },
            )
        try:
            return self.call(
                "tasks.task.chat.message.send",
                {
                    "fields": {
                        "taskId": task_id,
                        "text": text,
                    }
                },
            )
        except Exception:
            return self.call(
                "task.comment.add",
                {
                    "TASKID": task_id,
                    "COMMENTTEXT": text,
                },
            )

    @staticmethod
    def _normalize_checklist_text(value: str) -> str:
        return " ".join(value.strip().casefold().split())
