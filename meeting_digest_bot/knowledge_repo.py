from __future__ import annotations

from datetime import UTC, datetime
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
import zipfile

from pydantic import BaseModel, Field

from .kb_intake import KnowledgeIntake, KnowledgeObject
from .notion_kb import NotionKnowledgeClient, NotionTarget


KNOWLEDGE_REPO_DIRS = [
    "knowledge/task_cases",
    "knowledge/systems",
    "knowledge/features",
    "knowledge/instructions",
    "knowledge/prompts",
    "knowledge/drafts",
    "meta",
    "meta/notion",
    "indexes",
    "logs",
    "exports",
]

KNOWLEDGE_OBJECT_DIRS = {
    "task_cases": "knowledge/task_cases",
    "systems": "knowledge/systems",
    "features": "knowledge/features",
    "instructions": "knowledge/instructions",
}

NOTION_TARGET_KEYS = {
    "Task Cases": "TASK_CASES",
    "Systems": "SYSTEMS",
    "Features": "FEATURES",
    "Instructions": "INSTRUCTIONS",
}


class KnowledgeRepoResult(BaseModel):
    root: str
    created_dirs: list[str] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)
    objects_count: int = 0
    object_ids: list[str] = Field(default_factory=list)


class KnowledgeSearchResult(BaseModel):
    object_id: str
    title: str
    path: str
    score: int
    snippets: list[str] = Field(default_factory=list)


class KnowledgeRevisionProposal(BaseModel):
    object_id: str
    correction: str
    proposal_path: str
    metadata_path: str
    source_path: str
    created_at: str
    status: str = "draft"


class KnowledgeGeneratedDocument(BaseModel):
    object_id: str
    kind: str
    output_path: str
    created_at: str


class KnowledgeNotionResult(BaseModel):
    mode: str
    ready: bool
    missing_env: list[str] = Field(default_factory=list)
    planned_pages: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""


class KnowledgeNotionImportResult(BaseModel):
    mode: str = "import"
    ready: bool
    missing_env: list[str] = Field(default_factory=list)
    scanned_pages: int = 0
    proposals_count: int = 0
    planned_pages: list[dict[str, Any]] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)
    message: str = ""


class KnowledgeQualityIssue(BaseModel):
    object_id: str
    object_type: str
    path: str
    severity: str
    message: str


class KnowledgeQualityReport(BaseModel):
    generated_at: str
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    counts_by_type: dict[str, int] = Field(default_factory=dict)
    issues: list[KnowledgeQualityIssue] = Field(default_factory=list)


class KnowledgeCatalogObject(BaseModel):
    object_id: str
    object_type: str
    title: str
    status: str = "draft"
    system: str = "unknown"
    feature_area: str = ""
    summary: str = ""
    source_task_cases: list[str] = Field(default_factory=list)
    linked_bitrix_tasks: list[int] = Field(default_factory=list)
    source_tags: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class KnowledgeRepository:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def init(self) -> KnowledgeRepoResult:
        written: list[str] = []
        created_dirs: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)
        for rel in KNOWLEDGE_REPO_DIRS:
            path = self.root / rel
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))
            keep = path / ".gitkeep"
            if not keep.exists():
                keep.write_text("", encoding="utf-8")
                written.append(str(keep))

        notion_mapping = self.root / "meta" / "notion_mapping.json"
        if not notion_mapping.exists():
            notion_mapping.write_text(json.dumps(self._default_notion_mapping(), ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(str(notion_mapping))

        model_policy = self.root / "meta" / "model_policy.json"
        if not model_policy.exists():
            model_policy.write_text(json.dumps(self._default_model_policy(), ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(str(model_policy))

        return KnowledgeRepoResult(root=str(self.root), created_dirs=created_dirs, written_files=written)

    def upsert_objects(self, objects: list[KnowledgeObject], *, draft: bool = False) -> KnowledgeRepoResult:
        self.init()
        written: list[str] = []
        object_ids: list[str] = []
        for item in objects:
            object_ids.append(item.object_id)
            if draft:
                written.extend(self._write_draft(item))
            else:
                merged = self._merge_with_existing(item)
                written.extend(self._write_task_case(merged))
        return KnowledgeRepoResult(
            root=str(self.root),
            written_files=written,
            objects_count=len(objects),
            object_ids=object_ids,
        )

    def derive_catalogs(self) -> KnowledgeRepoResult:
        """Build Systems, Features, and Instructions from canonical task cases."""
        self.init()
        task_cases = self._load_task_cases(include_archived=False)
        written: list[str] = []
        object_ids: list[str] = []

        systems = self._derive_systems(task_cases)
        features = self._derive_features(task_cases)
        instructions = self._derive_instructions(features)

        for item in systems:
            written.extend(self._write_catalog_object(item, directory="systems", database="Systems"))
            object_ids.append(item.object_id)
        for item in features:
            written.extend(self._write_catalog_object(item, directory="features", database="Features"))
            object_ids.append(item.object_id)
        for item in instructions:
            written.extend(self._write_catalog_object(item, directory="instructions", database="Instructions"))
            object_ids.append(item.object_id)
        written.extend(self._prune_catalog_directory("systems", {item.object_id for item in systems}))
        written.extend(self._prune_catalog_directory("features", {item.object_id for item in features}))
        written.extend(self._prune_catalog_directory("instructions", {item.object_id for item in instructions}))

        return KnowledgeRepoResult(
            root=str(self.root),
            written_files=written,
            objects_count=len(object_ids),
            object_ids=object_ids,
        )

    def build_index(self) -> KnowledgeRepoResult:
        self.init()
        docs = []
        for path in self._knowledge_json_paths():
            if path.name.endswith(".notion.json"):
                continue
            data = self._read_json(path)
            if not data:
                continue
            if self._is_archived(data):
                continue
            text = self._index_text(data)
            docs.append(
                {
                    "object_id": data.get("object_id"),
                    "title": data.get("title"),
                    "path": str(path),
                    "tokens": sorted(set(self._tokens(text))),
                    "text": text,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        index_path = self.root / "indexes" / "knowledge_index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({"documents": docs}, ensure_ascii=False, indent=2), encoding="utf-8")
        return KnowledgeRepoResult(
            root=str(self.root),
            written_files=[str(index_path)],
            objects_count=len(docs),
            object_ids=[str(doc.get("object_id")) for doc in docs if doc.get("object_id")],
        )

    def build_chunk_index(self) -> KnowledgeRepoResult:
        self.init()
        chunks = []
        for path in self._knowledge_json_paths():
            if path.name.endswith(".notion.json"):
                continue
            data = self._read_json(path)
            if not data:
                continue
            if self._is_archived(data):
                continue
            for idx, chunk in enumerate(self._chunks_for_object(data), start=1):
                chunks.append(
                    {
                        "chunk_id": f"{data.get('object_id')}__chunk_{idx:03d}",
                        "object_id": data.get("object_id"),
                        "title": data.get("title"),
                        "path": str(path),
                        "content": chunk,
                        "tokens": sorted(set(self._tokens(chunk))),
                        "source_event_ids": self._source_event_ids(data),
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
        index_path = self.root / "indexes" / "knowledge_chunks.json"
        index_path.write_text(json.dumps({"chunks": chunks}, ensure_ascii=False, indent=2), encoding="utf-8")
        return KnowledgeRepoResult(
            root=str(self.root),
            written_files=[str(index_path)],
            objects_count=len(chunks),
            object_ids=sorted({str(chunk.get("object_id")) for chunk in chunks if chunk.get("object_id")}),
        )

    def search(self, query: str, *, limit: int = 5) -> list[KnowledgeSearchResult]:
        index_path = self.root / "indexes" / "knowledge_index.json"
        if not index_path.exists():
            self.build_index()
        data = self._read_json(index_path)
        query_tokens = set(self._tokens(query))
        results: list[KnowledgeSearchResult] = []
        for doc in data.get("documents", []):
            doc_tokens = set(doc.get("tokens") or [])
            score = len(query_tokens & doc_tokens)
            if score <= 0:
                continue
            results.append(
                KnowledgeSearchResult(
                    object_id=str(doc.get("object_id") or ""),
                    title=str(doc.get("title") or ""),
                    path=str(doc.get("path") or ""),
                    score=score,
                    snippets=self._snippets(str(doc.get("text") or ""), query_tokens),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]

    def ask(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        results = self.search(query, limit=limit)
        if not results:
            return {
                "answer": "Не найдено подтвержденных источников в локальной базе знаний.",
                "sources": [],
                "confidence": "low",
            }
        lines = ["Найденные подтвержденные источники:"]
        for result in results:
            lines.append(f"- {result.title} (`{result.object_id}`), score={result.score}")
            for snippet in result.snippets[:2]:
                lines.append(f"  - {snippet}")
        return {
            "answer": "\n".join(lines),
            "sources": [result.model_dump() for result in results],
            "confidence": "medium" if results[0].score >= 2 else "low",
        }

    def create_revision_proposal(
        self,
        *,
        object_id: str,
        correction: str,
        output_dir: Path | None = None,
    ) -> KnowledgeRevisionProposal:
        source_path = self.root / "knowledge" / "task_cases" / f"{object_id}.json"
        if not source_path.exists():
            raise FileNotFoundError(f"Knowledge object is not found: {source_path}")
        data = self._read_json(source_path)
        created_at = datetime.now(UTC).isoformat()
        proposal_dir = output_dir or (self.root / "knowledge" / "drafts")
        proposal_dir.mkdir(parents=True, exist_ok=True)
        proposal_path = proposal_dir / f"{object_id}__revision_proposal.md"
        metadata_path = proposal_dir / f"{object_id}__revision_proposal.json"
        proposal = self._revision_markdown(data, correction=correction, created_at=created_at)
        proposal_path.write_text(proposal, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "object_id": object_id,
                    "status": "draft",
                    "correction": correction,
                    "proposal_path": str(proposal_path),
                    "source_path": str(source_path),
                    "created_at": created_at,
                    "applied_at": None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return KnowledgeRevisionProposal(
            object_id=object_id,
            correction=correction,
            proposal_path=str(proposal_path),
            metadata_path=str(metadata_path),
            source_path=str(source_path),
            created_at=created_at,
        )

    def set_object_status(self, *, object_id: str, status: str) -> dict[str, Any]:
        if status not in {"draft", "approved", "archived"}:
            raise ValueError("Knowledge object status must be draft, approved, or archived.")
        path = self._canonical_object_path(object_id)
        if not path:
            raise FileNotFoundError(f"Knowledge object is not found: {object_id}")
        data = self._read_json(path)
        data["status"] = status
        data["updated_at"] = datetime.now(UTC).isoformat()
        if status == "archived":
            data["archived_at"] = data["updated_at"]
            data["needs_human_review"] = False
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rewrite_object_artifacts(path, data)
        return {"object_id": object_id, "status": status, "path": str(path)}

    def quality_report(self) -> KnowledgeQualityReport:
        counts_by_status: dict[str, int] = {}
        counts_by_type: dict[str, int] = {}
        issues: list[KnowledgeQualityIssue] = []
        for path in self._knowledge_json_paths(include_archived=True):
            data = self._read_json(path)
            if not data:
                continue
            object_id = str(data.get("object_id") or path.stem)
            object_type = str(data.get("object_type") or self._infer_object_type_from_path(path))
            status = str(data.get("status") or "draft")
            counts_by_status[status] = counts_by_status.get(status, 0) + 1
            counts_by_type[object_type] = counts_by_type.get(object_type, 0) + 1
            if status == "archived":
                continue
            if object_type == "task_case":
                if not data.get("source_events"):
                    issues.append(self._quality_issue(object_id, object_type, path, "high", "Task case has no source_events."))
                if not data.get("current_requirements"):
                    issues.append(self._quality_issue(object_id, object_type, path, "medium", "Task case has no current_requirements."))
                if not data.get("acceptance_criteria"):
                    issues.append(self._quality_issue(object_id, object_type, path, "medium", "Task case has no acceptance_criteria."))
            if object_type in {"system", "feature", "instruction"} and not data.get("source_task_cases"):
                issues.append(self._quality_issue(object_id, object_type, path, "medium", "Derived object has no source_task_cases."))
        return KnowledgeQualityReport(
            generated_at=datetime.now(UTC).isoformat(),
            counts_by_status=counts_by_status,
            counts_by_type=counts_by_type,
            issues=issues,
        )

    def set_revision_status(self, *, metadata_path: Path, status: str) -> KnowledgeRevisionProposal:
        data = self._read_json(metadata_path)
        if not data:
            raise FileNotFoundError(f"Revision metadata is not found: {metadata_path}")
        data["status"] = status
        data["updated_at"] = datetime.now(UTC).isoformat()
        metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return KnowledgeRevisionProposal(
            object_id=str(data.get("object_id") or ""),
            correction=str(data.get("correction") or ""),
            proposal_path=str(data.get("proposal_path") or ""),
            metadata_path=str(metadata_path),
            source_path=str(data.get("source_path") or ""),
            created_at=str(data.get("created_at") or ""),
            status=status,
        )

    def list_revision_metadata(self, *, status: str | None = None) -> list[dict[str, Any]]:
        items = []
        for path in self._revision_metadata_paths():
            data = self._read_json(path)
            if not data:
                continue
            if status and data.get("status") != status:
                continue
            data["_metadata_path"] = str(path)
            items.append(data)
        items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return items

    def resolve_revision_metadata(self, token: str) -> Path | None:
        needle = self._safe_slug(token)
        for path in self._revision_metadata_paths():
            data = self._read_json(path)
            candidates = {
                self._safe_slug(str(data.get("object_id") or "")),
                self._safe_slug(path.stem),
                self._safe_slug(path.name),
            }
            proposal_path = str(data.get("proposal_path") or "")
            if proposal_path:
                candidates.add(self._safe_slug(Path(proposal_path).stem))
                candidates.add(self._safe_slug(Path(proposal_path).name))
            if needle in candidates:
                return path
        return None

    def revision_diff_text(self, *, metadata_path: Path, max_chars: int = 3200) -> str:
        data = self._read_json(metadata_path)
        if not data:
            raise FileNotFoundError(f"Revision metadata is not found: {metadata_path}")
        proposal_path = Path(str(data.get("proposal_path") or ""))
        if not proposal_path.exists():
            raise FileNotFoundError(f"Revision proposal is not found: {proposal_path}")
        text = proposal_path.read_text(encoding="utf-8")
        marker = "```diff\n"
        start = text.find(marker)
        if start >= 0:
            end = text.find("\n```", start + len(marker))
            if end >= 0:
                text = text[start + len(marker) : end]
        return text[:max_chars].strip()

    def apply_revision(self, *, metadata_path: Path) -> KnowledgeRevisionProposal:
        data = self._read_json(metadata_path)
        if not data:
            raise FileNotFoundError(f"Revision metadata is not found: {metadata_path}")
        if data.get("status") != "approved":
            raise ValueError("Revision proposal must be approved before apply.")
        source_path = Path(str(data.get("source_path") or ""))
        source = self._read_json(source_path)
        history = source.get("revision_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "correction": data.get("correction"),
                "proposal_path": data.get("proposal_path"),
                "applied_at": datetime.now(UTC).isoformat(),
                "status": "applied",
            }
        )
        source["revision_history"] = history
        source["needs_human_review"] = False
        source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
        data["status"] = "applied"
        data["applied_at"] = datetime.now(UTC).isoformat()
        metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return KnowledgeRevisionProposal(
            object_id=str(data.get("object_id") or ""),
            correction=str(data.get("correction") or ""),
            proposal_path=str(data.get("proposal_path") or ""),
            metadata_path=str(metadata_path),
            source_path=str(source_path),
            created_at=str(data.get("created_at") or ""),
            status="applied",
        )

    def apply_notion_import(self, *, metadata_path: Path) -> KnowledgeRevisionProposal:
        data = self._read_json(metadata_path)
        if not data:
            raise FileNotFoundError(f"Notion import metadata is not found: {metadata_path}")
        if data.get("status") != "approved":
            raise ValueError("Notion import proposal must be approved before apply.")
        source_path = Path(str(data.get("source_path") or ""))
        source = self._read_json(source_path)
        live_markdown = Path(str(data.get("live_markdown_path") or "")).read_text(encoding="utf-8")
        self._apply_markdown_to_object(source, live_markdown)
        history = source.get("revision_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "source": "notion_import",
                "proposal_path": data.get("proposal_path"),
                "notion_url": data.get("notion_url"),
                "applied_at": datetime.now(UTC).isoformat(),
                "status": "applied",
            }
        )
        source["revision_history"] = history
        source["needs_human_review"] = False
        source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rewrite_object_artifacts(source_path, source)
        data["status"] = "applied"
        data["applied_at"] = datetime.now(UTC).isoformat()
        metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return KnowledgeRevisionProposal(
            object_id=str(data.get("object_id") or ""),
            correction=str(data.get("correction") or ""),
            proposal_path=str(data.get("proposal_path") or ""),
            metadata_path=str(metadata_path),
            source_path=str(source_path),
            created_at=str(data.get("created_at") or ""),
            status="applied",
        )

    def apply_resolved_revision(self, *, metadata_path: Path) -> KnowledgeRevisionProposal:
        data = self._read_json(metadata_path)
        if str(data.get("source") or "") == "notion_import":
            return self.apply_notion_import(metadata_path=metadata_path)
        return self.apply_revision(metadata_path=metadata_path)

    def generate_document(self, *, object_id: str, kind: str) -> KnowledgeGeneratedDocument:
        source_path = self._canonical_object_path(object_id)
        if not source_path:
            raise FileNotFoundError(f"Knowledge object is not found: {object_id}")
        data = self._read_json(source_path)
        created_at = datetime.now(UTC).isoformat()
        out_dir = self.root / "knowledge" / "drafts" / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{object_id}__{self._safe_slug(kind)}.md"
        out_path.write_text(self._generated_document_markdown(data, kind=kind, created_at=created_at), encoding="utf-8")
        return KnowledgeGeneratedDocument(object_id=object_id, kind=kind, output_path=str(out_path), created_at=created_at)

    def export_external_bundle(
        self,
        *,
        target: str = "notebooklm",
        output_dir: Path | None = None,
        system: str | None = None,
        feature_area: str | None = None,
        object_type: str | None = None,
    ) -> KnowledgeRepoResult:
        self.init()
        profile_parts = [target]
        if system:
            profile_parts.append(f"system_{self._safe_slug(system)}")
        if feature_area:
            profile_parts.append(f"feature_{self._safe_slug(feature_area)}")
        if object_type:
            profile_parts.append(f"type_{self._safe_slug(object_type)}")
        export_name = "__".join(profile_parts)
        export_dir = output_dir or (self.root / "exports" / export_name)
        if export_dir.exists():
            shutil.rmtree(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        objects = []
        for path in self._knowledge_json_paths():
            if path.name.endswith(".notion.json"):
                continue
            data = self._read_json(path)
            if not data:
                continue
            if system and self._safe_slug(str(data.get("system") or "")) != self._safe_slug(system):
                continue
            if feature_area and self._safe_slug(str(data.get("feature_area") or "")) != self._safe_slug(feature_area):
                continue
            if object_type and self._safe_slug(str(data.get("object_type") or "")) != self._safe_slug(object_type):
                continue
            object_id = str(data.get("object_id") or path.stem)
            object_dir = export_dir / object_id
            object_dir.mkdir(parents=True, exist_ok=True)
            if target == "agents":
                dest = object_dir / "ai_context.json"
                dest.write_text(json.dumps({"knowledge_object": data}, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                dest = object_dir / "source.md"
                dest.write_text(self._external_source_markdown(data), encoding="utf-8")
            written.append(str(dest))
            objects.append({"object_id": object_id, "title": data.get("title"), "path": str(dest)})
        manifest = {
            "target": target,
            "profile": {
                "system": system,
                "feature_area": feature_area,
                "object_type": object_type,
            },
            "generated_at": datetime.now(UTC).isoformat(),
            "objects": objects,
            "recommended_prompts": [
                "Answer only from attached sources.",
                "Cite object_id and source event IDs.",
                "If evidence is insufficient, say what is missing.",
            ],
        }
        manifest_path = export_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(manifest_path))
        zip_path = export_dir.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in export_dir.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(export_dir))
        written.append(str(zip_path))
        return KnowledgeRepoResult(
            root=str(self.root),
            written_files=written,
            objects_count=len(objects),
            object_ids=[str(item["object_id"]) for item in objects],
        )

    def notion_sync_plan(self, *, apply: bool = False, env: dict[str, str] | None = None) -> KnowledgeNotionResult:
        env = env or {}
        self._read_json(self.root / "meta" / "notion_mapping.json") or self._default_notion_mapping()
        targets = {database: NotionTarget.from_env(env, key=key) for database, key in NOTION_TARGET_KEYS.items()}
        planned = []
        for database, rel_dir in {
            "Task Cases": "task_cases",
            "Systems": "systems",
            "Features": "features",
            "Instructions": "instructions",
        }.items():
            for path in sorted((self.root / "knowledge" / rel_dir).glob("*.notion.json")):
                planned.append({"action": "upsert_page", "path": str(path), "database": database})
        missing = []
        if not env.get("NOTION_API_KEY"):
            missing.append("NOTION_API_KEY")
        planned_databases = {str(item["database"]) for item in planned}
        for database in sorted(planned_databases):
            key = NOTION_TARGET_KEYS[database]
            if not targets[database]:
                missing.append(f"NOTION_DATA_SOURCE_{key} or NOTION_DB_{key}")
        if missing:
            return KnowledgeNotionResult(
                mode="apply" if apply else "dry-run",
                ready=False,
                missing_env=missing,
                planned_pages=planned,
                message="Create Notion databases and configure env vars before apply.",
            )
        if apply:
            applied = []
            for item in planned:
                target = targets.get(str(item["database"]))
                if not target:
                    continue
                client = NotionKnowledgeClient(token=str(env["NOTION_API_KEY"]), target=target)
                result = client.upsert_projection(Path(item["path"]))
                applied.append({**item, **result})
            return KnowledgeNotionResult(
                mode="apply",
                ready=True,
                planned_pages=applied,
                message="Notion sync applied.",
            )
        return KnowledgeNotionResult(mode="dry-run", ready=True, planned_pages=planned, message="Dry-run plan is ready.")

    def notion_import_proposals(
        self,
        *,
        env: dict[str, str] | None = None,
        database: str | None = None,
        object_id: str | None = None,
        clients: dict[str, NotionKnowledgeClient] | None = None,
    ) -> KnowledgeNotionImportResult:
        env = env or {}
        clients = clients or {}
        targets = {name: NotionTarget.from_env(env, key=key) for name, key in NOTION_TARGET_KEYS.items()}
        databases = [database] if database else list(NOTION_TARGET_KEYS)
        missing = []
        if not env.get("NOTION_API_KEY") and not clients:
            missing.append("NOTION_API_KEY")
        for name in databases:
            if name not in NOTION_TARGET_KEYS:
                missing.append(f"Unknown database: {name}")
                continue
            key = NOTION_TARGET_KEYS[name]
            if name not in clients and not targets[name]:
                missing.append(f"NOTION_DATA_SOURCE_{key} or NOTION_DB_{key}")
        if missing:
            return KnowledgeNotionImportResult(
                ready=False,
                missing_env=missing,
                message="Configure Notion import env vars before reading live pages.",
            )

        planned: list[dict[str, Any]] = []
        written: list[str] = []
        scanned = 0
        for name in databases:
            client = clients.get(name)
            if not client:
                target = targets[name]
                if not target:
                    continue
                client = NotionKnowledgeClient(token=str(env["NOTION_API_KEY"]), target=target)
            for page in client.query_pages():
                scanned += 1
                live = client.page_to_projection(page, database=name)
                page_object_id = str((live.get("properties") or {}).get("ID") or "").strip()
                if object_id and page_object_id != object_id:
                    continue
                if not page_object_id:
                    planned.append({"action": "skip_page", "reason": "missing ID", "database": name, "page_id": page.get("id")})
                    continue
                local_path = self._notion_projection_path(page_object_id)
                source_path = self._canonical_object_path(page_object_id)
                if not local_path or not source_path:
                    planned.append(
                        {
                            "action": "skip_page",
                            "reason": "no local canonical object",
                            "database": name,
                            "object_id": page_object_id,
                            "page_id": page.get("id"),
                        }
                    )
                    continue
                local = self._read_json(local_path)
                local_markdown = str(local.get("content_markdown") or "")
                live_markdown = str(live.get("content_markdown") or "")
                if self._normalize_markdown(local_markdown) == self._normalize_markdown(live_markdown):
                    planned.append(
                        {
                            "action": "no_change",
                            "database": name,
                            "object_id": page_object_id,
                            "page_id": page.get("id"),
                        }
                    )
                    continue
                if self._looks_like_incomplete_notion_read(local_markdown, live_markdown):
                    planned.append(
                        {
                            "action": "ignored_incomplete_live_page",
                            "database": name,
                            "object_id": page_object_id,
                            "page_id": page.get("id"),
                            "url": page.get("url"),
                        }
                    )
                    continue
                diff = self._notion_import_diff(
                    database=name,
                    object_id=page_object_id,
                    local_projection_path=local_path,
                    local_markdown=local_markdown,
                    live_markdown=live_markdown,
                )
                diff_hash = self._hash_text(diff)
                if self._is_known_notion_import_diff(page_object_id, diff_hash):
                    planned.append(
                        {
                            "action": "ignored_known_diff",
                            "database": name,
                            "object_id": page_object_id,
                            "page_id": page.get("id"),
                            "url": page.get("url"),
                            "diff_hash": diff_hash,
                        }
                    )
                    continue
                proposal_files = self._write_notion_import_proposal(
                    database=name,
                    object_id=page_object_id,
                    source_path=source_path,
                    local_projection_path=local_path,
                    live_projection=live,
                    local_markdown=local_markdown,
                    live_markdown=live_markdown,
                    diff=diff,
                    diff_hash=diff_hash,
                )
                written.extend(proposal_files)
                planned.append(
                    {
                        "action": "propose_revision",
                        "database": name,
                        "object_id": page_object_id,
                        "page_id": page.get("id"),
                        "url": page.get("url"),
                        "proposal_path": proposal_files[0],
                        "metadata_path": proposal_files[1],
                    }
                )
        proposals = [item for item in planned if item.get("action") == "propose_revision"]
        return KnowledgeNotionImportResult(
            ready=True,
            scanned_pages=scanned,
            proposals_count=len(proposals),
            planned_pages=planned,
            written_files=written,
            message="Notion import proposals generated." if proposals else "No Notion edits detected.",
        )

    def _load_task_cases(self, *, include_archived: bool = True) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted((self.root / "knowledge" / "task_cases").glob("*.json")):
            if path.name.endswith(".notion.json"):
                continue
            data = self._read_json(path)
            if data and (include_archived or not self._is_archived(data)):
                items.append(data)
        return items

    def _knowledge_json_paths(self, *, include_archived: bool = False) -> list[Path]:
        paths: list[Path] = []
        for rel_dir in KNOWLEDGE_OBJECT_DIRS.values():
            paths.extend(sorted((self.root / rel_dir).glob("*.json")))
        result = []
        for path in paths:
            if path.name.endswith(".notion.json"):
                continue
            if not include_archived and self._is_archived(self._read_json(path)):
                continue
            result.append(path)
        return result

    def _revision_metadata_paths(self) -> list[Path]:
        paths = []
        drafts_dir = self.root / "knowledge" / "drafts"
        if drafts_dir.exists():
            paths.extend(sorted(drafts_dir.glob("*__revision_proposal.json")))
            paths.extend(sorted((drafts_dir / "notion_import").glob("*__notion_import.json")))
        return paths

    def _canonical_object_path(self, object_id: str) -> Path | None:
        for rel_dir in KNOWLEDGE_OBJECT_DIRS.values():
            path = self.root / rel_dir / f"{object_id}.json"
            if path.exists():
                return path
        return None

    def _notion_projection_path(self, object_id: str) -> Path | None:
        for rel_dir in KNOWLEDGE_OBJECT_DIRS.values():
            path = self.root / rel_dir / f"{object_id}.notion.json"
            if path.exists():
                return path
        return None

    def _derive_systems(self, task_cases: list[dict[str, Any]]) -> list[KnowledgeCatalogObject]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in task_cases:
            system = self._safe_slug(str(item.get("system") or "unknown"))
            grouped.setdefault(system, []).append(item)
        result: list[KnowledgeCatalogObject] = []
        for system, cases in sorted(grouped.items()):
            result.append(
                KnowledgeCatalogObject(
                    object_id=f"system__{system}",
                    object_type="system",
                    title=self._human_title(system),
                    system=system,
                    summary=self._catalog_summary(cases, label="System"),
                    source_task_cases=self._case_ids(cases),
                    linked_bitrix_tasks=self._case_bitrix_tasks(cases),
                    source_tags=self._case_tags(cases),
                    requirements=self._case_values(cases, "current_requirements"),
                    acceptance_criteria=self._case_values(cases, "acceptance_criteria"),
                    decisions=self._case_values(cases, "decisions"),
                    open_questions=self._case_values(cases, "open_questions"),
                )
            )
        return result

    def _derive_features(self, task_cases: list[dict[str, Any]]) -> list[KnowledgeCatalogObject]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in task_cases:
            system = self._safe_slug(str(item.get("system") or "unknown"))
            feature = self._safe_slug(str(item.get("feature_area") or "general"))
            grouped.setdefault((system, feature), []).append(item)
        result: list[KnowledgeCatalogObject] = []
        for (system, feature), cases in sorted(grouped.items()):
            result.append(
                KnowledgeCatalogObject(
                    object_id=f"feature__{system}__{feature}",
                    object_type="feature",
                    title=f"{self._human_title(system)} / {self._human_title(feature)}",
                    system=system,
                    feature_area=feature,
                    summary=self._catalog_summary(cases, label="Feature"),
                    source_task_cases=self._case_ids(cases),
                    linked_bitrix_tasks=self._case_bitrix_tasks(cases),
                    source_tags=self._case_tags(cases),
                    requirements=self._case_values(cases, "current_requirements"),
                    acceptance_criteria=self._case_values(cases, "acceptance_criteria"),
                    decisions=self._case_values(cases, "decisions"),
                    open_questions=self._case_values(cases, "open_questions"),
                )
            )
        return result

    def _derive_instructions(self, features: list[KnowledgeCatalogObject]) -> list[KnowledgeCatalogObject]:
        result: list[KnowledgeCatalogObject] = []
        for feature in features:
            result.append(
                KnowledgeCatalogObject(
                    object_id=f"instruction__{feature.system}__{feature.feature_area or 'general'}",
                    object_type="instruction",
                    title=f"Instruction: {feature.title}",
                    status=feature.status,
                    system=feature.system,
                    feature_area=feature.feature_area,
                    summary=feature.summary,
                    source_task_cases=feature.source_task_cases,
                    linked_bitrix_tasks=feature.linked_bitrix_tasks,
                    source_tags=feature.source_tags,
                    requirements=feature.requirements,
                    acceptance_criteria=feature.acceptance_criteria,
                    decisions=feature.decisions,
                    open_questions=feature.open_questions,
                )
            )
        return result

    def _write_catalog_object(self, item: KnowledgeCatalogObject, *, directory: str, database: str) -> list[str]:
        target_dir = self.root / "knowledge" / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / f"{item.object_id}.json"
        md_path = target_dir / f"{item.object_id}.md"
        notion_path = target_dir / f"{item.object_id}.notion.json"
        data = item.model_dump()
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self._catalog_markdown(item), encoding="utf-8")
        notion_path.write_text(
            json.dumps(self._catalog_notion_projection(item, database=database), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return [str(json_path), str(md_path), str(notion_path)]

    def _prune_catalog_directory(self, directory: str, expected_object_ids: set[str]) -> list[str]:
        target_dir = self.root / "knowledge" / directory
        removed: list[str] = []
        if not target_dir.exists():
            return removed
        for json_path in sorted(target_dir.glob("*.json")):
            if json_path.name.endswith(".notion.json"):
                continue
            data = self._read_json(json_path)
            object_id = str(data.get("object_id") or json_path.stem)
            if object_id in expected_object_ids:
                continue
            for path in (json_path, json_path.with_suffix(".md"), json_path.with_suffix(".notion.json")):
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
        return removed

    def _write_notion_import_proposal(
        self,
        *,
        database: str,
        object_id: str,
        source_path: Path,
        local_projection_path: Path,
        live_projection: dict[str, Any],
        local_markdown: str,
        live_markdown: str,
        diff: str | None = None,
        diff_hash: str | None = None,
    ) -> list[str]:
        created_at = datetime.now(UTC).isoformat()
        proposal_dir = self.root / "knowledge" / "drafts" / "notion_import"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        safe_id = self._safe_slug(object_id)
        proposal_path = proposal_dir / f"{safe_id}__notion_import.md"
        metadata_path = proposal_dir / f"{safe_id}__notion_import.json"
        live_path = proposal_dir / f"{safe_id}__notion_live.md"
        diff = diff if diff is not None else self._notion_import_diff(
            database=database,
            object_id=object_id,
            local_projection_path=local_projection_path,
            local_markdown=local_markdown,
            live_markdown=live_markdown,
        )
        diff_hash = diff_hash or self._hash_text(diff)
        live_path.write_text(live_markdown, encoding="utf-8")
        proposal_path.write_text(
            "\n".join(
                [
                    f"# Notion Import Proposal: {object_id}",
                    "",
                    f"- Object ID: `{object_id}`",
                    f"- Database: `{database}`",
                    f"- Status: `draft`",
                    f"- Created at: {created_at}",
                    f"- Diff hash: `{diff_hash}`",
                    f"- Source JSON: `{source_path}`",
                    f"- Local projection: `{local_projection_path}`",
                    f"- Notion page: {live_projection.get('url') or live_projection.get('page_id') or '-'}",
                    "",
                    "## Intent",
                    "",
                    "Manual Notion edits were detected. Review the diff and update canonical JSON only after approval.",
                    "",
                    "## Unified Diff",
                    "",
                    "```diff",
                    diff or "# No textual diff generated.",
                    "```",
                    "",
                    "## Live Notion Markdown",
                    "",
                    live_markdown.strip(),
                    "",
                ]
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "object_id": object_id,
                    "status": "draft",
                    "source": "notion_import",
                    "database": database,
                    "source_path": str(source_path),
                    "local_projection_path": str(local_projection_path),
                    "proposal_path": str(proposal_path),
                    "live_markdown_path": str(live_path),
                    "diff_hash": diff_hash,
                    "notion_page_id": live_projection.get("page_id"),
                    "notion_url": live_projection.get("url"),
                    "correction": "Manual Notion edit detected; review unified diff before applying to canonical JSON.",
                    "created_at": created_at,
                    "applied_at": None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return [str(proposal_path), str(metadata_path), str(live_path)]

    def _notion_import_metadata_path(self, object_id: str) -> Path:
        safe_id = self._safe_slug(object_id)
        return self.root / "knowledge" / "drafts" / "notion_import" / f"{safe_id}__notion_import.json"

    def _is_known_notion_import_diff(self, object_id: str, diff_hash: str) -> bool:
        metadata = self._read_json(self._notion_import_metadata_path(object_id))
        status = str(metadata.get("status") or "")
        return bool(diff_hash and metadata.get("diff_hash") == diff_hash and status in {"applied", "rejected"})

    def _notion_import_diff(
        self,
        *,
        database: str,
        object_id: str,
        local_projection_path: Path,
        local_markdown: str,
        live_markdown: str,
    ) -> str:
        try:
            fromfile = str(local_projection_path.relative_to(self.root))
        except ValueError:
            fromfile = str(local_projection_path)
        return "\n".join(
            difflib.unified_diff(
                local_markdown.splitlines(),
                live_markdown.splitlines(),
                fromfile=fromfile,
                tofile=f"notion:{database}:{object_id}",
                lineterm="",
            )
        )

    @classmethod
    def _looks_like_incomplete_notion_read(cls, local_markdown: str, live_markdown: str) -> bool:
        local_lines = [line for line in cls._normalize_markdown(local_markdown).splitlines() if line.strip()]
        live_lines = [line for line in cls._normalize_markdown(live_markdown).splitlines() if line.strip()]
        if len(local_lines) < 6 or not live_lines:
            return False
        local_text = "\n".join(local_lines)
        live_text = "\n".join(live_lines)
        if len(live_text) >= len(local_text) * 0.65:
            return False
        first_heading = next((line for line in local_lines if line.startswith("# ")), "")
        if first_heading and first_heading not in live_text:
            return True
        local_sections = {line for line in local_lines if line.startswith("## ")}
        live_sections = {line for line in live_lines if line.startswith("## ")}
        return bool(local_sections and len(live_sections) < max(1, len(local_sections) // 2))

    def _rewrite_object_artifacts(self, source_path: Path, data: dict[str, Any]) -> None:
        object_type = str(data.get("object_type") or "")
        if object_type == "task_case":
            item = KnowledgeObject.model_validate(data)
            md_path = source_path.with_suffix(".md")
            notion_path = source_path.with_suffix(".notion.json")
            md_path.write_text(KnowledgeIntake._spec_markdown(item), encoding="utf-8")
            notion_path.write_text(json.dumps(self._notion_projection(item), ensure_ascii=False, indent=2), encoding="utf-8")
            return
        directory = source_path.parent.name
        database = {
            "systems": "Systems",
            "features": "Features",
            "instructions": "Instructions",
        }.get(directory, "Instructions")
        item = KnowledgeCatalogObject.model_validate(data)
        source_path.with_suffix(".md").write_text(self._catalog_markdown(item), encoding="utf-8")
        source_path.with_suffix(".notion.json").write_text(
            json.dumps(self._catalog_notion_projection(item, database=database), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _merge_with_existing(self, item: KnowledgeObject) -> KnowledgeObject:
        path = self.root / "knowledge" / "task_cases" / f"{item.object_id}.json"
        if not path.exists():
            return item
        existing_data = self._read_json(path)
        try:
            existing = KnowledgeObject.model_validate(existing_data)
        except Exception:
            return item
        return KnowledgeObject(
            **{
                **existing.model_dump(),
                "title": item.title or existing.title,
                "system": item.system if item.system != "unknown" else existing.system,
                "subsystem": item.subsystem or existing.subsystem,
                "feature_area": item.feature_area or existing.feature_area,
                "source_tags": self._merge_unique(existing.source_tags, item.source_tags),
                "linked_bitrix_tasks": sorted(set(existing.linked_bitrix_tasks) | set(item.linked_bitrix_tasks)),
                "linked_loom_ids": sorted(set(existing.linked_loom_ids) | set(item.linked_loom_ids)),
                "linked_telegram_posts": sorted(set(existing.linked_telegram_posts) | set(item.linked_telegram_posts)),
                "current_summary": item.current_summary or existing.current_summary,
                "current_requirements": self._merge_unique(existing.current_requirements, item.current_requirements),
                "acceptance_criteria": self._merge_unique(existing.acceptance_criteria, item.acceptance_criteria),
                "decisions": self._merge_unique(existing.decisions, item.decisions),
                "open_questions": self._merge_unique(existing.open_questions, item.open_questions),
                "demo_feedback": self._merge_unique(existing.demo_feedback, item.demo_feedback),
                "source_events": self._merge_events(existing.source_events, item.source_events),
            }
        )

    def _write_task_case(self, item: KnowledgeObject) -> list[str]:
        task_dir = self.root / "knowledge" / "task_cases"
        task_dir.mkdir(parents=True, exist_ok=True)
        json_path = task_dir / f"{item.object_id}.json"
        md_path = task_dir / f"{item.object_id}.md"
        notion_path = task_dir / f"{item.object_id}.notion.json"
        json_path.write_text(json.dumps(item.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(KnowledgeIntake._spec_markdown(item), encoding="utf-8")
        notion_path.write_text(json.dumps(self._notion_projection(item), ensure_ascii=False, indent=2), encoding="utf-8")
        return [str(json_path), str(md_path), str(notion_path)]

    def _write_draft(self, item: KnowledgeObject) -> list[str]:
        draft_dir = self.root / "knowledge" / "drafts" / item.object_id
        draft_dir.mkdir(parents=True, exist_ok=True)
        proposal_path = draft_dir / "proposal.json"
        diff_path = draft_dir / "proposal.md"
        proposal_path.write_text(json.dumps(item.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        diff_path.write_text(self._draft_markdown(item), encoding="utf-8")
        return [str(proposal_path), str(diff_path)]

    @staticmethod
    def _notion_projection(item: KnowledgeObject) -> dict[str, Any]:
        return {
            "database": "Task Cases",
            "properties": {
                "Title": item.title,
                "ID": item.object_id,
                "Type": item.object_type,
                "Status": item.status,
                "System": item.system,
                "Feature Area": item.feature_area,
                "Tags": item.source_tags,
                "Bitrix Tasks": [str(value) for value in item.linked_bitrix_tasks],
                "Updated At": datetime.now(UTC).isoformat(),
            },
            "content_markdown": KnowledgeIntake._spec_markdown(item),
        }

    @staticmethod
    def _catalog_notion_projection(item: KnowledgeCatalogObject, *, database: str) -> dict[str, Any]:
        return {
            "database": database,
            "properties": {
                "Title": item.title,
                "ID": item.object_id,
                "Type": item.object_type,
                "Status": item.status,
                "System": item.system,
                "Feature Area": item.feature_area,
                "Tags": item.source_tags,
                "Bitrix Tasks": [str(value) for value in item.linked_bitrix_tasks],
                "Updated At": datetime.now(UTC).isoformat(),
            },
            "content_markdown": KnowledgeRepository._catalog_markdown(item),
        }

    @staticmethod
    def _catalog_markdown(item: KnowledgeCatalogObject) -> str:
        lines = [
            f"# {item.title}",
            "",
            f"- Object ID: `{item.object_id}`",
            f"- Type: `{item.object_type}`",
            f"- System: `{item.system}`",
            f"- Feature area: `{item.feature_area or 'general'}`",
            f"- Status: `{item.status}`",
            f"- Source task cases: {', '.join(item.source_task_cases) or '-'}",
            f"- Bitrix tasks: {', '.join(str(task) for task in item.linked_bitrix_tasks) or '-'}",
            f"- Source tags: {', '.join(item.source_tags) or '-'}",
            "",
            "## Summary",
            "",
            item.summary or "No summary yet.",
            "",
        ]
        KnowledgeRepository._extend_catalog_section(lines, "Confirmed Requirements", item.requirements)
        KnowledgeRepository._extend_catalog_section(lines, "Acceptance Criteria", item.acceptance_criteria)
        KnowledgeRepository._extend_catalog_section(lines, "Decisions", item.decisions)
        KnowledgeRepository._extend_catalog_section(lines, "Open Questions", item.open_questions)
        if item.object_type == "instruction":
            lines.extend(
                [
                    "## User Instruction Draft",
                    "",
                    "Use the confirmed requirements and acceptance criteria above as grounded source material.",
                    "Before publishing to users, review open questions and resolve any implementation-specific gaps.",
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _default_notion_mapping() -> dict[str, Any]:
        return {
            "databases": {
                "task_cases": {
                    "notion_database_id": "${NOTION_DB_TASK_CASES}",
                    "source_path": "knowledge/task_cases",
                    "title_property": "Title",
                    "id_property": "ID",
                },
                "systems": {"notion_database_id": "${NOTION_DB_SYSTEMS}", "source_path": "knowledge/systems"},
                "features": {"notion_database_id": "${NOTION_DB_FEATURES}", "source_path": "knowledge/features"},
                "instructions": {
                    "notion_database_id": "${NOTION_DB_INSTRUCTIONS}",
                    "source_path": "knowledge/instructions",
                },
            }
        }

    @staticmethod
    def _default_model_policy() -> dict[str, Any]:
        return {
            "revision": {"mode": "proposal_only", "direct_mutation_allowed": False},
            "search": {"mode": "local_lexical_and_chunk_mvp", "requires_sources": True},
            "embeddings": {"provider": "openai", "enabled": False, "vector_store": "pending"},
        }

    @staticmethod
    def _draft_markdown(item: KnowledgeObject) -> str:
        lines = [
            f"# Draft Proposal: {item.title}",
            "",
            f"Object ID: `{item.object_id}`",
            "",
            "This is a review draft. Apply it to `knowledge/task_cases` only after human review.",
            "",
            KnowledgeIntake._spec_markdown(item).strip(),
        ]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _revision_markdown(data: dict[str, Any], *, correction: str, created_at: str) -> str:
        return "\n".join(
            [
                f"# Revision Proposal: {data.get('title') or data.get('object_id')}",
                "",
                f"- Object ID: `{data.get('object_id')}`",
                f"- Created at: {created_at}",
                "- Mode: proposal only",
                "",
                "## User Correction",
                "",
                correction,
                "",
                "## Grounding Rules",
                "",
                "- Use only source events from the knowledge object.",
                "- Prefer demo events over discussion events in conflicts.",
                "- Preserve source event IDs for material changes.",
                "- Return a patch/diff for human review before modifying the Git knowledge repository.",
                "",
                "## Source Events",
                "",
                *[
                    f"- `{event.get('event_id')}`: {event.get('title')} ({event.get('event_type')})"
                    for event in data.get("source_events", [])
                    if isinstance(event, dict)
                ],
            ]
        ).strip() + "\n"

    @staticmethod
    def _index_text(data: dict[str, Any]) -> str:
        parts = [
            data.get("object_id", ""),
            data.get("object_type", ""),
            data.get("title", ""),
            data.get("system", ""),
            data.get("feature_area", ""),
            data.get("current_summary", ""),
            data.get("summary", ""),
            " ".join(data.get("current_requirements") or []),
            " ".join(data.get("requirements") or []),
            " ".join(data.get("acceptance_criteria") or []),
            " ".join(data.get("decisions") or []),
            " ".join(data.get("open_questions") or []),
            " ".join(data.get("demo_feedback") or []),
            " ".join(data.get("source_task_cases") or []),
        ]
        for event in data.get("source_events", []):
            if isinstance(event, dict):
                parts.extend(
                    [
                        event.get("title", ""),
                        event.get("summary", ""),
                        " ".join(event.get("action_items") or []),
                        " ".join(event.get("decisions") or []),
                    ]
                )
        return "\n".join(str(part) for part in parts if part)

    @staticmethod
    def _chunks_for_object(data: dict[str, Any]) -> list[str]:
        chunks = [
            "\n".join(
                [
                    str(data.get("title") or ""),
                    str(data.get("current_summary") or ""),
                    str(data.get("summary") or ""),
                    "\n".join(data.get("current_requirements") or []),
                    "\n".join(data.get("requirements") or []),
                    "\n".join(data.get("acceptance_criteria") or []),
                ]
            ).strip()
        ]
        for event in data.get("source_events", []):
            if isinstance(event, dict):
                chunks.append(
                    "\n".join(
                        [
                            str(event.get("event_id") or ""),
                            str(event.get("title") or ""),
                            str(event.get("summary") or ""),
                            "\n".join(event.get("action_items") or []),
                            "\n".join(event.get("decisions") or []),
                        ]
                    ).strip()
                )
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _source_event_ids(data: dict[str, Any]) -> list[str]:
        return [
            str(event.get("event_id"))
            for event in data.get("source_events", [])
            if isinstance(event, dict) and event.get("event_id")
        ]

    @staticmethod
    def _external_source_markdown(data: dict[str, Any]) -> str:
        if data.get("object_type") != "task_case":
            return KnowledgeRepository._catalog_markdown(KnowledgeCatalogObject.model_validate(data))
        item = KnowledgeObject.model_validate(data)
        return "\n\n".join(
            [
                KnowledgeIntake._overview_markdown(item).strip(),
                KnowledgeIntake._spec_markdown(item).strip(),
                KnowledgeIntake._events_markdown(item).strip(),
                KnowledgeIntake._sources_markdown(item).strip(),
            ]
        ) + "\n"

    @classmethod
    def _apply_markdown_to_object(cls, data: dict[str, Any], markdown: str) -> None:
        sections = cls._markdown_sections(markdown)
        if data.get("object_type") == "task_case":
            summary = sections.get("Current Summary") or sections.get("Summary")
            if summary:
                data["current_summary"] = "\n".join(summary).strip()
            mapping = {
                "Requirements": "current_requirements",
                "Confirmed Requirements": "current_requirements",
                "Acceptance Criteria": "acceptance_criteria",
                "Decisions": "decisions",
                "Open Questions": "open_questions",
                "Demo Feedback": "demo_feedback",
            }
        else:
            summary = sections.get("Summary")
            if summary:
                data["summary"] = "\n".join(summary).strip()
            mapping = {
                "Confirmed Requirements": "requirements",
                "Requirements": "requirements",
                "Acceptance Criteria": "acceptance_criteria",
                "Decisions": "decisions",
                "Open Questions": "open_questions",
            }
        for section, key in mapping.items():
            values = cls._section_bullets(sections.get(section) or [])
            if values:
                data[key] = values

    @staticmethod
    def _markdown_sections(markdown: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current = ""
        for raw_line in markdown.splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                current = line[3:].strip()
                sections.setdefault(current, [])
                continue
            if line.startswith("# "):
                continue
            if current:
                sections.setdefault(current, []).append(line)
        return {key: [line for line in value if line.strip()] for key, value in sections.items()}

    @staticmethod
    def _section_bullets(lines: list[str]) -> list[str]:
        values: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- "):
                values.append(stripped[2:].strip())
            elif stripped.startswith("1. "):
                values.append(stripped[3:].strip())
        return [value for value in values if value]

    @staticmethod
    def _generated_document_markdown(data: dict[str, Any], *, kind: str, created_at: str) -> str:
        title = str(data.get("title") or data.get("object_id") or "Knowledge object")
        requirements = data.get("current_requirements") or data.get("requirements") or []
        acceptance = data.get("acceptance_criteria") or []
        decisions = data.get("decisions") or []
        questions = data.get("open_questions") or []
        source_cases = data.get("source_task_cases") or [data.get("object_id")]
        lines = [
            f"# {kind.replace('_', ' ').title()}: {title}",
            "",
            f"- Object ID: `{data.get('object_id')}`",
            f"- System: `{data.get('system') or 'unknown'}`",
            f"- Feature area: `{data.get('feature_area') or 'general'}`",
            f"- Generated at: {created_at}",
            f"- Sources: {', '.join(str(item) for item in source_cases if item) or '-'}",
            "",
            "## Context",
            "",
            str(data.get("current_summary") or data.get("summary") or "No summary yet."),
            "",
        ]
        KnowledgeRepository._extend_catalog_section(lines, "Functional Requirements", [str(item) for item in requirements])
        KnowledgeRepository._extend_catalog_section(lines, "Acceptance Criteria", [str(item) for item in acceptance])
        KnowledgeRepository._extend_catalog_section(lines, "Confirmed Decisions", [str(item) for item in decisions])
        if kind in {"user_instruction", "support_faq"}:
            lines.extend(["## Operator Flow", "", "1. Review prerequisites and affected system.", "2. Follow the confirmed requirements above.", "3. Validate the expected result with acceptance criteria.", ""])
        if kind in {"technical_spec", "implementation_spec"}:
            lines.extend(["## Implementation Notes", "", "- Use only confirmed requirements as scope.", "- Treat open questions as blockers before final estimation.", ""])
        KnowledgeRepository._extend_catalog_section(lines, "Open Questions", [str(item) for item in questions])
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [token for token in re.findall(r"[\wА-Яа-яІіЇїЄєҐґ]{3,}", text.casefold(), flags=re.UNICODE)]

    @staticmethod
    def _snippets(text: str, query_tokens: set[str]) -> list[str]:
        snippets = []
        for line in text.splitlines():
            line_tokens = set(KnowledgeRepository._tokens(line))
            if query_tokens & line_tokens:
                snippets.append(line[:240])
            if len(snippets) >= 3:
                break
        return snippets

    @staticmethod
    def _merge_unique(left: list[str], right: list[str]) -> list[str]:
        result = list(left)
        seen = {KnowledgeRepository._norm(value) for value in left}
        for value in right:
            key = KnowledgeRepository._norm(value)
            if key and key not in seen:
                result.append(value)
                seen.add(key)
        return result

    @staticmethod
    def _merge_events(left: list[Any], right: list[Any]) -> list[Any]:
        result = list(left)
        seen = {getattr(event, "event_id", "") for event in left}
        for event in right:
            event_id = getattr(event, "event_id", "")
            if event_id and event_id not in seen:
                result.append(event)
                seen.add(event_id)
        return result

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", str(value).casefold()).strip()

    @staticmethod
    def _normalize_markdown(value: str) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_")
        return slug or "unknown"

    @staticmethod
    def _human_title(value: str) -> str:
        known = {
            "aicallorder": "AIcallorder",
            "bitrix": "Bitrix",
            "meeting_digest_bot": "MeetingDigestBot",
            "unknown": "Unknown",
            "knowledge_base": "Knowledge Base",
            "telegram_publication": "Telegram Publication",
            "checklists": "Checklists",
            "comments": "Comments",
            "general": "General",
        }
        return known.get(value, value.replace("_", " ").title())

    @staticmethod
    def _case_ids(cases: list[dict[str, Any]]) -> list[str]:
        return sorted({str(item.get("object_id")) for item in cases if item.get("object_id")})

    @staticmethod
    def _case_bitrix_tasks(cases: list[dict[str, Any]]) -> list[int]:
        tasks: set[int] = set()
        for item in cases:
            for value in item.get("linked_bitrix_tasks") or []:
                try:
                    tasks.add(int(value))
                except (TypeError, ValueError):
                    continue
        return sorted(tasks)

    @staticmethod
    def _case_tags(cases: list[dict[str, Any]]) -> list[str]:
        tags: set[str] = set()
        for item in cases:
            tags.update(str(value) for value in item.get("source_tags") or [] if str(value).strip())
        return sorted(tags)

    @staticmethod
    def _case_values(cases: list[dict[str, Any]], key: str) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for item in cases:
            for value in item.get(key) or []:
                text = str(value).strip()
                norm = KnowledgeRepository._norm(text)
                if text and norm not in seen:
                    values.append(text)
                    seen.add(norm)
        return values

    @staticmethod
    def _catalog_summary(cases: list[dict[str, Any]], *, label: str) -> str:
        titles = [str(item.get("title") or item.get("object_id") or "").strip() for item in cases]
        titles = [title for title in titles if title]
        if not titles:
            return f"{label} catalog generated from task discussions and demos."
        preview = "; ".join(titles[:5])
        suffix = f" (+{len(titles) - 5} more)" if len(titles) > 5 else ""
        return f"{label} catalog generated from {len(titles)} task case(s): {preview}{suffix}."

    @staticmethod
    def _extend_catalog_section(lines: list[str], title: str, values: list[str]) -> None:
        if not values:
            return
        lines.extend([f"## {title}", ""])
        lines.extend(f"- {value}" for value in values)
        lines.append("")

    @staticmethod
    def _is_archived(data: dict[str, Any]) -> bool:
        return str(data.get("status") or "").casefold() == "archived"

    @staticmethod
    def _infer_object_type_from_path(path: Path) -> str:
        directory = path.parent.name
        return {
            "task_cases": "task_case",
            "systems": "system",
            "features": "feature",
            "instructions": "instruction",
        }.get(directory, "unknown")

    @staticmethod
    def _quality_issue(object_id: str, object_type: str, path: Path, severity: str, message: str) -> KnowledgeQualityIssue:
        return KnowledgeQualityIssue(
            object_id=object_id,
            object_type=object_type,
            path=str(path),
            severity=severity,
            message=message,
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
