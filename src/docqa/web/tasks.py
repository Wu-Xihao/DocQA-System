"""Durable question ownership and atomic cancel-versus-result publication."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.agent.providers import make_chat_clients
from docqa.agent.sessions import SessionStore
from docqa.docbase.providers import make_providers
from docqa.docbase.storage import _io_path, load_generation, read_current
from docqa.domain.errors import DocQAError
from docqa.web.runner import STAGES, HeadlessRunner


class TaskStore:
    def __init__(self, root: Path, capacity=20):
        self.root, self.capacity = root, capacity
        _io_path(root).mkdir(parents=True, exist_ok=True)
        self.path = root / "questions.sqlite"
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DocQAError("WEB_SCHEMA_UNSUPPORTED", "Unsupported Web task schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,"
                "state TEXT NOT NULL,payload TEXT NOT NULL,stage TEXT NOT NULL,"
                "cancel_requested INTEGER NOT NULL DEFAULT 0,result TEXT,error_code TEXT,"
                "created_at REAL NOT NULL,updated_at REAL NOT NULL)"
            )
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(_io_path(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            raise DocQAError("WEB_STORAGE_FAILED", "Web task transaction failed") from exc
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise DocQAError("WEB_JOB_NOT_FOUND", "Question task was not found")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def enqueue(self, owner_id, payload):
        now, job_id = time.time(), uuid4().hex
        with self.transaction() as db:
            count = db.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if count >= self.capacity:
                raise DocQAError("WEB_QUEUE_FULL", "Question queue is full", retryable=True)
            db.execute(
                "INSERT INTO jobs(job_id,owner_id,state,payload,stage,created_at,updated_at) "
                "VALUES(?,?,'queued',?,'queued',?,?)",
                (job_id, owner_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
        return self.get(job_id, owner_id)

    def get(self, job_id, owner_id=None):
        with self.transaction() as db:
            hidden = (
                owner_id
                and db.execute("SELECT 1 FROM sqlite_master WHERE name='ws_messages'").fetchone()
                and db.execute(
                    "SELECT 1 FROM ws_messages m JOIN ws_conversations c ON c.id=m.conversation WHERE m.job_id=? AND c.deleted=1",
                    (job_id,),
                ).fetchone()
            )
            if hidden:
                raise DocQAError("WEB_JOB_NOT_FOUND", "Question task was deleted")
            row = db.execute(
                "SELECT * FROM jobs WHERE job_id=?" + (" AND owner_id=?" if owner_id else ""),
                (job_id, owner_id) if owner_id else (job_id,),
            ).fetchone()
        return self.decode(row)

    def list(self, owner_id, limit=50):
        with self.transaction() as db:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (owner_id, limit),
                )
            ]

    def claim(self):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET state='running',stage='starting',updated_at=? WHERE job_id=?",
                (time.time(), row["job_id"]),
            )
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            return self.decode(row)

    def stage(self, job_id, stage):
        if stage not in {"starting", *STAGES}:
            return
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET stage=?,updated_at=? WHERE job_id=? AND state='running' "
                "AND cancel_requested=0",
                (stage, time.time(), job_id),
            )

    def cancel(self, job_id, owner_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE job_id=? AND owner_id=?", (job_id, owner_id)
            ).fetchone()
            value = self.decode(row)
            if value["state"] in {"queued", "running"}:
                db.execute(
                    "UPDATE jobs SET cancel_requested=1,state=?,stage=?,updated_at=? WHERE job_id=?",
                    (
                        "cancelled" if value["state"] == "queued" else "running",
                        "cancelled" if value["state"] == "queued" else "cancelling",
                        time.time(),
                        job_id,
                    ),
                )
        return self.get(job_id, owner_id)

    def finish(self, job_id, receipt=None, error_code=None):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            value = self.decode(row)
            if value["state"] != "running":
                return value
            if value["cancel_requested"] or error_code == "WEB_JOB_CANCELLED":
                state, receipt, error_code = "cancelled", None, "WEB_JOB_CANCELLED"
            else:
                state = (
                    "failed"
                    if error_code or (receipt and receipt["status"] == "failed")
                    else "succeeded"
                )
            db.execute(
                "UPDATE jobs SET state=?,stage=?,result=?,error_code=?,updated_at=? WHERE job_id=?",
                (
                    state,
                    state,
                    json.dumps(receipt) if receipt else None,
                    error_code,
                    time.time(),
                    job_id,
                ),
            )
        return self.get(job_id)

    def recover(self):
        # Only the server holding the OS worker lock may do this. A late child
        # can write private assets but cannot commit a public completed job.
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET state='failed',stage='failed',result=NULL,"
                "error_code='WEB_WORKER_RESTARTED',updated_at=? WHERE state='running'",
                (time.time(),),
            )

    def by_answer(self, answer_id, owner_id):
        # Do not expose a SessionStore answer merely because its random ID is known.
        with self.transaction() as db:
            exclude_deleted = ""
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ws_messages'").fetchone():
                exclude_deleted = " AND NOT EXISTS(SELECT 1 FROM ws_messages m JOIN ws_conversations c ON c.id=m.conversation WHERE m.job_id=jobs.job_id AND c.deleted=1)"
            row = db.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND state IN ('succeeded','failed') "
                "AND json_extract(result,'$.answer_id')=?" + exclude_deleted,
                (owner_id, answer_id),
            ).fetchone()
        if row is None:
            raise DocQAError("ANSWER_NOT_FOUND", "Answer was not found in this browser session")
        return self.decode(row)


class QuestionManager:
    def __init__(self, options, *, runner_factory=None):
        self.options = options
        self.store = TaskStore(options.state, options.max_pending_jobs)
        self.runner_factory = runner_factory
        self.shutdown = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.lock = None
        self.lifecycle = threading.Lock()
        self.activity = threading.RLock()
        self.active_jobs = set()

    @property
    def ready(self):
        return bool(self.thread and self.thread.is_alive() and not self.shutdown.is_set())

    def start(self):
        with self.lifecycle:
            if self.thread and self.thread.is_alive():
                raise DocQAError("WEB_ALREADY_RUNNING", "This question worker is already running")
            worker_lock = portalocker.Lock(self.options.state / ".web-worker.lock", timeout=0)
            try:
                worker_lock.acquire()
            except portalocker.exceptions.LockException as exc:
                raise DocQAError(
                    "WEB_ALREADY_RUNNING", "Another Web worker uses this state"
                ) from exc
            self.lock = worker_lock
            try:
                self.store.recover()
                self.shutdown.clear()
                self.thread = threading.Thread(
                    target=self._loop, name="docqa-web-worker", daemon=True
                )
                self.thread.start()
            except BaseException:
                self.shutdown.set()
                worker_lock.release()
                self.lock, self.thread = None, None
                raise

    def stop(self):
        self.shutdown.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=15)
        if self.thread and self.thread.is_alive():
            raise DocQAError("WEB_WORKER_STOP_FAILED", "Question worker did not finish shutdown")
        if self.lock:
            self.lock.release()

    def catalog(self, *, display_only=False):
        if display_only:
            from docqa.web.catalog import display_catalog

            return display_catalog(self.options.index, self.options.agent.retrieval)
        if not (self.options.index / "registry.sqlite").is_file():
            raise DocQAError("INDEX_NOT_FOUND", "Build or configure a knowledge index first")
        generation_id, _ = read_current(self.options.index)
        if generation_id is None:
            raise DocQAError("INDEX_NOT_FOUND", "No published knowledge index")
        with load_generation(self.options.index / "generations" / generation_id) as generation:
            from docqa.docbase.retrieval import validate_recall_selection

            validate_recall_selection(generation, self.options.agent.retrieval)
            if generation.profiles["mode"] != self.options.agent.retrieval.mode:
                raise DocQAError(
                    "PROFILE_MISMATCH", "Knowledge index and retrieval configuration differ"
                )
            docs = [
                {key: item[key] for key in ("doc_id", "source_name", "category")}
                for item in generation.members
            ]
            return dict(
                generation_id=generation_id,
                documents=docs,
                categories=sorted({item["category"] for item in docs}),
                chunks=len(generation.chunks),
                synthetic=generation.manifest["synthetic"],
                recall_routes=list(self.options.agent.retrieval.recall_routes),
                partial_documents=generation.manifest.get("partial_source_count", 0),
            )

    def submit(self, owner_id, request):
        if not self.ready:
            raise DocQAError("WORKER_UNAVAILABLE", "Question worker is not running", retryable=True)
        catalog = self.catalog(display_only=True)
        make_chat_clients(self.options.agent)  # Configuration probe; no network requests.
        make_providers(self.options.agent.retrieval)
        payload = request.model_dump(mode="json")
        types = payload.pop("doc_types", None)
        if types is not None:
            allowed = [
                d["doc_id"]
                for d in catalog["documents"]
                if Path(d["source_name"]).suffix.lower().lstrip(".") in types
                and (request.categories is None or d["category"] in request.categories)
                and (request.doc_ids is None or d["doc_id"] in request.doc_ids)
            ]
            if not allowed:
                raise DocQAError("SCOPE_EMPTY", "No documents match the selected types")
            payload["doc_ids"] = allowed
        payload["generation_id"] = catalog["generation_id"]
        job = self.store.enqueue(owner_id, payload)
        self.wakeup.set()
        return job

    def agent_state(self, job_id):
        return self.options.state / "agent" / job_id

    def answer(self, job):
        if not job["result"]:
            return None
        return SessionStore(self.agent_state(job["job_id"])).load_answer(job["result"]["answer_id"])

    def retrieval_preview(self, job):
        if job["state"] != "running":
            return []
        path = _io_path(self.agent_state(job["job_id"]) / "retrieval-preview.json")
        try:
            if path.stat().st_size > 16384:
                return []
            rows = json.loads(path.read_bytes())
            if not isinstance(rows, list) or len(rows) > 4:
                return []
            if any(
                set(r) != {"source_name", "text"}
                or not isinstance(r["source_name"], str)
                or not isinstance(r["text"], str)
                or len(r["source_name"]) > 256
                or len(r["text"]) > 360
                for r in rows
            ):
                return []
            return rows
        except (OSError, ValueError, TypeError):
            return []

    def _loop(self):
        while not self.shutdown.is_set():
            try:
                with self.activity:
                    job = self.store.claim()
                    if job is not None:
                        self.active_jobs.add(job["job_id"])
                if job is None:
                    self.wakeup.wait(0.2)
                    self.wakeup.clear()
                    continue
                try:
                    runner = (
                        self.runner_factory(job)
                        if self.runner_factory
                        else HeadlessRunner(
                            self.options.index,
                            self.agent_state(job["job_id"]),
                            self.options.agent,
                            self.options.state / "runs",
                            timeout_seconds=self.options.runner_timeout_seconds,
                        )
                    )
                    receipt = runner.run(
                        job["job_id"],
                        job["payload"],
                        cancelled=lambda: (
                            self.shutdown.is_set()
                            or self.store.get(job["job_id"])["cancel_requested"]
                        ),
                        progress=lambda stage: self.store.stage(job["job_id"], stage),
                    )
                    error_code = None
                    if receipt["status"] == "failed":
                        failed = SessionStore(self.agent_state(job["job_id"])).load_answer(
                            receipt["answer_id"]
                        )
                        error_code = failed.stop_reason
                        logging.getLogger(__name__).warning(
                            "DocQA Agent failed: job=%s code=%s", job["job_id"], error_code
                        )
                    self.store.finish(job["job_id"], receipt=receipt, error_code=error_code)
                except Exception as exc:
                    code = exc.code if isinstance(exc, DocQAError) else "RUNNER_FAILED"
                    logging.getLogger(__name__).warning(
                        "DocQA question failed: job=%s stage=%s code=%s",
                        job["job_id"],
                        self.store.get(job["job_id"])["stage"],
                        code,
                    )
                    self.store.finish(
                        job["job_id"],
                        error_code=code,
                    )
                finally:
                    with self.activity:
                        self.active_jobs.discard(job["job_id"])
            except (DocQAError, OSError, ValueError, sqlite3.Error):
                # Persistence failure makes the consumer unavailable. Never accept
                # more async work into a queue without a functioning consumer.
                self.shutdown.set()
