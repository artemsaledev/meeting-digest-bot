from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import requests


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_RICH_TEXT_LIMIT = 1900
NOTION_BLOCK_BATCH_LIMIT = 100


@dataclass(slots=True)
class NotionTarget:
    parent_id: str
    parent_type: str
    api_version: str

    @classmethod
    def from_env(cls, env: dict[str, str], *, key: str = "TASK_CASES") -> "NotionTarget | None":
        key = key.upper()
        data_source_id = env.get(f"NOTION_DATA_SOURCE_{key}") or env.get(f"NOTION_{key}_DATA_SOURCE_ID")
        if data_source_id:
            return cls(parent_id=data_source_id, parent_type="data_source_id", api_version=env.get("NOTION_API_VERSION", "2025-09-03"))
        database_id = env.get(f"NOTION_DB_{key}") or env.get(f"NOTION_{key}_DATABASE_ID")
        if database_id:
            # Legacy database endpoints remain practical for existing single-source databases.
            return cls(parent_id=database_id, parent_type="database_id", api_version=env.get("NOTION_API_VERSION", "2022-06-28"))
        return None


class NotionKnowledgeClient:
    def __init__(self, *, token: str, target: NotionTarget) -> None:
        self.token = token
        self.target = target

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.target.api_version,
            "Content-Type": "application/json",
        }

    def upsert_projection(self, projection_path: Path) -> dict[str, Any]:
        projection = self._read_json(projection_path)
        properties = projection.get("properties") or {}
        database = str(projection.get("database") or "")
        object_id = str(properties.get("ID") or "").strip()
        title = str(properties.get("Title") or object_id or projection_path.stem).strip()
        page = self.find_page_by_object_id(object_id) if object_id else None
        page_properties = self._page_properties(properties, title=title, database=database)
        markdown = str(projection.get("content_markdown") or "")
        blocks = self.markdown_to_blocks(markdown)
        if page:
            page_id = page["id"]
            self.update_page(page_id, page_properties)
            live_markdown = self.blocks_to_markdown(self.list_block_children(page_id))
            if self._normalize_markdown(live_markdown) == self._normalize_markdown(markdown):
                return {"action": "update_page_metadata", "page_id": page_id, "url": page.get("url"), "object_id": object_id}
            self.replace_page_blocks(page_id, blocks)
            return {"action": "update_page_body", "page_id": page_id, "url": page.get("url"), "object_id": object_id}
        created = self.create_page(page_properties, blocks)
        return {"action": "create_page", "page_id": created.get("id"), "url": created.get("url"), "object_id": object_id}

    def find_page_by_object_id(self, object_id: str) -> dict[str, Any] | None:
        if not object_id:
            return None
        body = {
            "filter": {
                "property": "ID",
                "rich_text": {"equals": object_id},
            },
            "page_size": 1,
        }
        if self.target.parent_type == "data_source_id":
            data = self._request("POST", f"/data_sources/{self.target.parent_id}/query", json=body)
        else:
            data = self._request("POST", f"/databases/{self.target.parent_id}/query", json=body)
        results = data.get("results") or []
        return results[0] if results else None

    def query_pages(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if cursor:
                body["start_cursor"] = cursor
            if self.target.parent_type == "data_source_id":
                data = self._request("POST", f"/data_sources/{self.target.parent_id}/query", json=body)
            else:
                data = self._request("POST", f"/databases/{self.target.parent_id}/query", json=body)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def page_to_projection(self, page: dict[str, Any], *, database: str) -> dict[str, Any]:
        properties = self._plain_properties(page.get("properties") or {})
        blocks = self.list_block_children(str(page["id"]))
        return {
            "database": database,
            "page_id": page.get("id"),
            "url": page.get("url"),
            "properties": properties,
            "content_markdown": self.blocks_to_markdown(blocks),
        }

    def create_page(self, properties: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "parent": {self.target.parent_type: self.target.parent_id},
            "properties": properties,
        }
        if self.target.parent_type == "data_source_id":
            body["content"] = blocks[:NOTION_BLOCK_BATCH_LIMIT]
        else:
            body["children"] = blocks[:NOTION_BLOCK_BATCH_LIMIT]
        page = self._request("POST", "/pages", json=body)
        remaining = blocks[NOTION_BLOCK_BATCH_LIMIT:]
        if remaining:
            self.append_blocks(str(page["id"]), remaining)
        return page

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def replace_page_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        for child in self.list_block_children(page_id):
            block_id = child.get("id")
            if block_id:
                self.archive_block(str(block_id))
        self.append_blocks(page_id, blocks)

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._request("GET", path)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def archive_block(self, block_id: str) -> dict[str, Any]:
        return self._request("PATCH", f"/blocks/{block_id}", json={"archived": True})

    def append_blocks(self, block_id: str, blocks: list[dict[str, Any]]) -> None:
        for start in range(0, len(blocks), NOTION_BLOCK_BATCH_LIMIT):
            batch = blocks[start : start + NOTION_BLOCK_BATCH_LIMIT]
            self._request("PATCH", f"/blocks/{block_id}/children", json={"children": batch})

    @classmethod
    def markdown_to_blocks(cls, markdown: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        in_code = False
        code_lines: list[str] = []
        for raw_line in markdown.splitlines():
            line = raw_line.rstrip()
            if line.strip().startswith("```"):
                if in_code:
                    blocks.extend(cls._code_blocks("\n".join(code_lines)))
                    code_lines = []
                    in_code = False
                else:
                    in_code = True
                continue
            if in_code:
                code_lines.append(line)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# "):
                blocks.extend(cls._text_blocks("heading_1", stripped[2:].strip()))
            elif stripped.startswith("## "):
                blocks.extend(cls._text_blocks("heading_2", stripped[3:].strip()))
            elif stripped.startswith("### "):
                blocks.extend(cls._text_blocks("heading_3", stripped[4:].strip()))
            elif stripped.startswith("- "):
                blocks.extend(cls._text_blocks("bulleted_list_item", stripped[2:].strip()))
            elif stripped.startswith("> "):
                blocks.extend(cls._text_blocks("quote", stripped[2:].strip()))
            else:
                blocks.extend(cls._text_blocks("paragraph", stripped))
        if code_lines:
            blocks.extend(cls._code_blocks("\n".join(code_lines)))
        return blocks or cls._text_blocks("paragraph", "No content.")

    @classmethod
    def blocks_to_markdown(cls, blocks: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for block in blocks:
            block_type = str(block.get("type") or "")
            payload = block.get(block_type) or {}
            text = cls._rich_text_plain(payload.get("rich_text") or [])
            if not text and block_type != "divider":
                continue
            if block_type == "heading_1":
                lines.extend([f"# {text}", ""])
            elif block_type == "heading_2":
                lines.extend([f"## {text}", ""])
            elif block_type == "heading_3":
                lines.extend([f"### {text}", ""])
            elif block_type == "bulleted_list_item":
                lines.append(f"- {text}")
            elif block_type == "numbered_list_item":
                lines.append(f"1. {text}")
            elif block_type == "to_do":
                checked = "x" if payload.get("checked") else " "
                lines.append(f"- [{checked}] {text}")
            elif block_type == "quote":
                lines.extend([f"> {text}", ""])
            elif block_type == "code":
                language = payload.get("language") or ""
                lines.extend([f"```{language}", text, "```", ""])
            elif block_type == "divider":
                lines.extend(["---", ""])
            else:
                lines.extend([text, ""])
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _text_blocks(cls, block_type: str, text: str) -> list[dict[str, Any]]:
        chunks = cls._split_text(text)
        return [
            {
                "object": "block",
                "type": block_type,
                block_type: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            }
            for chunk in chunks
        ]

    @classmethod
    def _code_blocks(cls, text: str) -> list[dict[str, Any]]:
        return [
            {
                "object": "block",
                "type": "code",
                "code": {
                    "language": "plain text",
                    "rich_text": [{"type": "text", "text": {"content": chunk}}],
                },
            }
            for chunk in cls._split_text(text)
        ]

    @staticmethod
    def _split_text(text: str) -> list[str]:
        cleaned = text or ""
        if not cleaned:
            return [""]
        return [cleaned[start : start + NOTION_RICH_TEXT_LIMIT] for start in range(0, len(cleaned), NOTION_RICH_TEXT_LIMIT)]

    @staticmethod
    def _page_properties(properties: dict[str, Any], *, title: str, database: str = "") -> dict[str, Any]:
        result = {
            "Title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
            "ID": {"rich_text": [{"type": "text", "text": {"content": str(properties.get("ID") or "")[:2000]}}]},
        }
        if database and database != "Task Cases":
            return result
        text_props = {
            "Type": "select",
            "Status": "select",
            "System": "select",
            "Feature Area": "rich_text",
        }
        for key, prop_type in text_props.items():
            value = str(properties.get(key) or "").strip()
            if not value:
                continue
            if prop_type == "select":
                result[key] = {"select": {"name": value[:100]}}
            else:
                result[key] = {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}
        tags = properties.get("Tags") or []
        if isinstance(tags, list) and tags:
            result["Tags"] = {"multi_select": [{"name": str(tag)[:100]} for tag in tags if str(tag).strip()]}
        tasks = properties.get("Bitrix Tasks") or []
        if isinstance(tasks, list) and tasks:
            result["Bitrix Tasks"] = {"rich_text": [{"type": "text", "text": {"content": ", ".join(map(str, tasks))[:2000]}}]}
        return result

    @classmethod
    def _plain_properties(cls, properties: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, payload in properties.items():
            prop_type = payload.get("type")
            value = payload.get(prop_type) if prop_type else None
            if prop_type == "title":
                result[name] = cls._rich_text_plain(value or [])
            elif prop_type == "rich_text":
                result[name] = cls._rich_text_plain(value or [])
            elif prop_type == "select":
                result[name] = (value or {}).get("name") if isinstance(value, dict) else ""
            elif prop_type == "multi_select":
                result[name] = [str(item.get("name")) for item in value or [] if isinstance(item, dict)]
            elif prop_type == "date":
                result[name] = (value or {}).get("start") if isinstance(value, dict) else ""
            elif prop_type == "number":
                result[name] = value
            elif prop_type == "checkbox":
                result[name] = bool(value)
            else:
                result[name] = str(value or "")
        return result

    @staticmethod
    def _rich_text_plain(values: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            text = item.get("plain_text")
            if text is None and isinstance(item.get("text"), dict):
                text = item["text"].get("content")
            parts.append(str(text or ""))
        return "".join(parts).strip()

    @staticmethod
    def _normalize_markdown(value: str) -> str:
        return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = requests.request(
                    method,
                    NOTION_API_BASE + path,
                    headers=self.headers,
                    json=json,
                    timeout=45,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = RuntimeError(f"Notion API {method} {path} failed: {response.status_code} {response.text[:1000]}")
                    time.sleep(min(2 * attempt, 8))
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(f"Notion API {method} {path} failed: {response.status_code} {response.text[:1000]}")
                return response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                time.sleep(min(2 * attempt, 8))
        raise RuntimeError(f"Notion API {method} {path} failed after retries: {last_error}")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            parsed = __import__("json").loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
