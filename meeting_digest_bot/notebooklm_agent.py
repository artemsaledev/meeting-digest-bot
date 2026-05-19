from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Any
from zipfile import ZipFile


NOTEBOOKLM_URL = "https://notebooklm.google.com/"


@dataclass(slots=True)
class NotebookLMPackage:
    session_id: str
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    source_files: list[Path]
    prompt_path: Path

    @property
    def title(self) -> str:
        return str(self.manifest.get("notebooklm_project_title") or self.manifest.get("title") or self.session_id)


@dataclass(slots=True)
class RemoteTaskExtractorConfig:
    host: str
    username: str
    password: str = ""
    port: int = 22
    remote_exports_root: str = "/opt/meeting-digest-bot/exports/task_extractor"


class NotebookLMAgent:
    def __init__(
        self,
        *,
        exports_root: Path | str = Path("exports") / "task_extractor",
        profile_dir: Path | str = Path("data") / "notebooklm-browser-profile",
    ) -> None:
        self.exports_root = Path(exports_root)
        self.profile_dir = Path(profile_dir)

    def prepare_package(self, *, session_id: str) -> NotebookLMPackage:
        package_root = self._package_root(session_id)
        manifest_path = package_root / "machine_bundle" / "handoff_manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Task Extractor handoff manifest not found: {manifest_path}")
        manifest = self._load_json(manifest_path)
        source_files = self._source_files(package_root=package_root, manifest=manifest)
        prompt_path = package_root / "prompt_workspace" / "prompt_for_notebooklm.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"NotebookLM prompt not found: {prompt_path}")
        result = NotebookLMPackage(
            session_id=session_id,
            root=package_root,
            manifest_path=manifest_path,
            manifest=manifest,
            source_files=source_files,
            prompt_path=prompt_path,
        )
        self.write_run_log(result, status="prepared")
        return result

    def prepare_knowledge_package(
        self,
        *,
        knowledge_dir: Path | str,
        session_id: str = "company-knowledge",
    ) -> NotebookLMPackage:
        knowledge_root = Path(knowledge_dir)
        export_root = knowledge_root / "exports" / "notebooklm"
        manifest_path = export_root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"NotebookLM knowledge manifest not found: {manifest_path}")
        manifest = self._load_json(manifest_path)
        package_root = self.exports_root / session_id
        source_dir = package_root / "source_bundle"
        prompt_dir = package_root / "prompt_workspace"
        machine_dir = package_root / "machine_bundle"
        if package_root.exists():
            shutil.rmtree(package_root)
        source_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)
        machine_dir.mkdir(parents=True, exist_ok=True)

        source_files: list[str] = []
        overview = source_dir / "00_overview.md"
        overview.write_text(self._knowledge_overview_markdown(manifest=manifest), encoding="utf-8")
        source_files.append(str(overview.relative_to(package_root)))
        grouped_sources = self._knowledge_grouped_sources(knowledge_root=knowledge_root, manifest=manifest)
        for name, text in grouped_sources:
            dest = source_dir / name
            dest.write_text(text, encoding="utf-8")
            source_files.append(str(dest.relative_to(package_root)))

        prompt_path = prompt_dir / "prompt_for_notebooklm.md"
        prompt_path.write_text(self._knowledge_start_prompt(), encoding="utf-8")
        handoff = {
            "session_id": session_id,
            "status": "exported",
            "notebooklm_project_title": "Company Knowledge Base",
            "notebooklm_project_url": "",
            "source": "company_knowledge",
            "knowledge_dir": str(knowledge_root),
            "source_bundle_files": source_files,
            "created_at": datetime.now(UTC).isoformat(),
        }
        (machine_dir / "handoff_manifest.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.prepare_package(session_id=session_id)

    def queue_prompt(
        self,
        *,
        session_id: str,
        prompt: str,
        kind: str = "rag_followup",
    ) -> Path:
        package_root = self._package_root(session_id)
        prompt_queue = package_root / "prompt_workspace" / "prompt_queue"
        prompt_queue.mkdir(parents=True, exist_ok=True)
        safe_kind = self._safe_file_part(kind or "prompt")
        prompt_path = prompt_queue / f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{safe_kind}.md"
        prompt_path.write_text(prompt.strip() + "\n", encoding="utf-8")
        try:
            prompt_queue.chmod(0o2775)
            prompt_path.chmod(0o664)
        except OSError:
            pass
        return prompt_path

    def open_auth(self, *, url: str = NOTEBOOKLM_URL) -> dict[str, Any]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        executable = self._browser_executable()
        args = [
            str(executable),
            f"--user-data-dir={self.profile_dir.resolve()}",
            "--profile-directory=Default",
            "--no-first-run",
            "--disable-default-apps",
            url,
        ]
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            "status": "opened",
            "url": url,
            "profile_dir": str(self.profile_dir.resolve()),
            "browser": str(executable),
            "pid": process.pid,
        }

    def create_notebook(self, *, session_id: str, send_prompt: bool = True) -> dict[str, Any]:
        package = self.prepare_package(session_id=session_id)
        existing_url = str(package.manifest.get("notebooklm_project_url") or "").strip()
        if existing_url:
            run_path = self.write_run_log(
                package,
                status="already_exists",
                notebooklm_project_url=existing_url,
                notes=["Existing NotebookLM project URL found in handoff manifest."],
            )
            return {
                "status": "already_exists",
                "session_id": package.session_id,
                "notebooklm_project_title": package.title,
                "notebooklm_project_url": existing_url,
                "uploaded_sources": [],
                "expected_sources": [path.name for path in package.source_files],
                "prompt_sent": False,
                "run_path": str(run_path),
            }
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required for notebook creation. Run: python -m pip install playwright") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        executable = self._browser_executable()
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir.resolve()),
                executable_path=str(executable),
                headless=False,
                viewport={"width": 1440, "height": 1000},
                args=["--no-first-run", "--disable-default-apps"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(NOTEBOOKLM_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(3_000)
                self._click_button_by_text(
                    page,
                    exact="add\nСоздать",
                    contains="Создать",
                    fallback_contains="Create",
                )
                page.wait_for_url("**/notebook/**", timeout=60_000)
                page.wait_for_timeout(5_000)
                notebook_url = page.url.split("?")[0]

                self._upload_sources(page, package.source_files)
                uploaded = self._wait_for_sources(page, package.source_files)

                prompt_sent = False
                if send_prompt:
                    prompt = package.prompt_path.read_text(encoding="utf-8").strip()
                    if prompt:
                        self._send_prompt(page, prompt)
                        prompt_sent = True

                self._write_notebook_url(package=package, notebook_url=notebook_url, status="notebook_created")
                run_path = self.write_run_log(
                    package,
                    status="notebook_created",
                    notebooklm_project_url=notebook_url,
                    notes=["Prompt sent."] if prompt_sent else ["Prompt skipped."],
                )
                return {
                    "status": "notebook_created",
                    "session_id": package.session_id,
                    "notebooklm_project_title": package.title,
                    "notebooklm_project_url": notebook_url,
                    "uploaded_sources": uploaded,
                    "expected_sources": [path.name for path in package.source_files],
                    "prompt_sent": prompt_sent,
                    "run_path": str(run_path),
                }
            finally:
                context.close()

    def watch(
        self,
        *,
        once: bool = False,
        interval_seconds: int = 60,
        limit: int = 1,
        remote: RemoteTaskExtractorConfig | None = None,
        send_prompt: bool = True,
    ) -> dict[str, Any]:
        runs = 0
        processed: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        while True:
            runs += 1
            try:
                processed.extend(self.process_pending(limit=limit, remote=remote, send_prompt=send_prompt))
            except Exception as exc:
                failures.append({"error": f"{type(exc).__name__}: {exc}"})
                print(json.dumps({"notebooklm_agent_error": failures[-1]}, ensure_ascii=False), flush=True)
            if processed:
                print(json.dumps({"notebooklm_agent_processed": processed[-len(processed) :]}, ensure_ascii=False), flush=True)
            if once:
                break
            time.sleep(max(5, interval_seconds))
        return {"runs": runs, "processed": processed, "failures": failures}

    def process_pending(
        self,
        *,
        limit: int = 1,
        remote: RemoteTaskExtractorConfig | None = None,
        send_prompt: bool = True,
    ) -> list[dict[str, Any]]:
        if remote:
            self.pull_remote_pending(remote=remote, limit=limit)
        results: list[dict[str, Any]] = []
        for session_id in self.pending_session_ids(limit=limit):
            result = self.create_notebook(session_id=session_id, send_prompt=send_prompt)
            results.append(result)
            if remote:
                self.push_remote_metadata(remote=remote, session_id=session_id)
        remaining = max(0, limit - len(results))
        if remaining:
            for session_id, prompt_path in self.pending_prompt_items(limit=remaining):
                result = self.send_queued_prompt(session_id=session_id, prompt_path=prompt_path)
                results.append(result)
                if remote:
                    self.push_remote_metadata(remote=remote, session_id=session_id)
        return results

    def pending_session_ids(self, *, limit: int = 10) -> list[str]:
        if not self.exports_root.exists():
            return []
        result: list[str] = []
        for manifest_path in sorted(self.exports_root.glob("*/machine_bundle/handoff_manifest.json")):
            try:
                manifest = self._load_json(manifest_path)
            except Exception:
                continue
            if self._is_pending_manifest(manifest):
                result.append(manifest_path.parents[1].name)
            if len(result) >= limit:
                break
        return result

    def pending_prompt_items(self, *, limit: int = 10) -> list[tuple[str, Path]]:
        if not self.exports_root.exists():
            return []
        result: list[tuple[str, Path]] = []
        for manifest_path in sorted(self.exports_root.glob("*/machine_bundle/handoff_manifest.json")):
            try:
                manifest = self._load_json(manifest_path)
            except Exception:
                continue
            if not str(manifest.get("notebooklm_project_url") or "").strip():
                continue
            session_root = manifest_path.parents[1]
            for prompt_path in sorted((session_root / "prompt_workspace" / "prompt_queue").glob("*.md")):
                sent_path = prompt_path.with_suffix(prompt_path.suffix + ".sent")
                if sent_path.exists():
                    continue
                result.append((session_root.name, prompt_path))
                if len(result) >= limit:
                    return result
        return result

    def send_queued_prompt(self, *, session_id: str, prompt_path: Path) -> dict[str, Any]:
        package = self.prepare_package(session_id=session_id)
        notebook_url = str(package.manifest.get("notebooklm_project_url") or "").strip()
        if not notebook_url:
            raise RuntimeError(f"NotebookLM project URL is not set for session_id={session_id}")
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt:
            prompt_path.with_suffix(prompt_path.suffix + ".sent").write_text("empty prompt\n", encoding="utf-8")
            return {"status": "skipped_empty_prompt", "session_id": session_id, "prompt_path": str(prompt_path)}
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required for NotebookLM prompt sending. Run: python -m pip install playwright") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        executable = self._browser_executable()
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir.resolve()),
                executable_path=str(executable),
                headless=False,
                viewport={"width": 1440, "height": 1000},
                args=["--no-first-run", "--disable-default-apps"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(notebook_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(5_000)
                self._send_prompt(page, prompt)
                sent_path = prompt_path.with_suffix(prompt_path.suffix + ".sent")
                sent_path.write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
                run_path = self.write_run_log(
                    package,
                    status="prompt_sent",
                    notebooklm_project_url=notebook_url,
                    notes=[f"Queued prompt sent: {prompt_path.name}"],
                )
                return {
                    "status": "prompt_sent",
                    "session_id": session_id,
                    "notebooklm_project_url": notebook_url,
                    "prompt_path": str(prompt_path),
                    "run_path": str(run_path),
                }
            finally:
                context.close()

    def pull_remote_pending(self, *, remote: RemoteTaskExtractorConfig, limit: int = 5) -> list[str]:
        client = self._connect_remote(remote)
        downloaded: list[str] = []
        try:
            sftp = client.open_sftp()
            try:
                names = sorted(sftp.listdir(remote.remote_exports_root))
            except FileNotFoundError:
                return []
            for name in names:
                if name.endswith(".zip"):
                    continue
                remote_session_root = f"{remote.remote_exports_root.rstrip('/')}/{name}"
                manifest_remote = f"{remote_session_root}/machine_bundle/handoff_manifest.json"
                try:
                    with sftp.open(manifest_remote, "r") as handle:
                        manifest = json.loads(handle.read().decode("utf-8"))
                except Exception:
                    continue
                if not self._is_pending_manifest(manifest):
                    continue
                local_session_root = self.exports_root / name
                self._download_remote_tree(sftp, remote_session_root, local_session_root)
                downloaded.append(name)
                if len(downloaded) >= limit:
                    break
            sftp.close()
        finally:
            client.close()
        return downloaded

    def push_remote_metadata(self, *, remote: RemoteTaskExtractorConfig, session_id: str) -> None:
        session_root = self.exports_root / session_id
        client = self._connect_remote(remote)
        try:
            sftp = client.open_sftp()
            remote_bundle = f"{remote.remote_exports_root.rstrip('/')}/{session_id}/machine_bundle"
            for name in ["handoff_manifest.json", "notebooklm_run.json"]:
                local_path = session_root / "machine_bundle" / name
                if local_path.exists():
                    sftp.put(str(local_path), f"{remote_bundle}/{name}")
            sftp.close()
        finally:
            client.close()

    def write_run_log(
        self,
        package: NotebookLMPackage,
        *,
        status: str,
        notebooklm_project_url: str = "",
        notes: list[str] | None = None,
    ) -> Path:
        run = {
            "session_id": package.session_id,
            "status": status,
            "notebooklm_project_title": package.title,
            "notebooklm_project_url": notebooklm_project_url,
            "source_files": [path.name for path in package.source_files],
            "source_count": len(package.source_files),
            "prompt_file": str(package.prompt_path.relative_to(package.root)),
            "notes": notes or [],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        run_path = package.root / "machine_bundle" / "notebooklm_run.json"
        run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return run_path

    @staticmethod
    def _is_pending_manifest(manifest: dict[str, Any]) -> bool:
        if str(manifest.get("notebooklm_project_url") or "").strip():
            return False
        status = str(manifest.get("status") or "").strip()
        return status in {"exported", "ready", "published"} or not status

    @staticmethod
    def _knowledge_overview_markdown(*, manifest: dict[str, Any]) -> str:
        objects = manifest.get("objects") or []
        lines = [
            "# Company Knowledge Base",
            "",
            "This NotebookLM source bundle mirrors the canonical company knowledge base.",
            "Use it as an external research layer. Do not treat NotebookLM as the source of truth.",
            "",
            f"Objects: {len(objects)}",
            "",
            "## Operating Rules",
            "",
            "- Answer only from uploaded sources.",
            "- Cite object IDs and source events.",
            "- When proposing corrections, identify affected systems, features, instructions, and task cases.",
            "- Return changes as a reviewable proposal, not as an invisible final edit.",
        ]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _knowledge_start_prompt() -> str:
        return "\n".join(
            [
                "You are an external research layer for the company knowledge base.",
                "Build an internal map of systems, features, instructions, task cases, source events, and contradictions.",
                "When asked a follow-up prompt, return:",
                "1. direct answer;",
                "2. relevant source IDs;",
                "3. possible gaps or contradictions;",
                "4. proposed canonical changes, if the prompt asks to correct knowledge.",
                "Do not invent facts outside uploaded sources.",
            ]
        ) + "\n"

    @classmethod
    def _knowledge_grouped_sources(cls, *, knowledge_root: Path, manifest: dict[str, Any]) -> list[tuple[str, str]]:
        buckets: dict[str, list[tuple[str, str]]] = {
            "systems": [],
            "features": [],
            "instructions": [],
            "task_cases": [],
        }
        for item in manifest.get("objects") or []:
            raw_path = Path(str(item.get("path") or ""))
            source_path = raw_path if raw_path.is_absolute() else knowledge_root / raw_path
            if not source_path.exists():
                continue
            object_id = str(item.get("object_id") or source_path.parent.name or source_path.stem)
            if object_id.startswith("system__"):
                bucket = "systems"
            elif object_id.startswith("feature__"):
                bucket = "features"
            elif object_id.startswith("instruction__"):
                bucket = "instructions"
            else:
                bucket = "task_cases"
            body = source_path.read_text(encoding="utf-8")
            buckets[bucket].append((object_id, body))

        grouped: list[tuple[str, str]] = []
        for bucket in ["systems", "features", "instructions"]:
            entries = buckets[bucket]
            if not entries:
                continue
            grouped.append((f"01_{bucket}.md", cls._knowledge_group_markdown(title=bucket, entries=entries)))
        task_entries = buckets["task_cases"]
        chunk_size = 15
        for offset in range(0, len(task_entries), chunk_size):
            chunk = task_entries[offset : offset + chunk_size]
            part = int(offset / chunk_size) + 1
            grouped.append((f"02_task_cases_part_{part:02d}.md", cls._knowledge_group_markdown(title=f"task_cases_part_{part:02d}", entries=chunk)))
        return grouped

    @staticmethod
    def _knowledge_group_markdown(*, title: str, entries: list[tuple[str, str]]) -> str:
        lines = [
            f"# {title.replace('_', ' ').title()}",
            "",
            "Each section below is one canonical knowledge object. Preserve object IDs when citing or proposing changes.",
        ]
        for object_id, body in entries:
            lines.extend(
                [
                    "",
                    "---",
                    "",
                    f"## Object ID: `{object_id}`",
                    "",
                    body.strip(),
                ]
            )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _safe_file_part(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip().lower())
        return cleaned[:120].strip("_") or "source"

    @staticmethod
    def _connect_remote(remote: RemoteTaskExtractorConfig) -> Any:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("Paramiko is required for remote watch mode. Run: python -m pip install paramiko") from exc
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": remote.host,
            "username": remote.username,
            "port": remote.port,
            "timeout": 20,
            "banner_timeout": 20,
            "auth_timeout": 20,
        }
        if remote.password:
            kwargs["password"] = remote.password
        client.connect(**kwargs)
        return client

    def _download_remote_tree(self, sftp: Any, remote_root: str, local_root: Path) -> None:
        local_root.mkdir(parents=True, exist_ok=True)
        for item in sftp.listdir_attr(remote_root):
            remote_path = f"{remote_root.rstrip('/')}/{item.filename}"
            local_path = local_root / item.filename
            if self._remote_is_dir(item):
                self._download_remote_tree(sftp, remote_path, local_path)
            else:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote_path, str(local_path))

    @staticmethod
    def _remote_is_dir(item: Any) -> bool:
        import stat

        return stat.S_ISDIR(item.st_mode)

    def _upload_sources(self, page: Any, source_files: list[Path]) -> None:
        absolute_files = [str(path.resolve()) for path in source_files]
        with page.expect_file_chooser(timeout=15_000) as chooser_info:
            self._click_button_by_text(page, contains="Загрузить файлы", fallback_contains="upload")
        chooser = chooser_info.value
        chooser.set_files(absolute_files)

    @staticmethod
    def _wait_for_sources(page: Any, source_files: list[Path]) -> list[str]:
        expected = [path.name for path in source_files]
        deadline_ms = 120_000
        step_ms = 2_000
        elapsed = 0
        uploaded: list[str] = []
        while elapsed <= deadline_ms:
            body = page.locator("body").inner_text(timeout=10_000)
            uploaded = [name for name in expected if name in body]
            if len(uploaded) == len(expected):
                return uploaded
            page.wait_for_timeout(step_ms)
            elapsed += step_ms
        missing = sorted(set(expected) - set(uploaded))
        raise TimeoutError(f"NotebookLM did not show all uploaded sources. Missing: {', '.join(missing)}")

    @staticmethod
    def _send_prompt(page: Any, prompt: str) -> None:
        textareas = page.locator("textarea")
        if textareas.count() == 0:
            raise RuntimeError("NotebookLM prompt textarea not found.")
        textarea = textareas.last
        textarea.fill(prompt)
        textarea.press("Enter")
        page.wait_for_timeout(20_000)

    @staticmethod
    def _click_button_by_text(
        page: Any,
        *,
        exact: str | None = None,
        contains: str | None = None,
        fallback_contains: str | None = None,
    ) -> None:
        buttons = page.locator("button")
        count = buttons.count()
        fallback_index: int | None = None
        for index in range(count):
            button = buttons.nth(index)
            text = "\n".join(part.strip() for part in button.inner_text(timeout=5_000).splitlines() if part.strip())
            if exact and text == exact:
                button.click(timeout=15_000)
                return
            if contains and contains in text:
                button.click(timeout=15_000)
                return
            if fallback_contains and fallback_contains in text:
                fallback_index = index
        if fallback_index is not None:
            buttons.nth(fallback_index).click(timeout=15_000)
            return
        raise RuntimeError(f"NotebookLM button not found: exact={exact!r}, contains={contains!r}")

    def _write_notebook_url(self, *, package: NotebookLMPackage, notebook_url: str, status: str) -> None:
        manifest = dict(package.manifest)
        manifest["status"] = status
        manifest["notebooklm_project_url"] = notebook_url
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        package.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        package.manifest.update(manifest)

    def _package_root(self, session_id: str) -> Path:
        direct = self.exports_root / session_id
        if direct.exists():
            return direct
        zip_matches = sorted(self.exports_root.glob(f"task_extractor_{session_id}__*__notebooklm.zip"))
        if not zip_matches:
            raise FileNotFoundError(f"Task Extractor export not found for session_id={session_id} in {self.exports_root}")
        target = self.exports_root / session_id
        target.mkdir(parents=True, exist_ok=True)
        with ZipFile(zip_matches[-1]) as archive:
            archive.extractall(target)
        return target

    def _source_files(self, *, package_root: Path, manifest: dict[str, Any]) -> list[Path]:
        raw_files = manifest.get("source_bundle_files") or []
        if not raw_files:
            raw_files = [str(path.relative_to(package_root)) for path in sorted((package_root / "source_bundle").glob("*.md"))]
        result: list[Path] = []
        missing: list[str] = []
        for raw in raw_files:
            path = package_root / str(raw)
            if path.exists() and path.is_file():
                result.append(path)
            else:
                missing.append(str(raw))
        if missing:
            raise FileNotFoundError(f"Missing NotebookLM source files: {', '.join(missing)}")
        if not result:
            raise FileNotFoundError(f"No NotebookLM source files found in {package_root / 'source_bundle'}")
        return result

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON file: {path}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return parsed

    @staticmethod
    def _browser_executable() -> Path:
        system = platform.system().lower()
        candidates: list[Path] = []
        if system == "windows":
            env = os.environ
            for base in [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")]:
                if not base:
                    continue
                candidates.extend(
                    [
                        Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                        Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    ]
                )
        elif system == "darwin":
            candidates.extend(
                [
                    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                ]
            )
        else:
            for name in ["google-chrome", "chromium-browser", "chromium", "microsoft-edge"]:
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError("Chrome/Edge executable not found. Install Chrome or Edge, then retry.")
