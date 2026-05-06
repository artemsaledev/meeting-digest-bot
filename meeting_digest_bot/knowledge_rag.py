from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Protocol

import requests


DEFAULT_EMBEDDINGS_MODEL = "text-embedding-3-small"
DEFAULT_LLM_MODEL = "gpt-4.1-mini"
DEFAULT_MIN_ANSWER_SCORE = 0.18
ANSWER_MODES = {
    "general": "Give a concise operational answer.",
    "user_instruction": "Write practical user-facing instructions with steps and caveats.",
    "technical_spec": "Write a technical specification with requirements, constraints, and acceptance criteria.",
    "support_answer": "Write a support-style answer that names the likely cause, action, and evidence.",
}


class EmbeddingClient(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


class ChatClient(Protocol):
    def answer(
        self,
        *,
        query: str,
        contexts: list[dict[str, Any]],
        model: str | None = None,
        answer_mode: str = "general",
    ) -> str:
        ...


@dataclass(slots=True)
class ExternalAIClient:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    embeddings_model: str = DEFAULT_EMBEDDINGS_MODEL
    llm_model: str = DEFAULT_LLM_MODEL
    timeout_seconds: int = 120

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._request(
            "POST",
            "/embeddings",
            {
                "model": self.embeddings_model,
                "input": texts,
            },
        )
        rows = sorted(data.get("data") or [], key=lambda item: int(item.get("index", 0)))
        return [[float(value) for value in row.get("embedding", [])] for row in rows]

    def answer(
        self,
        *,
        query: str,
        contexts: list[dict[str, Any]],
        model: str | None = None,
        answer_mode: str = "general",
    ) -> str:
        mode_instruction = ANSWER_MODES.get(answer_mode, ANSWER_MODES["general"])
        context_text = "\n\n".join(
            [
                "\n".join(
                    [
                        f"[{idx}] {item.get('title')} ({item.get('object_id')}, {item.get('chunk_id')})",
                        str(item.get("content") or "").strip(),
                    ]
                )
                for idx, item in enumerate(contexts, start=1)
            ]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You answer using only the provided company knowledge contexts. "
                    f"{mode_instruction} "
                    "Always include compact citations as [object_id/chunk_id]. "
                    "If the context is insufficient, say what is missing instead of guessing."
                ),
            },
            {
                "role": "user",
                "content": f"Question:\n{query}\n\nCompany knowledge contexts:\n{context_text}",
            },
        ]
        data = self._request(
            "POST",
            "/chat/completions",
            {
                "model": model or self.llm_model,
                "messages": messages,
                "temperature": 0.1,
            },
        )
        choices = data.get("choices") or []
        if not choices:
            return "No answer returned by external LLM."
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip() or "No answer returned by external LLM."

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.request(
            method,
            self.base_url.rstrip("/") + path,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"External AI API {method} {path} failed: {response.status_code} {response.text[:1000]}")
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}


class KnowledgeVectorStore:
    def __init__(self, root: Path, *, db_path: Path | None = None, embeddings_model: str = DEFAULT_EMBEDDINGS_MODEL) -> None:
        self.root = Path(root)
        self.db_path = db_path or (self.root / "indexes" / "knowledge_vectors.sqlite")
        self.embeddings_model = embeddings_model

    def build(self, *, client: EmbeddingClient, force: bool = False, batch_size: int = 32) -> dict[str, Any]:
        chunks = self._load_chunks()
        self._init_db()
        self._delete_stale_embeddings({str(chunk.get("chunk_id")) for chunk in chunks if chunk.get("chunk_id")})
        existing = self._existing_hashes()
        to_embed = []
        for chunk in chunks:
            content = str(chunk.get("content") or "")
            content_hash = self._hash_text(content)
            if not force and existing.get(str(chunk.get("chunk_id"))) == content_hash:
                continue
            to_embed.append({**chunk, "content_hash": content_hash})

        embedded_count = 0
        for start in range(0, len(to_embed), batch_size):
            batch = to_embed[start : start + batch_size]
            vectors = client.embed_texts([str(item.get("content") or "") for item in batch])
            if len(vectors) != len(batch):
                raise RuntimeError(f"Embedding API returned {len(vectors)} vectors for {len(batch)} chunks.")
            self._upsert_embeddings(batch, vectors)
            embedded_count += len(batch)
            self._write_usage(
                {
                    "operation": "build_embeddings",
                    "model": self.embeddings_model,
                    "texts_count": len(batch),
                    "chars": sum(len(str(item.get("content") or "")) for item in batch),
                    "estimated_tokens": sum(self._estimate_tokens(str(item.get("content") or "")) for item in batch),
                }
            )

        return {
            "ready": True,
            "db_path": str(self.db_path),
            "chunks_total": len(chunks),
            "embedded_count": embedded_count,
            "reused_count": len(chunks) - embedded_count,
            "model": self.embeddings_model,
        }

    def search(
        self,
        query: str,
        *,
        client: EmbeddingClient,
        limit: int = 5,
        system: str | None = None,
        object_type: str | None = None,
        threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        self._init_db()
        query_vector = client.embed_texts([query])[0]
        self._write_usage(
            {
                "operation": "search_query_embedding",
                "model": self.embeddings_model,
                "texts_count": 1,
                "chars": len(query),
                "estimated_tokens": self._estimate_tokens(query),
            }
        )
        query_tokens = self._tokens(query)
        rows = self._all_embeddings()
        results = []
        for row in rows:
            metadata = row["metadata"]
            if not self._metadata_matches(metadata, system=system, object_type=object_type):
                continue
            vector_score = self._cosine(query_vector, row["embedding"])
            lexical_score = self._lexical_score(query_tokens, str(metadata.get("content") or ""))
            score = vector_score + (0.15 * lexical_score)
            if score < threshold:
                continue
            results.append({**metadata, "score": score, "vector_score": vector_score, "lexical_score": lexical_score})
        results.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return results[:limit]

    def answer(
        self,
        query: str,
        *,
        embedding_client: EmbeddingClient,
        chat_client: ChatClient,
        limit: int = 5,
        model: str | None = None,
        system: str | None = None,
        object_type: str | None = None,
        threshold: float = 0.0,
        min_score: float = DEFAULT_MIN_ANSWER_SCORE,
        answer_mode: str = "general",
    ) -> dict[str, Any]:
        contexts = self.search(
            query,
            client=embedding_client,
            limit=limit,
            system=system,
            object_type=object_type,
            threshold=threshold,
        )
        if not contexts:
            return {
                "answer": "No relevant knowledge chunks found.",
                "sources": [],
                "mode": answer_mode,
                "confidence": "low",
            }
        top_score = float(contexts[0].get("score") or 0.0)
        if top_score < min_score:
            return {
                "answer": (
                    "Context score is below the configured threshold, so I will not synthesize an answer. "
                    "Narrow the query or lower min_score if this is intentional."
                ),
                "sources": contexts,
                "mode": answer_mode,
                "confidence": "low",
                "top_score": top_score,
            }
        answer = chat_client.answer(query=query, contexts=contexts, model=model, answer_mode=answer_mode)
        self._write_usage(
            {
                "operation": "answer",
                "model": model or getattr(chat_client, "llm_model", None) or DEFAULT_LLM_MODEL,
                "contexts_count": len(contexts),
                "chars": len(query) + sum(len(str(item.get("content") or "")) for item in contexts),
                "estimated_tokens": self._estimate_tokens(query)
                + sum(self._estimate_tokens(str(item.get("content") or "")) for item in contexts),
                "answer_mode": answer_mode,
            }
        )
        return {
            "answer": answer,
            "sources": contexts,
            "mode": answer_mode,
            "confidence": "medium" if top_score >= 0.2 else "low",
            "top_score": top_score,
        }

    def stats(self) -> dict[str, Any]:
        self._init_db()
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            model_rows = conn.execute("SELECT model, COUNT(*) FROM embeddings GROUP BY model").fetchall()
        finally:
            conn.close()
        return {
            "ready": count > 0,
            "db_path": str(self.db_path),
            "chunks_embedded": int(count),
            "models": {str(model): int(total) for model, total in model_rows},
            "usage": self.usage_stats(),
        }

    def usage_stats(self) -> dict[str, Any]:
        path = self.root / "logs" / "rag_usage.jsonl"
        if not path.exists():
            return {"events": 0, "estimated_tokens": 0, "by_operation": {}, "by_model": {}}
        events = 0
        estimated_tokens = 0
        by_operation: dict[str, int] = {}
        by_model: dict[str, int] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            events += 1
            tokens = int(item.get("estimated_tokens") or 0)
            estimated_tokens += tokens
            operation = str(item.get("operation") or "unknown")
            model = str(item.get("model") or "unknown")
            by_operation[operation] = by_operation.get(operation, 0) + tokens
            by_model[model] = by_model.get(model, 0) + tokens
        return {
            "events": events,
            "estimated_tokens": estimated_tokens,
            "by_operation": by_operation,
            "by_model": by_model,
        }

    def _load_chunks(self) -> list[dict[str, Any]]:
        index_path = self.root / "indexes" / "knowledge_chunks.json"
        if not index_path.exists():
            raise FileNotFoundError(f"Chunk index is not found: {index_path}")
        data = json.loads(index_path.read_text(encoding="utf-8"))
        chunks = data.get("chunks") or []
        return [item for item in chunks if isinstance(item, dict) and item.get("chunk_id") and item.get("content")]

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT PRIMARY KEY,
                    object_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_object_id ON embeddings(object_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model)")
            conn.commit()
        finally:
            conn.close()

    def _existing_hashes(self) -> dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT chunk_id, content_hash FROM embeddings WHERE model = ?",
                (self.embeddings_model,),
            ).fetchall()
        finally:
            conn.close()
        return {str(chunk_id): str(content_hash) for chunk_id, content_hash in rows}

    def _delete_stale_embeddings(self, current_chunk_ids: set[str]) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT chunk_id FROM embeddings WHERE model = ?",
                (self.embeddings_model,),
            ).fetchall()
            stale = [str(chunk_id) for (chunk_id,) in rows if str(chunk_id) not in current_chunk_ids]
            if stale:
                conn.executemany(
                    "DELETE FROM embeddings WHERE model = ? AND chunk_id = ?",
                    [(self.embeddings_model, chunk_id) for chunk_id in stale],
                )
                conn.commit()
                self._write_usage(
                    {
                        "operation": "delete_stale_embeddings",
                        "model": self.embeddings_model,
                        "chunks_count": len(stale),
                        "estimated_tokens": 0,
                    }
                )
        finally:
            conn.close()

    def _upsert_embeddings(self, chunks: list[dict[str, Any]], vectors: list[list[float]]) -> None:
        now = datetime.now(UTC).isoformat()
        rows = []
        for chunk, vector in zip(chunks, vectors):
            metadata = {
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "object_id": str(chunk.get("object_id") or ""),
                "title": str(chunk.get("title") or ""),
                "path": str(chunk.get("path") or ""),
                "content": str(chunk.get("content") or ""),
                "source_event_ids": chunk.get("source_event_ids") or [],
            }
            metadata["object_type"] = self._infer_object_type(metadata["object_id"])
            metadata["system"] = self._infer_system(metadata["object_id"], metadata["path"])
            rows.append(
                (
                    metadata["chunk_id"],
                    metadata["object_id"],
                    metadata["title"],
                    metadata["path"],
                    str(chunk.get("content_hash") or ""),
                    metadata["content"],
                    json.dumps(vector, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                    self.embeddings_model,
                    now,
                )
            )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO embeddings (
                    chunk_id, object_id, title, path, content_hash, content,
                    embedding_json, metadata_json, model, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    object_id=excluded.object_id,
                    title=excluded.title,
                    path=excluded.path,
                    content_hash=excluded.content_hash,
                    content=excluded.content,
                    embedding_json=excluded.embedding_json,
                    metadata_json=excluded.metadata_json,
                    model=excluded.model,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _all_embeddings(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT metadata_json, embedding_json FROM embeddings WHERE model = ?",
                (self.embeddings_model,),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for metadata_json, embedding_json in rows:
            metadata = json.loads(metadata_json)
            embedding = [float(value) for value in json.loads(embedding_json)]
            result.append({"metadata": metadata, "embedding": embedding})
        return result

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if not left_norm or not right_norm:
            return 0.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[\wА-Яа-яІіЇїЄєҐґ]{3,}", str(text).casefold(), flags=re.UNICODE)}

    @classmethod
    def _lexical_score(cls, query_tokens: set[str], content: str) -> float:
        if not query_tokens:
            return 0.0
        content_tokens = cls._tokens(content)
        if not content_tokens:
            return 0.0
        return len(query_tokens & content_tokens) / len(query_tokens)

    @staticmethod
    def _infer_object_type(object_id: str) -> str:
        if object_id.startswith("task_case__"):
            return "task_case"
        if object_id.startswith("system__"):
            return "system"
        if object_id.startswith("feature__"):
            return "feature"
        if object_id.startswith("instruction__"):
            return "instruction"
        return "unknown"

    @staticmethod
    def _infer_system(object_id: str, path: str) -> str:
        parts = object_id.split("__")
        if len(parts) >= 2 and parts[0] in {"system", "feature", "instruction"}:
            return parts[1]
        path_lower = path.casefold()
        if "bitrix" in object_id.casefold() or "bitrix" in path_lower:
            return "bitrix"
        if "aicallorder" in object_id.casefold() or "aicallorder" in path_lower:
            return "aicallorder"
        return "unknown"

    @staticmethod
    def _metadata_matches(metadata: dict[str, Any], *, system: str | None, object_type: str | None) -> bool:
        if system and str(metadata.get("system") or "").casefold() != system.casefold():
            return False
        if object_type and str(metadata.get("object_type") or "").casefold() != object_type.casefold():
            return False
        return True

    def _write_usage(self, item: dict[str, Any]) -> None:
        path = self.root / "logs" / "rag_usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {"created_at": datetime.now(UTC).isoformat(), **item}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, int(len(str(text or "")) / 4))


def client_from_env(env: dict[str, str], *, require_llm: bool = False) -> ExternalAIClient | None:
    api_key = env.get("KNOWLEDGE_RAG_API_KEY") or env.get("OPENAI_API_KEY") or env.get("LLM_API_KEY")
    if not api_key:
        return None
    base_url = env.get("KNOWLEDGE_RAG_BASE_URL") or env.get("OPENAI_BASE_URL") or env.get("LLM_BASE_URL") or "https://api.openai.com/v1"
    embeddings_model = env.get("KNOWLEDGE_EMBEDDINGS_MODEL") or env.get("OPENAI_EMBEDDINGS_MODEL") or DEFAULT_EMBEDDINGS_MODEL
    llm_model = env.get("KNOWLEDGE_RAG_LLM_MODEL") or env.get("LLM_MODEL") or env.get("OPENAI_MODEL") or DEFAULT_LLM_MODEL
    timeout = int(env.get("KNOWLEDGE_RAG_TIMEOUT_SECONDS") or env.get("LLM_TIMEOUT_SECONDS") or "120")
    if require_llm and not llm_model:
        return None
    return ExternalAIClient(
        api_key=api_key,
        base_url=base_url,
        embeddings_model=embeddings_model,
        llm_model=llm_model,
        timeout_seconds=timeout,
    )
