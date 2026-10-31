"""Workspace metadata on the existing job database; jobs/answers remain authoritative."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def submission_data(value):
    data = value.model_dump(mode="json")
    if value.mode == "retrieval":
        data.pop("mode", None)  # Preserve fingerprints of existing retrieval requests.
    return data


class WorkspaceStore:
    def __init__(self, tasks):
        self.tasks = tasks
        self.mutations = threading.RLock()
        with closing(sqlite3.connect(_io_path(tasks.path))) as db:
            present = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='ws_conversations'"
            ).fetchone()
            if not present and db.execute("SELECT count(*) FROM jobs").fetchone()[0]:
                backup = _io_path(tasks.root / "backups")
                backup.mkdir(exist_ok=True)
                with closing(
                    sqlite3.connect(backup / ("before-workspace-" + uuid4().hex + ".sqlite"))
                ) as target:
                    db.backup(target)
        with tasks.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ws_conversations(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0, generation_id TEXT,
                    version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS ws_conversation_owner ON ws_conversations(owner,updated);
                CREATE TABLE IF NOT EXISTS ws_messages(
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, job_id TEXT NOT NULL, request_key TEXT,
                    request_hash TEXT, scope TEXT NOT NULL, created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS ws_message_request
                    ON ws_messages(conversation,request_key) WHERE request_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS ws_message_conversation ON ws_messages(conversation,created);
                CREATE TABLE IF NOT EXISTS ws_objects(
                    kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS ws_object_owner ON ws_objects(kind,owner,updated);
                CREATE TABLE IF NOT EXISTS ws_deletions(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                    error_code TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(owner,kind,target));
            """)

    @staticmethod
    def conversation_row(db, conversation, owner):
        row = db.execute(
            "SELECT * FROM ws_conversations WHERE id=? AND owner=? AND deleted=0",
            (conversation, owner),
        ).fetchone()
        if row is None:
            raise DocQAError("CONVERSATION_NOT_FOUND", "Conversation was not found")
        return dict(row)

    def create(self, owner, title="新对话", generation=None):
        identity, now = uuid4().hex, time.time()
        with self.tasks.transaction() as db:
            db.execute(
                "INSERT INTO ws_conversations(id,owner,title,generation_id,created,updated) VALUES(?,?,?,?,?,?)",
                (identity, owner, title, generation, now, now),
            )
        return self.get(identity, owner)

    def get(self, identity, owner):
        with self.tasks.transaction() as db:
            return self.conversation_row(db, identity, owner)

    def migrate_history(self, owner):
        """Idempotent read-through migration; never rewrite historical jobs or answers."""
        with self.tasks.transaction() as db:
            rows = db.execute(
                "SELECT * FROM jobs j WHERE owner_id=? AND NOT EXISTS(SELECT 1 FROM ws_messages m WHERE m.job_id=j.job_id)",
                (owner,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                identity = uuid5(NAMESPACE_URL, "docqa-history:" + row["job_id"]).hex
                db.execute(
                    "INSERT OR IGNORE INTO ws_conversations(id,owner,title,generation_id,created,updated) VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        owner,
                        payload["question"][:48],
                        payload["generation_id"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )
                scope = encoded(
                    {
                        "mode": "selected" if payload.get("doc_ids") else "all",
                        "doc_ids": payload.get("doc_ids"),
                        "categories": payload.get("categories"),
                        "generation_id": payload["generation_id"],
                    }
                )
                for role in ("user", "assistant"):
                    mid = uuid5(NAMESPACE_URL, row["job_id"] + role).hex
                    db.execute(
                        "INSERT OR IGNORE INTO ws_messages VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            mid,
                            identity,
                            role,
                            payload["question"] if role == "user" else "",
                            row["job_id"],
                            None,
                            None,
                            scope,
                            row["created_at"] + (0.001 if role == "assistant" else 0),
                        ),
                    )

    def list(self, owner, *, query="", archived=False, offset=0, limit=50):
        self.migrate_history(owner)
        with self.tasks.transaction() as db:
            rows = db.execute(
                "SELECT c.* FROM ws_conversations c WHERE owner=? AND deleted=0 AND archived=? AND (title LIKE ? OR EXISTS(SELECT 1 FROM ws_messages m WHERE m.conversation=c.id AND m.role='user' AND m.content LIKE ?)) ORDER BY pinned DESC,updated DESC LIMIT ? OFFSET ?",
                (owner, int(archived), "%" + query + "%", "%" + query + "%", limit + 1, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": offset + limit if len(rows) > limit else None,
        }

    def patch(self, identity, owner, changes):
        with self.tasks.transaction() as db:
            row = self.conversation_row(db, identity, owner)
            expected = changes.pop("version", None)
            if expected is not None and expected != row["version"]:
                raise DocQAError("CONVERSATION_CHANGED", "Conversation changed in another tab")
            permitted = {"title", "pinned", "archived", "deleted"}
            if not set(changes).issubset(permitted):
                raise DocQAError("INVALID_REQUEST", "Unsupported conversation update")
            for key, value in changes.items():
                row[key] = value
            db.execute(
                "UPDATE ws_conversations SET title=?,pinned=?,archived=?,deleted=?,version=version+1,updated=? WHERE id=?",
                (
                    row["title"],
                    int(row["pinned"]),
                    int(row["archived"]),
                    int(row["deleted"]),
                    time.time(),
                    identity,
                ),
            )
        return {**row, "version": row["version"] + 1}

    def upgrade(self, identity, owner, generation):
        with self.tasks.transaction() as db:
            self.conversation_row(db, identity, owner)
            if db.execute(
                "SELECT 1 FROM ws_messages m JOIN jobs j ON j.job_id=m.job_id WHERE m.conversation=? AND j.state IN ('queued','running')",
                (identity,),
            ).fetchone():
                raise DocQAError(
                    "CONVERSATION_BUSY", "Wait for the current question before upgrading"
                )
            db.execute(
                "UPDATE ws_conversations SET generation_id=?,version=version+1,updated=? WHERE id=?",
                (generation, time.time(), identity),
            )
        return self.get(identity, owner)

    def messages(self, identity, owner, *, offset=0, limit=100):
        with self.tasks.transaction() as db:
            self.conversation_row(db, identity, owner)
            rows = db.execute(
                "SELECT * FROM ws_messages WHERE conversation=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                (identity, limit + 1, offset),
            ).fetchall()
        values = []
        for row in reversed(rows[:limit]):
            value = dict(row)
            value["scope"] = json.loads(value["scope"])
            value.pop("request_hash", None)
            values.append(value)
        return {"items": values, "next_cursor": offset + limit if len(rows) > limit else None}

    def previous_submission(self, identity, owner, value):
        """Resolve retries before catalog validation, including after an upgrade."""
        fingerprint = sha256(encoded(submission_data(value)).encode()).hexdigest()
        with self.tasks.transaction() as db:
            self.conversation_row(db, identity, owner)
            row = db.execute(
                "SELECT job_id,request_hash FROM ws_messages WHERE conversation=? AND request_key=?",
                (identity, value.client_request_id),
            ).fetchone()
        if row and row["request_hash"] != fingerprint:
            raise DocQAError("IDEMPOTENCY_CONFLICT", "Request key was reused for different input")
        return row["job_id"] if row else None

    def submit(
        self, identity, owner, value, generation, document_ids, *, chat_history_override=None
    ):
        request_key = value.client_request_id
        data = submission_data(value)
        fingerprint = sha256(encoded(data).encode()).hexdigest()
        now, job_id = time.time(), uuid4().hex
        with self.tasks.transaction() as db:
            conversation = self.conversation_row(db, identity, owner)
            previous = db.execute(
                "SELECT job_id,request_hash FROM ws_messages WHERE conversation=? AND request_key=?",
                (identity, request_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise DocQAError(
                        "IDEMPOTENCY_CONFLICT", "Request key was reused for different input"
                    )
                return previous["job_id"]
            if (
                value.mode != "chat"
                and conversation["generation_id"]
                and conversation["generation_id"] != generation
            ):
                raise DocQAError("CONVERSATION_CHANGED", "Conversation version changed")
            if db.execute(
                "SELECT 1 FROM ws_messages m JOIN jobs j ON j.job_id=m.job_id WHERE m.conversation=? AND j.state IN ('queued','running')",
                (identity,),
            ).fetchone():
                raise DocQAError("CONVERSATION_BUSY", "A reply is still running")
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
                ).fetchone()[0]
                >= self.tasks.capacity
            ):
                raise DocQAError("WEB_QUEUE_FULL", "Question queue is full")
            history = [
                row[0]
                for row in db.execute(
                    "SELECT m.content FROM ws_messages m JOIN jobs j ON j.job_id=m.job_id WHERE m.conversation=? AND m.role='user' AND COALESCE(json_extract(j.payload,'$.mode'),'retrieval')='retrieval' ORDER BY m.created DESC LIMIT 4",
                    (identity,),
                )
            ][::-1]
            payload = {
                key: data[key] for key in ("question", "categories", "topics", "output_format")
            }
            payload.update(
                doc_ids=document_ids,
                generation_id=generation,
                context_questions=[text[:1800] for text in history],
            )
            scope = {
                "mode": value.scope_mode,
                "doc_ids": document_ids,
                "categories": value.categories,
                "doc_types": value.doc_types,
                "generation_id": generation,
                "interaction_mode": value.mode,
            }
            if value.mode == "chat":
                turns, used = [], 0
                rows = db.execute(
                    "SELECT j.payload,j.result FROM ws_messages m JOIN jobs j ON j.job_id=m.job_id WHERE m.conversation=? AND m.role='user' AND j.owner_id=? AND j.state='succeeded' AND json_extract(j.payload,'$.mode')='chat' ORDER BY m.created DESC LIMIT 8",
                    (identity, owner),
                ).fetchall()
                for row in rows:
                    prior, result = json.loads(row["payload"]), json.loads(row["result"])
                    pair = [
                        {"role": "user", "content": prior["question"]},
                        {"role": "assistant", "content": result["chat"]["content"]},
                    ]
                    size = sum(len(m["content"].encode("utf-8")) for m in pair)
                    if used + size > 24000:
                        break
                    turns.insert(0, pair)
                    used += size
                payload = {
                    "mode": "chat",
                    "question": value.question,
                    "generation_id": None,
                    "chat_history": [message for pair in turns for message in pair],
                }
                if chat_history_override is not None:
                    payload["chat_history"] = chat_history_override
                scope = {
                    "mode": "all",
                    "interaction_mode": "chat",
                    "generation_id": None,
                    "doc_ids": None,
                    "categories": None,
                    "doc_types": None,
                }
            db.execute(
                "INSERT INTO jobs(job_id,owner_id,state,payload,stage,created_at,updated_at) VALUES(?,?,'queued',?,'queued',?,?)",
                (job_id, owner, encoded(payload), now, now),
            )
            for role in ("user", "assistant"):
                db.execute(
                    "INSERT INTO ws_messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        identity,
                        role,
                        value.question if role == "user" else "",
                        job_id,
                        request_key if role == "user" else None,
                        fingerprint if role == "user" else None,
                        encoded(scope),
                        now + (0.001 if role == "assistant" else 0),
                    ),
                )
            title = (
                value.question[:48] if conversation["title"] == "新对话" else conversation["title"]
            )
            db.execute(
                "UPDATE ws_conversations SET title=?,generation_id=?,updated=?,version=version+1 WHERE id=?",
                (
                    title,
                    conversation["generation_id"] if value.mode == "chat" else generation,
                    now,
                    identity,
                ),
            )
        return job_id

    def object_put(self, kind, owner, value):
        now = time.time()
        with self.tasks.transaction() as db:
            if (
                kind == "media"
                and not db.execute(
                    "SELECT 1 FROM jobs j WHERE owner_id=? AND json_extract(result,'$.answer_id')=? "
                    "AND NOT EXISTS(SELECT 1 FROM ws_messages m JOIN ws_conversations c ON c.id=m.conversation WHERE m.job_id=j.job_id AND c.deleted=1)",
                    (owner, value["answer_id"]),
                ).fetchone()
            ):
                raise DocQAError("ANSWER_NOT_FOUND", "Answer was deleted")
            if (
                kind == "export"
                and not db.execute(
                    "SELECT 1 FROM ws_objects WHERE kind='artifact' AND id=? AND owner=?",
                    (value["artifact_id"], owner),
                ).fetchone()
            ):
                raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Artifact was deleted")
            db.execute(
                "INSERT INTO ws_objects VALUES(?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                (kind, value["id"], owner, encoded(value), now, now),
            )
        return value

    def object_delete(self, kind, identity, owner):
        with self.tasks.transaction() as db:
            db.execute(
                "DELETE FROM ws_objects WHERE kind=? AND id=? AND owner=?", (kind, identity, owner)
            )

    def object_update(self, kind, owner, value):
        """Worker updates must never recreate an object deleted while work ran."""
        with self.tasks.transaction() as db:
            return bool(
                db.execute(
                    "UPDATE ws_objects SET payload=?,updated=? WHERE kind=? AND id=? AND owner=?",
                    (encoded(value), time.time(), kind, value["id"], owner),
                ).rowcount
            )

    def object_get(self, kind, identity, owner=None):
        with self.tasks.transaction() as db:
            row = db.execute(
                "SELECT payload FROM ws_objects WHERE kind=? AND id=?"
                + (" AND owner=?" if owner is not None else ""),
                (kind, identity, owner) if owner is not None else (kind, identity),
            ).fetchone()
        if row is None:
            raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Workspace item was not found")
        return json.loads(row[0])

    def objects(self, kind, owner=None, limit=100):
        with self.tasks.transaction() as db:
            rows = db.execute(
                "SELECT payload FROM ws_objects WHERE kind=?"
                + (" AND owner=?" if owner is not None else "")
                + " ORDER BY created DESC LIMIT ?",
                (kind, owner, limit) if owner is not None else (kind, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
