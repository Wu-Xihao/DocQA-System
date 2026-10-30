"""Workspace application services: durable upload outbox and checked study artifacts."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.doc2md.config import ParseOptions
from docqa.doc2md.security import inspect_snapshot, is_within
from docqa.docbase.retrieval import generation_directory, validate_recall_selection
from docqa.docbase.storage import (
    _io_path,
    _safe_path,
    generation_recall_routes,
    load_generation,
    read_current,
)
from docqa.domain.errors import DocQAError
from docqa.domain.formats import UPLOAD_FORMATS
from docqa.service.config import ServiceOptions
from docqa.service.config import load_options as load_service_options
from docqa.service.pipeline import Service
from docqa.web.media import ImageService, checksum
from docqa.web.studio import LABELS, build_content, export_bytes
from docqa.web.workspace_store import WorkspaceStore, encoded


class Workspace:
    def __init__(self, manager):
        self.manager, self.options = manager, manager.options
        self.store = WorkspaceStore(manager.store)
        self.images = ImageService(manager, self.store)
        from docqa.web.deletion import DeletionService

        self.deletions = DeletionService(self)
        self.shutdown = threading.Event()
        self.thread = self.process = None
        self.visual_thread = None
        self.worker_error = None
        self._last_spawn = 0
        self._last_monitor_poll = 0
        self._members_generation, self._members = None, {}
        self._display_catalogs = {}
        self._display_lock = threading.Lock()
        from docqa.web.search import SearchMetrics

        self.search_metrics = SearchMetrics()
        self.upload_root = Path(
            self.options.upload_root or self.options.state / "uploaded-sources"
        ).resolve()
        self.enabled = bool(
            self.options.service_state
            and self.options.index.resolve() == (self.options.service_state / "index").resolve()
            and self.options.service_mode != "disabled"
        )

    def start(self):
        # The manager holds the Web state lock before workspace startup. Any
        # leftover receiving state belongs to the interrupted previous server.
        with self.manager.store.transaction() as db:
            interrupted = db.execute(
                "SELECT owner,payload FROM ws_objects WHERE kind='upload' AND json_extract(payload,'$.state')='uploading'"
            ).fetchall()
        for row in interrupted:
            record = json.loads(row["payload"])
            record.update(state="upload_failed", error_code="UPLOAD_INCOMPLETE")
            self.store.object_put("upload", row["owner"], record)
        for kind in ("artifact_job", "export", "visual_check"):
            with self.manager.store.transaction() as db:
                rows = db.execute(
                    "SELECT owner,payload FROM ws_objects WHERE kind=? AND json_extract(payload,'$.state') IN ('generating','running')",
                    (kind,),
                ).fetchall()
            for row in rows:
                record = json.loads(row["payload"])
                record["state"] = "queued"
                self.store.object_put(kind, row["owner"], record)
        self.shutdown.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="docqa-workspace")
        self.thread.start()
        self.visual_thread = threading.Thread(
            target=self._visual_loop, daemon=True, name="docqa-visual-checks"
        )
        self.visual_thread.start()

    def stop(self):
        self.shutdown.set()
        if self.visual_thread:
            self.visual_thread.join(timeout=5)
        if self.thread:
            self.thread.join(timeout=5)
        if self.process and self.process.poll() is None:
            # Only our one-shot child is stopped; an independent Service is
            # never signalled. Its durable jobs recover at the next worker start.
            from docqa.doc2md.adapters import _terminate_tool

            _terminate_tool(self.process)

    def catalog(self, generation=None):
        selected = generation_directory(self.options.index, generation)
        with load_generation(selected) as current:
            validate_recall_selection(current, self.options.agent.retrieval)
            return {
                "generation_id": current.manifest["generation_id"],
                "chunks": len(current.chunks),
                "recall_routes": list(generation_recall_routes(current.profiles)),
                "documents": [
                    {
                        "doc_id": item["doc_id"],
                        "source_name": item["source_name"],
                        "category": item["category"],
                        "artifact_id": item["artifact_id"],
                        "quality": (item.get("source_quality") or {}).get("index_mode", "complete"),
                    }
                    for item in current.members
                ],
            }

    def display_catalog(self, generation=None):
        """Cache immutable list metadata, never the evidence/submission checks."""
        directory = generation_directory(self.options.index, generation)
        signature = tuple(
            (
                _io_path(directory / name).stat().st_size,
                _io_path(directory / name).stat().st_mtime_ns,
            )
            for name in ("manifest.json", "members.json", "profiles.json", "catalog.sqlite")
        )
        key = str(directory)
        with self._display_lock:
            cached = self._display_catalogs.get(key)
            if cached is None or cached[0] != signature:
                from docqa.web.catalog import display_catalog

                value = display_catalog(
                    self.options.index, self.options.agent.retrieval, directory.name
                )
                self._display_catalogs[key] = (signature, value)
                if len(self._display_catalogs) > 16:
                    self._display_catalogs.pop(next(iter(self._display_catalogs)))
            return copy.deepcopy(self._display_catalogs[key][1])

    def document_content(self, identity, generation=None):
        """Read Markdown only from the checked, immutable generation package."""
        artifact, _, _ = self.document_source(identity, generation)
        path = _safe_path(artifact, "document.md")
        if _io_path(path).stat().st_size > self.options.max_asset_bytes:
            raise DocQAError("ASSET_TOO_LARGE", "Markdown exceeds preview limit")
        return _io_path(path).read_bytes()

    def document_source(self, identity, generation=None):
        from docqa.web.documents import document_package

        return document_package(self.options.index, identity, generation)

    def submit(self, conversation, owner, value, *, chat_history_override=None):
        previous = self.store.previous_submission(conversation, owner, value)
        if previous:
            return self.manager.store.get(previous, owner)
        selected = self.store.get(conversation, owner)
        if value.mode == "chat":
            if not self.manager.ready:
                raise DocQAError("WORKER_UNAVAILABLE", "Question worker is not running")
            if self.options.agent.mode != "demo":
                from docqa.agent.providers import HttpChatClient

                HttpChatClient(self.options.agent.generator)  # Only conversation API is required.
            job_id = self.store.submit(
                conversation, owner, value, None, None, chat_history_override=chat_history_override
            )
            self.manager.wakeup.set()
            return self.manager.store.get(job_id, owner)
        catalog = self.display_catalog(selected["generation_id"])
        available = [
            d
            for d in catalog["documents"]
            if (value.categories is None or d["category"] in value.categories)
            and (
                value.doc_types is None
                or Path(d["source_name"]).suffix.lower().lstrip(".") in value.doc_types
            )
        ]
        if value.doc_ids is not None:
            ids = {d["doc_id"] for d in available}
            if not set(value.doc_ids).issubset(ids):
                raise DocQAError(
                    "SCOPE_CHANGED", "Selected documents are outside this conversation version"
                )
            available = [d for d in available if d["doc_id"] in value.doc_ids]
        if not available:
            raise DocQAError("SCOPE_EMPTY", "Select at least one available document")
        from docqa.agent.providers import make_chat_clients
        from docqa.docbase.providers import make_providers

        make_chat_clients(self.options.agent)
        make_providers(self.options.agent.retrieval)
        job_id = self.store.submit(
            conversation, owner, value, catalog["generation_id"], [d["doc_id"] for d in available]
        )
        self.manager.wakeup.set()
        return self.manager.store.get(job_id, owner)

    def service(self):
        if not self.enabled:
            raise DocQAError("UPLOAD_NOT_CONFIGURED", "Uploads require a managed Service index")
        if self.options.agent.mode == "http" and self.options.service_config is None:
            raise DocQAError(
                "UPLOAD_NOT_CONFIGURED", "Real ingestion needs the existing Service configuration"
            )
        opts = (
            load_service_options(self.options.service_config)
            if self.options.service_config
            else ServiceOptions(
                allowed_source_roots=[self.upload_root],
                retrieval=self.options.agent.retrieval,
                parse=ParseOptions(
                    ocr_mode="disabled" if self.options.agent.mode == "demo" else "auto"
                ),
                settle_seconds=0,
            )
        )
        if not any(is_within(self.upload_root, p.resolve()) for p in opts.allowed_source_roots):
            raise DocQAError(
                "UPLOAD_ROOT_NOT_ALLOWED", "Add the configured upload directory to Service roots"
            )
        if opts.retrieval.model_dump() != self.options.agent.retrieval.model_dump():
            raise DocQAError("PROFILE_MISMATCH", "Service and Web retrieval configurations differ")
        return Service(self.options.service_state, options=opts)

    def create_upload(self, owner, value):
        self.service()  # Validate configuration before receiving any document.
        suffix = Path(value.name).suffix.lower()
        if suffix.lstrip(".") not in UPLOAD_FORMATS:
            raise DocQAError("FORMAT_UNSUPPORTED", "Upload PDF, DOCX, PPTX, XLSX or CSV")
        if value.size > self.options.max_upload_bytes:
            raise DocQAError("BODY_TOO_LARGE", "File exceeds upload limit")
        pending = [
            u
            for u in self.store.objects("upload", owner, 1000)
            if u["state"] in {"selected", "uploading", "received"}
        ]
        if len(pending) >= 20:
            raise DocQAError("WEB_QUEUE_FULL", "There are too many unfinished uploads")
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.name).strip(" .")
        safe = (Path(safe).stem[:70] or "document") + suffix
        if Path(safe).stem.upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }:
            safe = "document_" + safe
        record = {
            "id": uuid4().hex,
            "name": value.name,
            "relative_path": value.relative_path,
            "safe_name": safe,
            "size": value.size,
            "category": value.category,
            "state": "selected",
            "received": 0,
            "error_code": None,
            "created_at": time.time(),
            "doc_id": None,
        }
        return self.store.object_put("upload", owner, record)

    def public_upload(self, value):
        return {k: v for k, v in value.items() if k not in {"safe_name", "path", "sha256"}}

    def staging(self, identity):
        root = self.options.state / "uploads/staging"
        _io_path(root).mkdir(parents=True, exist_ok=True)
        return _safe_path(root, identity + ".part")

    async def receive_upload(self, owner, identity, request):
        record = self.store.object_get("upload", identity, owner)
        if record["state"] not in {"selected", "upload_failed", "received"}:
            raise DocQAError("UPLOAD_ALREADY_COMMITTED", "Upload is already being processed")
        path = self.staging(identity)
        try:
            with portalocker.Lock(_io_path(path.with_suffix(".lock")), timeout=0):
                record.update(state="uploading", received=0, error_code=None)
                self.store.object_put("upload", owner, record)
                digest, size = sha256(), 0
                try:
                    with _io_path(path).open("wb") as stream:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > min(record["size"], self.options.max_upload_bytes):
                                raise DocQAError(
                                    "BODY_TOO_LARGE", "Upload exceeded its declared size"
                                )
                            stream.write(chunk)
                            digest.update(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if size != record["size"]:
                        raise DocQAError("UPLOAD_INCOMPLETE", "Uploaded byte count is incomplete")
                    inspect_snapshot(
                        path, Path(record["safe_name"]).suffix[1:], self.service().options.parse
                    )
                    record.update(state="received", received=size, sha256=digest.hexdigest())
                    duplicate = next(
                        (
                            u
                            for u in self.store.objects("upload", owner, 1000)
                            if u["id"] != identity
                            and u.get("sha256") == record["sha256"]
                            and u["state"] in {"ready", "ready_with_gaps"}
                        ),
                        None,
                    )
                    if duplicate:
                        record["duplicate_of"] = duplicate["id"]
                    self.store.object_put("upload", owner, record)
                except Exception as error:
                    record.update(
                        state="upload_failed",
                        received=size,
                        error_code=error.code
                        if isinstance(error, DocQAError)
                        else "UPLOAD_INCOMPLETE",
                    )
                    self.store.object_put("upload", owner, record)
                    raise
        except portalocker.exceptions.LockException as error:
            raise DocQAError("UPLOAD_BUSY", "Upload is already in progress") from error
        return self.public_upload(record)

    def commit_upload(self, owner, identity):
        record = self.store.object_get("upload", identity, owner)
        if record["state"] in {
            "committed",
            "queued",
            "parsing",
            "indexing",
            "ready",
            "ready_with_gaps",
            "review_required",
            "failed",
        }:
            return self.public_upload(record)
        if record["state"] != "received":
            raise DocQAError("UPLOAD_INCOMPLETE", "Finish uploading before committing")
        service = self.service()
        category = sha256(record["category"].encode()).hexdigest()[:16]
        category_root = _safe_path(self.upload_root, category)
        final = _safe_path(category_root, identity + "/" + record["safe_name"])
        _io_path(final.parent).mkdir(parents=True, exist_ok=True)
        path = self.staging(identity)
        with portalocker.Lock(_io_path(path.with_suffix(".lock")), timeout=0):
            if checksum(path) != record["sha256"]:
                raise DocQAError("SOURCE_HASH_MISMATCH", "Staged upload changed")
            temporary = final.with_suffix(final.suffix + ".part")
            # A final .part suffix is excluded by the source type policy.
            shutil.copyfile(_io_path(path), _io_path(temporary))
            _io_path(temporary).replace(_io_path(final))
            record.update(state="committed", path=str(final))
            self.store.object_put("upload", owner, record)
        self.deliver_upload(owner, record, service)
        return self.public_upload(self.store.object_get("upload", identity, owner))

    def deliver_upload(self, owner, record, service):
        path = Path(record["path"])
        source = service.add_source(
            path.parent.parent,
            category=record["category"],
            types=list(UPLOAD_FORMATS),
            recursive=True,
        )
        documents = service.list_documents()
        existing = next((d for d in documents if Path(d["path"]) == path), None)
        if existing is None:
            service.enqueue_parse(path, source_id=source["source_id"])
            existing = next(d for d in service.list_documents() if Path(d["path"]) == path)
        record.update(
            state="queued",
            doc_id=existing["doc_id"],
            source_id=source["source_id"],
            error_code=None,
        )
        self.store.object_put("upload", owner, record)

    def _spawn_service(self, service):
        if (
            self.options.service_mode != "auto"
            or (self.process and self.process.poll() is None)
            or time.monotonic() - self._last_spawn < 10
        ):
            return
        self._last_spawn = time.monotonic()
        # The child acquires Service's OS lock. If an external worker owns it,
        # the child exits safely and the existing worker processes these jobs.
        snapshot = self.options.state / "service-workspace.json"
        _io_path(snapshot).write_text(
            encoded(service.options.model_dump(mode="json")), encoding="utf-8"
        )
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "PATH", "SYSTEMDRIVE"}
        }
        environment.update(
            PYTHONPATH=str(Path(__file__).resolve().parents[2]),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        models = [
            service.options.parse.vision,
            service.options.retrieval.dense,
            service.options.retrieval.reranker,
            service.options.retrieval.sparse,
        ]
        for model in models:
            for name in (model.api_key_env, getattr(model, "endpoint_env", None)):
                if name:
                    if name.upper() in {
                        "SYSTEMROOT",
                        "WINDIR",
                        "COMSPEC",
                        "PATHEXT",
                        "TEMP",
                        "TMP",
                        "PATH",
                        "SYSTEMDRIVE",
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "LD_PRELOAD",
                        "LD_LIBRARY_PATH",
                    } or name.upper().startswith(("PYTHON", "DYLD_")):
                        raise DocQAError("INVALID_CONFIG", "Model variable names a runtime setting")
                    if name in os.environ:
                        environment[name] = os.environ[name]
        self.process = subprocess.Popen(
            [sys.executable, "-m", "docqa.web.ingestion_worker", str(snapshot), str(service.root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def refresh_uploads(self):
        records = self.store.objects("upload", limit=1000)
        active = [
            r for r in records if r["state"] in {"committed", "queued", "parsing", "indexing"}
        ]
        if not active:
            return
        service = self.service()
        pending_jobs = (
            service.queue.list(state="queued", limit=100)
            or service.queue.list(state="running", limit=100)
            or service.queue.list(state="retry_wait", limit=100)
        )
        failed_revision = service.registry.meta("failed_build_revision")
        build_failed = (
            failed_revision is not None
            and failed_revision == service.registry.meta("desired_revision")
            and not pending_jobs
        )
        docs = {d["doc_id"]: d for d in service.list_documents()}
        generation = (
            read_current(self.options.index)[0]
            if (self.options.index / "registry.sqlite").is_file()
            else None
        )
        if generation != self._members_generation:
            self._members = {}
            if generation:
                with load_generation(generation_directory(self.options.index, generation)) as index:
                    self._members = {m["doc_id"]: m for m in index.members}
            self._members_generation = generation
        with self.manager.store.transaction() as db:
            owners = {
                row["id"]: row["owner"]
                for row in db.execute("SELECT id,owner FROM ws_objects WHERE kind='upload'")
            }
        waiting = False
        build_job = service.queue.latest("build")
        for record in active:
            owner = owners[record["id"]]
            record.pop("progress", None)
            if record["state"] == "committed":
                self.deliver_upload(owner, record, service)
                waiting = True
                continue
            doc = docs.get(record.get("doc_id"))
            member = self._members.get(record.get("doc_id"))
            if (
                member
                and doc
                and Path(doc.get("latest_attempt_artifact") or doc.get("artifact_dir") or "").name
                == member["artifact_id"]
            ):
                record.update(
                    state="ready_with_gaps" if member.get("source_quality") else "ready",
                    error_code=None,
                )
            elif build_failed:
                record.update(state="failed", error_code="INDEX_BUILD_FAILED")
            elif doc and doc["state"] in {"failed", "review_required"}:
                # A build may still publish native-only content after parsing.
                pending_build = bool(pending_jobs)
                if pending_build:
                    record["state"] = "indexing"
                    waiting = True
                else:
                    record.update(state=doc["state"], error_code=doc.get("error_code"))
            else:
                parse_job = (
                    service.queue.latest(f"parse:{doc['doc_id']}:{doc['target_version']}")
                    if doc
                    else None
                )
                if doc and doc["state"] == "ready":
                    record["state"] = "indexing"
                elif parse_job and parse_job["state"] in {"queued", "retry_wait"}:
                    record["state"] = "queued"
                    record["progress"] = {
                        "stage": parse_job["state"],
                        "current": None,
                        "total": None,
                    }
                else:
                    record["state"] = "parsing"
                    if parse_job and parse_job["state"] == "running":
                        record["progress"] = (parse_job.get("result") or {}).get("progress")
                waiting = True
            if record["state"] == "indexing" and build_job and build_job["state"] == "running":
                record["progress"] = (build_job.get("result") or {}).get("progress")
            self.store.object_put("upload", owner, record)
        if waiting:
            self._spawn_service(service)

    def create_artifact(self, owner, value, *, previous_id=None, version=1):
        with self.store.mutations:
            return self._create_artifact(owner, value, previous_id=previous_id, version=version)

    def _create_artifact(self, owner, value, *, previous_id=None, version=1):
        self.store.get(value.conversation_id, owner)
        job = self.manager.store.by_answer(value.answer_id, owner)
        with self.manager.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM ws_messages WHERE conversation=? AND job_id=?",
                (value.conversation_id, job["job_id"]),
            ).fetchone():
                raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Answer is outside this conversation")
        answer = self.manager.answer(job)
        build_content(answer, value.kind)  # Fail clearly before queueing impossible output.
        record = {
            "id": uuid4().hex,
            "state": "queued",
            "stage": "queued",
            **value.model_dump(mode="json"),
            "created_at": time.time(),
            "error_code": None,
            "previous_id": previous_id,
            "version": version,
        }
        self.store.object_put("artifact_job", owner, record)
        return record

    def artifact(self, owner, identity):
        record = self.store.object_get("artifact", identity, owner)
        path = _safe_path(self.options.state / "studio", identity + "/artifact.json")
        if checksum(path) != record["sha256"]:
            raise DocQAError("SOURCE_HASH_MISMATCH", "Artifact integrity validation failed")
        return json.loads(_io_path(path).read_bytes())

    def edit_artifact(self, owner, identity, value):
        with self.store.mutations:
            original = self.artifact(owner, identity)
            self.store.get(original["conversation_id"], owner)
            job = self.manager.store.by_answer(original["answer_id"], owner)
            answer = self.manager.answer(job)
            if value.plan is not None:
                if original["kind"] not in {"mindmap", "slides"}:
                    raise DocQAError(
                        "ARTIFACT_PLAN_INVALID", "Section editing supports mindmaps and slides"
                    )
                value.plan.check_claims(c.claim_id for c in answer.claims)
                content = build_content(answer, original["kind"], value.plan)
            else:
                content = copy.deepcopy(original["content"])
            content["organization"] = "user_editorial"
            content.pop("organization_notice", None)
            artifact = {
                **original,
                "id": uuid4().hex,
                "title": value.title,
                "content": content,
                "version": original["version"] + 1,
                "previous_id": identity,
                "created_at": time.time(),
            }
            path = _safe_path(self.options.state / "studio", artifact["id"] + "/artifact.json")
            _io_path(path.parent).mkdir(parents=True, exist_ok=True)
            raw = encoded(artifact).encode()
            _io_path(path).write_bytes(raw)
            self.store.object_put(
                "artifact",
                owner,
                {k: v for k, v in artifact.items() if k != "content"}
                | {"sha256": sha256(raw).hexdigest()},
            )
            return artifact

    def process_artifacts(self):
        with self.manager.store.transaction() as db:
            rows = [
                (row["owner"], json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT owner,payload FROM ws_objects WHERE kind='artifact_job' AND json_extract(payload,'$.state')='queued' ORDER BY created LIMIT 1"
                )
            ]
        for owner, record in rows:
            record.update(state="generating", stage="validating")
            if not self.store.object_update("artifact_job", owner, record):
                continue
            try:
                job = self.manager.store.by_answer(record["answer_id"], owner)
                answer = self.manager.answer(job)
                from docqa.agent.sessions import SessionStore

                SessionStore(self.manager.agent_state(job["job_id"])).resolve_citations(
                    answer.answer_id
                )
                plan = None
                organization = "deterministic"
                notice = None
                if record["kind"] in {"mindmap", "slides"} and not answer.synthetic:
                    from docqa.agent.providers import make_chat_clients

                    record["stage"] = "organizing"
                    if not self.store.object_update("artifact_job", owner, record):
                        continue
                    generator, _ = make_chat_clients(self.manager.options.agent)
                    prepared = build_content(answer, record["kind"])
                    try:
                        plan = generator.organize_study(
                            answer.question,
                            prepared["facts"],
                            record["kind"],
                            deadline=time.monotonic() + 35,
                        )
                        organization = "ai_editorial"
                    except DocQAError as error:
                        notice = (
                            "AI编排未完成（"
                            + error.code
                            + "）；已按现有主题整理，正文和引用仍保留。"
                        )
                content = build_content(answer, record["kind"], plan)
                content["organization"] = organization
                if notice:
                    content["organization_notice"] = notice
                identity = uuid4().hex
                artifact = {
                    "id": identity,
                    "title": record["title"]
                    or LABELS[record["kind"]] + " · " + answer.question[:40],
                    "kind": record["kind"],
                    "conversation_id": record["conversation_id"],
                    "answer_id": answer.answer_id,
                    "generation_id": answer.generation_id,
                    "version": record.get("version", 1),
                    "created_at": time.time(),
                    "content": content,
                }
                with self.store.mutations:
                    self.store.object_get("artifact_job", record["id"], owner)
                    record["output_id"] = identity
                    self.store.object_update("artifact_job", owner, record)
                    path = _safe_path(self.options.state / "studio", identity + "/artifact.json")
                    _io_path(path.parent).mkdir(parents=True, exist_ok=True)
                    raw = encoded(artifact).encode()
                    _io_path(path).write_bytes(raw)
                    self.store.object_put(
                        "artifact",
                        owner,
                        {k: v for k, v in artifact.items() if k != "content"}
                        | {"sha256": sha256(raw).hexdigest()},
                    )
                    record.update(state="ready", stage="complete", artifact_id=identity)
                    self.store.object_update("artifact_job", owner, record)
            except Exception as error:
                record.update(
                    state="failed",
                    stage="failed",
                    error_code=error.code if isinstance(error, DocQAError) else "ARTIFACT_FAILED",
                )
            self.store.object_update("artifact_job", owner, record)

    def request_visual_check(self, owner, identity):
        with self.store.mutations:
            return self._request_visual_check(owner, identity)

    def _request_visual_check(self, owner, identity):
        media = self.store.object_get("media", identity, owner)
        self.images.preview(owner, identity, "preview")
        if self.options.service_config is None:
            raise DocQAError(
                "VISION_NOT_CONFIGURED", "Visual checking requires a Service vision configuration"
            )
        vision = load_service_options(self.options.service_config).parse.vision
        if vision.mode != "http":
            raise DocQAError("VISION_NOT_CONFIGURED", "Configure a vision model first")
        profile = sha256(vision.model_dump_json().encode()).hexdigest()
        for record in self.store.objects("visual_check", owner, 100):
            if (
                record["media_id"] == identity
                and record["profile"] == profile
                and record["state"] in {"queued", "running", "ready"}
            ):
                return record
        if (
            sum(
                r["state"] in {"queued", "running"}
                for r in self.store.objects("visual_check", None, 100)
            )
            >= 4
        ):
            raise DocQAError("WEB_QUEUE_FULL", "Visual check queue is full")
        return self.store.object_put(
            "visual_check",
            owner,
            {
                "id": uuid4().hex,
                "media_id": identity,
                "answer_id": media["answer_id"],
                "profile": profile,
                "state": "queued",
                "created_at": time.time(),
            },
        )

    def process_visual_checks(self):
        for record in self.store.objects("visual_check", None, 100):
            if record["state"] != "queued":
                continue
            with self.manager.store.transaction() as db:
                row = db.execute(
                    "SELECT owner FROM ws_objects WHERE kind='visual_check' AND id=?",
                    (record["id"],),
                ).fetchone()
            if row is None:
                continue
            owner = row[0]
            record["state"] = "running"
            if not self.store.object_update("visual_check", owner, record):
                continue
            try:
                from docqa.web.visual import isolated_check

                media = self.store.object_get("media", record["media_id"], owner)
                job = self.manager.store.by_answer(record["answer_id"], owner)
                answer = self.manager.answer(job)
                citation = next(
                    c for c in answer.citations if c.citation_id == media["citation_id"]
                )
                claims = [
                    {"claim_id": c.claim_id, "text": c.text}
                    for c in answer.claims
                    if c.claim_id in citation.claim_ids
                ]
                vision = load_service_options(self.options.service_config).parse.vision
                if sha256(vision.model_dump_json().encode()).hexdigest() != record["profile"]:
                    raise DocQAError(
                        "VISION_CONFIG_CHANGED", "Vision configuration changed; request a new check"
                    )
                with tempfile.TemporaryDirectory(prefix="docqa-visual-") as directory:
                    image = Path(directory) / "source.png"
                    image.write_bytes(self.images.preview(owner, record["media_id"], "preview"))
                    record["result"] = isolated_check(
                        image, answer.question, claims, vision, cancelled=self.shutdown.is_set
                    )
                record["state"] = "ready"
            except Exception as error:
                record.update(
                    state="failed",
                    error_code=error.code
                    if isinstance(error, DocQAError)
                    else "VISION_CHECK_FAILED",
                )
            self.store.object_update("visual_check", owner, record)
            break

    def process_exports(self):
        with self.manager.store.transaction() as db:
            rows = [
                (row["owner"], json.loads(row["payload"]))
                for row in db.execute(
                    "SELECT owner,payload FROM ws_objects WHERE kind='export' AND json_extract(payload,'$.state')='queued' ORDER BY created LIMIT 1"
                )
            ]
        for owner, record in rows:
            record["state"] = "generating"
            if not self.store.object_update("export", owner, record):
                continue
            try:
                raw, mime = self.export(owner, record["artifact_id"], record["format"])
                with self.store.mutations:
                    self.store.object_get("export", record["id"], owner)
                    root = self.options.state / "studio/exports"
                    _io_path(root).mkdir(parents=True, exist_ok=True)
                    path = _safe_path(root, record["id"] + "." + record["format"])
                    _io_path(path).write_bytes(raw)
                    record.update(
                        state="ready", mime=mime, sha256=sha256(raw).hexdigest(), size=len(raw)
                    )
                    self.store.object_update("export", owner, record)
            except Exception as error:
                record.update(
                    state="failed",
                    error_code=error.code if isinstance(error, DocQAError) else "EXPORT_FAILED",
                )
            self.store.object_update("export", owner, record)

    def export(self, owner, identity, format_name):
        artifact = self.artifact(owner, identity)
        raw, mime = export_bytes(artifact, format_name)
        if len(raw) > self.options.max_source_download_bytes:
            raise DocQAError("ASSET_TOO_LARGE", "Export exceeds limit")
        return raw, mime

    def _loop(self):
        while not self.shutdown.wait(1):
            try:
                self.deletions.process()
                from docqa.web.maintenance import poll_monitors

                poll_monitors(self)
                self.process_artifacts()
                self.process_exports()
                self.refresh_uploads()
                self.worker_error = None
            except Exception as error:
                self.worker_error = (
                    error.code if isinstance(error, DocQAError) else "WORKSPACE_WORKER_FAILED"
                )

    def _visual_loop(self):
        while not self.shutdown.wait(0.35):
            try:
                self.process_visual_checks()
            except (DocQAError, OSError, ValueError):
                # Durable jobs recover on restart; avoid killing unrelated maintenance.
                continue
