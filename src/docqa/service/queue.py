"""Durable local work queue with bounded retries and transactional ownership.

The service worker owns an OS lock for its lifetime. This queue deliberately never
steals work based on a heartbeat timeout: ``recover_running`` is only safe after
the caller has acquired that exclusive lock. No exception messages are persisted.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError

_STATES = frozenset({"queued", "running", "retry_wait", "succeeded", "failed", "cancelled"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_ID = re.compile(r"[0-9a-f]{32}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
        "authorization",
        "access_token",
        "refresh_token",
        "bearer_token",
        "client_secret",
        "private_key",
    }
)
_MAX_JSON_BYTES = 64 * 1024


def _error(code: str, message: str, *, retryable: bool = False) -> DocQAError:
    return DocQAError(code, message, retryable=retryable)


def _validate_json(value: dict) -> str:
    if not isinstance(value, dict):
        raise _error("SERVICE_INVALID_JOB", "Task payload and result must be JSON objects")

    def visit(node: object, depth: int) -> None:
        if depth > 32:
            raise ValueError("JSON nesting exceeds the limit")
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str) or key.casefold().replace("-", "_") in _SECRET_KEYS:
                    raise ValueError("Non-JSON keys or credential fields are not accepted")
                visit(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
        elif node is None or isinstance(node, (str, bool, int)):
            return
        elif isinstance(node, float) and math.isfinite(node):
            return
        else:
            raise ValueError("Only finite plain JSON values are accepted")

    try:
        visit(value, 0)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError("JSON exceeds the byte limit")
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError) as exc:
        raise _error(
            "SERVICE_INVALID_JOB", "Task JSON is invalid, oversized, or contains credentials"
        ) from exc


class JobQueue:
    """SQLite queue whose operations are atomic across local threads/processes."""

    def __init__(
        self,
        root: Path,
        clock: Callable[[], float] = time.time,
        *,
        max_pending: int = 1000,
        max_attempts: int = 3,
        retry_base_seconds: float = 5,
    ):
        if (
            isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or max_pending < 1
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 100
            or isinstance(retry_base_seconds, bool)
            or not isinstance(retry_base_seconds, (float, int))
            or not math.isfinite(retry_base_seconds)
            or not 0 <= retry_base_seconds <= 86400
            or not callable(clock)
        ):
            raise _error("SERVICE_INVALID_JOB", "Invalid queue limits or clock")
        self.root = Path(root).absolute()
        self.path = _io_path(self.root / "jobs.sqlite")
        self.clock = clock
        self.max_pending = max_pending
        self.max_attempts = max_attempts
        self.retry_base_seconds = float(retry_base_seconds)
        try:
            _io_path(self.root).mkdir(parents=True, exist_ok=True)
            with self._write() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, priority INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    available_at REAL NOT NULL, heartbeat_at REAL, owner TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0, result TEXT, error_code TEXT,
                    superseded_by TEXT
                )""")
                db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS jobs_waiting_dedupe
                    ON jobs(dedupe_key) WHERE state IN ('queued','retry_wait')""")
                db.execute("""CREATE INDEX IF NOT EXISTS jobs_claim
                    ON jobs(state, available_at, priority, created_at)""")
        except OSError as exc:
            raise _error("SERVICE_QUEUE_STORAGE_ERROR", "Cannot initialize task queue") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(str(self.path), timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=10000")
                yield db
        except sqlite3.Error as exc:
            busy = isinstance(exc, sqlite3.OperationalError) and any(
                word in str(exc).lower() for word in ("locked", "busy")
            )
            raise _error(
                "SERVICE_QUEUE_BUSY" if busy else "SERVICE_QUEUE_STORAGE_ERROR",
                "Task queue is temporarily busy" if busy else "Task queue storage failed",
                retryable=busy,
            ) from exc

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self) -> float:
        value = self.clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
        ):
            raise _error("SERVICE_INVALID_JOB", "Queue clock must return finite epoch seconds")
        return float(value)

    @staticmethod
    def _id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise _error("SERVICE_INVALID_JOB", "Invalid task identifier")

    @staticmethod
    def _owner(owner: str) -> None:
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
            raise _error("SERVICE_INVALID_JOB", "Invalid worker identifier")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        result = dict(row)
        try:
            result["payload"] = json.loads(result["payload"])
            result["result"] = (
                json.loads(result["result"]) if result["result"] is not None else None
            )
            if not isinstance(result["payload"], dict) or (
                result["result"] is not None and not isinstance(result["result"], dict)
            ):
                raise ValueError("Invalid stored JSON")
        except (ValueError, TypeError) as exc:
            raise _error("SERVICE_STATE_CORRUPT", "Stored task JSON is invalid") from exc
        return result

    @staticmethod
    def _row(db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise _error("SERVICE_JOB_NOT_FOUND", "Task does not exist")
        return row

    def _owned(self, db: sqlite3.Connection, job_id: str, owner: str) -> sqlite3.Row:
        self._id(job_id)
        self._owner(owner)
        row = self._row(db, job_id)
        if row["state"] != "running" or row["owner"] != owner:
            raise _error("SERVICE_JOB_NOT_OWNED", "Task is not running under this worker")
        return row

    @staticmethod
    def _waiting(db: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM jobs WHERE dedupe_key=? AND state IN ('queued','retry_wait')",
            (dedupe_key,),
        ).fetchone()

    def _capacity(self, db: sqlite3.Connection) -> None:
        count = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','retry_wait')"
        ).fetchone()[0]
        if count >= self.max_pending:
            raise _error(
                "SERVICE_QUEUE_FULL", "Task queue reached its active task limit", retryable=True
            )

    def enqueue(self, kind: str, payload: dict, dedupe_key: str, priority: int = 100) -> dict:
        try:
            key_bytes = dedupe_key.encode("utf-8") if isinstance(dedupe_key, str) else b""
        except UnicodeError as exc:
            raise _error("SERVICE_INVALID_JOB", "Deduplication key is not valid UTF-8") from exc
        if (
            not isinstance(kind, str)
            or not _KIND.fullmatch(kind)
            or not isinstance(dedupe_key, str)
            or not dedupe_key.strip()
            or len(key_bytes) > 1024
            or isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -1000000 <= priority <= 1000000
        ):
            raise _error("SERVICE_INVALID_JOB", "Invalid task kind, deduplication key, or priority")
        encoded = _validate_json(payload)
        now = self._now()
        with self._write() as db:
            conflict = db.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key=? AND kind<>? AND state IN ('queued','running','retry_wait') LIMIT 1",
                (dedupe_key, kind),
            ).fetchone()
            if conflict:
                raise _error(
                    "SERVICE_DEDUPE_CONFLICT", "Deduplication key belongs to another task kind"
                )
            existing = self._waiting(db, dedupe_key)
            if existing is not None:
                return self._decode(existing)
            self._capacity(db)
            job_id = uuid4().hex
            db.execute(
                """INSERT INTO jobs(job_id,kind,payload,dedupe_key,priority,state,
                created_at,updated_at,available_at) VALUES(?,?,?,?,?,'queued',?,?,?)""",
                (job_id, kind, encoded, dedupe_key, priority, now, now, now),
            )
            return self._decode(self._row(db, job_id))

    def claim(self, owner: str) -> dict | None:
        self._owner(owner)
        now = self._now()
        with self._write() as db:
            # Queue limits may be lowered after a restart. Exhausted old retries
            # must not hide later runnable jobs or receive one extra attempt.
            db.execute(
                """UPDATE jobs SET state='failed',updated_at=?,error_code='SERVICE_ATTEMPTS_EXHAUSTED'
                WHERE state IN ('queued','retry_wait') AND attempt>=?""",
                (now, self.max_attempts),
            )
            row = db.execute(
                """SELECT * FROM jobs j
                WHERE j.state IN ('queued','retry_wait') AND j.available_at<=?
                AND NOT EXISTS(SELECT 1 FROM jobs r WHERE r.dedupe_key=j.dedupe_key AND r.state='running')
                ORDER BY j.priority,j.created_at,j.job_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE jobs SET state='running',owner=?,heartbeat_at=?,updated_at=?,
                attempt=attempt+1,error_code=NULL,result=NULL WHERE job_id=?""",
                (owner, now, now, row["job_id"]),
            )
            return self._decode(self._row(db, row["job_id"]))

    def heartbeat(self, job_id: str, owner: str) -> dict:
        now = self._now()
        with self._write() as db:
            self._owned(db, job_id, owner)
            db.execute(
                "UPDATE jobs SET heartbeat_at=?,updated_at=? WHERE job_id=?", (now, now, job_id)
            )
            return self._decode(self._row(db, job_id))

    def report_progress(self, job_id, owner, stage, current=None, total=None):
        """Transient stage counters; only the current worker may update them."""
        if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z_]{0,39}", stage):
            raise _error("SERVICE_INVALID_JOB", "Invalid progress stage")
        if (current, total) != (None, None) and (
            type(current) is not int
            or type(total) is not int
            or not 0 <= current <= total <= 100000000
        ):
            raise _error("SERVICE_INVALID_JOB", "Invalid progress counters")
        value = {"progress": {"stage": stage, "current": current, "total": total}}
        with self._write() as db:
            self._owned(db, job_id, owner)
            db.execute(
                "UPDATE jobs SET result=?,updated_at=? WHERE job_id=?",
                (_validate_json(value), self._now(), job_id),
            )

    def finish(self, job_id: str, owner: str, result: dict) -> dict:
        encoded = _validate_json(result)
        now = self._now()
        with self._write() as db:
            self._owned(db, job_id, owner)
            db.execute(
                """UPDATE jobs SET state='succeeded',result=?,updated_at=?,owner=NULL,
                error_code=NULL WHERE job_id=?""",
                (encoded, now, job_id),
            )
            return self._decode(self._row(db, job_id))

    def fail(self, job_id: str, owner: str, error_code: str, retryable: bool = False) -> dict:
        if (
            not isinstance(error_code, str)
            or not _CODE.fullmatch(error_code)
            or not isinstance(retryable, bool)
        ):
            raise _error(
                "SERVICE_INVALID_JOB",
                "A stable uppercase error code and boolean retry flag are required",
            )
        now = self._now()
        with self._write() as db:
            row = self._owned(db, job_id, owner)
            successor = self._waiting(db, row["dedupe_key"])
            retry = retryable and row["attempt"] < self.max_attempts and successor is None
            delay = min(self.retry_base_seconds * 2 ** (row["attempt"] - 1), 86400)
            db.execute(
                """UPDATE jobs SET state=?,updated_at=?,available_at=?,owner=NULL,
                result=NULL,error_code=?,superseded_by=? WHERE job_id=?""",
                (
                    "retry_wait" if retry else "failed",
                    now,
                    now + delay if retry else now,
                    error_code,
                    successor["job_id"] if successor else None,
                    job_id,
                ),
            )
            return self._decode(self._row(db, job_id))

    def cancel(self, job_id: str) -> dict:
        self._id(job_id)
        now = self._now()
        with self._write() as db:
            row = self._row(db, job_id)
            if row["state"] == "running":
                raise _error("SERVICE_JOB_RUNNING", "A running task cannot be cancelled")
            if row["state"] not in _TERMINAL:
                db.execute(
                    "UPDATE jobs SET state='cancelled',updated_at=?,owner=NULL WHERE job_id=?",
                    (now, job_id),
                )
            return self._decode(self._row(db, job_id))

    def retry(self, job_id: str) -> dict:
        self._id(job_id)
        now = self._now()
        with self._write() as db:
            row = self._row(db, job_id)
            if row["state"] not in {"failed", "cancelled"}:
                raise _error(
                    "SERVICE_JOB_NOT_RETRYABLE", "Only failed or cancelled tasks can be retried"
                )
            conflict = db.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key=? AND kind<>? AND state IN ('queued','running','retry_wait') LIMIT 1",
                (row["dedupe_key"], row["kind"]),
            ).fetchone()
            if conflict:
                raise _error(
                    "SERVICE_DEDUPE_CONFLICT", "Deduplication key belongs to another task kind"
                )
            existing = self._waiting(db, row["dedupe_key"])
            if existing is not None:
                return self._decode(existing)
            self._capacity(db)
            db.execute(
                """UPDATE jobs SET state='queued',attempt=0,updated_at=?,available_at=?,
                owner=NULL,heartbeat_at=NULL,result=NULL,error_code=NULL,superseded_by=NULL
                WHERE job_id=?""",
                (now, now, job_id),
            )
            return self._decode(self._row(db, job_id))

    def recover_running(self) -> int:
        """Recover interrupted work; caller MUST first own the exclusive worker lock."""
        now = self._now()
        with self._write() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE state='running' ORDER BY created_at,job_id"
            ).fetchall()
            for row in rows:
                successor = self._waiting(db, row["dedupe_key"])
                failed = row["attempt"] >= self.max_attempts or successor is not None
                db.execute(
                    """UPDATE jobs SET state=?,owner=NULL,heartbeat_at=NULL,updated_at=?,
                    available_at=?,error_code='SERVICE_WORKER_INTERRUPTED',superseded_by=? WHERE job_id=?""",
                    (
                        "failed" if failed else "queued",
                        now,
                        now,
                        successor["job_id"] if successor else None,
                        row["job_id"],
                    ),
                )
            return len(rows)

    def get(self, job_id: str) -> dict:
        self._id(job_id)
        with self._connect() as db:
            return self._decode(self._row(db, job_id))

    def latest(self, dedupe_key: str) -> dict | None:
        """Return the latest attempt record, including terminal state.

        Reconciliation must consult terminal records before recreating a desired
        target. ``rowid`` resolves tied timestamps without relying on UUID order.
        This method never retries, cancels, or otherwise changes the task.
        """
        try:
            encoded_key = dedupe_key.encode("utf-8") if isinstance(dedupe_key, str) else b""
        except UnicodeError as exc:
            raise _error("SERVICE_INVALID_JOB", "Deduplication key is not valid UTF-8") from exc
        if not isinstance(dedupe_key, str) or not dedupe_key.strip() or len(encoded_key) > 1024:
            raise _error("SERVICE_INVALID_JOB", "Invalid task deduplication key")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE dedupe_key=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            return self._decode(row) if row is not None else None

    def list(self, limit: int = 100, state: str | None = None) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise _error("SERVICE_INVALID_JOB", "Task list limit must be between 1 and 1000")
        if state is not None and (not isinstance(state, str) or state not in _STATES):
            raise _error("SERVICE_INVALID_JOB", "Unknown task state")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE (? IS NULL OR state=?) ORDER BY created_at DESC,job_id DESC LIMIT ?",
                (state, state, limit),
            ).fetchall()
            return [self._decode(row) for row in rows]

    def stats(self) -> dict:
        with self._connect() as db:
            counts = {state: 0 for state in sorted(_STATES)}
            counts.update(
                {
                    row["state"]: row["count"]
                    for row in db.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state")
                }
            )
        return {
            "total": sum(counts.values()),
            "active": sum(counts[state] for state in ("queued", "running", "retry_wait")),
            "max_pending": self.max_pending,
            "states": counts,
        }
