"""Owned deletion with a durable, retryable file-cleanup outbox.

Only per-question and studio outputs are deleted. Shared source/index assets
and uploads are never derived from client-provided paths or included here.
"""

import json
import re
import shutil
import sqlite3
import time
from uuid import uuid4

import portalocker

from docqa.docbase.storage import _io_path, _safe_path
from docqa.domain.errors import DocQAError
from docqa.web.workspace_store import encoded


class DeletionService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store, self.manager = workspace.store, workspace.manager
        self.root = workspace.options.state.resolve()

    @staticmethod
    def public(row):
        return {key: row[key] for key in ("id", "kind", "target", "state", "error_code")}

    def get(self, owner, identity):
        with self.manager.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM ws_deletions WHERE id=? AND owner=?", (identity, owner)
            ).fetchone()
        if row is None:
            raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Deletion was not found")
        return self.public(row)

    def request(self, owner, kind, target):
        if kind not in {"conversation", "artifact"}:
            raise ValueError("Unsupported deletion kind")
        # Serialize short publication/cleanup windows, not model inference.
        with self.store.mutations, self.manager.activity, self.manager.store.transaction() as db:
            previous = db.execute(
                "SELECT * FROM ws_deletions WHERE owner=? AND kind=? AND target=?",
                (owner, kind, target),
            ).fetchone()
            if previous is not None:
                identity = previous["id"]
            else:
                jobs, answers = set(), set()
                if kind == "conversation":
                    self.store.conversation_row(db, target, owner)
                    rows = db.execute(
                        "SELECT DISTINCT j.* FROM jobs j JOIN ws_messages m ON m.job_id=j.job_id WHERE m.conversation=? AND j.owner_id=? AND NOT EXISTS(SELECT 1 FROM ws_messages other JOIN ws_conversations c ON c.id=other.conversation WHERE other.job_id=j.job_id AND other.conversation<>m.conversation AND c.deleted=0)",
                        (target, owner),
                    ).fetchall()
                    for row in rows:
                        jobs.add(row["job_id"])
                        if row["result"] and json.loads(row["result"]).get("answer_id"):
                            answers.add(json.loads(row["result"])["answer_id"])
                    # Blocks new questions immediately; the running runner sees cancellation.
                    db.execute(
                        "UPDATE ws_conversations SET deleted=1 WHERE id=? AND owner=?",
                        (target, owner),
                    )
                    for jid in jobs:
                        db.execute(
                            "UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END WHERE job_id=?",
                            (jid,),
                        )
                elif not db.execute(
                    "SELECT 1 FROM ws_objects WHERE kind='artifact' AND id=? AND owner=?",
                    (target, owner),
                ).fetchone():
                    raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Artifact was not found")

                objects = [
                    (r["kind"], r["id"], json.loads(r["payload"]))
                    for r in db.execute(
                        "SELECT kind,id,payload FROM ws_objects WHERE owner=? AND kind IN ('artifact','artifact_job','export','media','visual_check')",
                        (owner,),
                    )
                ]
                artifacts = {
                    oid
                    for k, oid, value in objects
                    if k == "artifact"
                    and (
                        value.get("conversation_id") == target
                        if kind == "conversation"
                        else oid == target
                    )
                }
                removed, exports = [], []
                for k, oid, value in objects:
                    remove = (
                        (k == "artifact" and oid in artifacts)
                        or (
                            k == "artifact_job"
                            and (
                                (kind == "conversation" and value.get("conversation_id") == target)
                                or value.get("artifact_id") in artifacts
                                or (
                                    value.get("previous_id") in artifacts
                                    and value.get("state") in {"queued", "generating"}
                                )
                            )
                        )
                        or (k == "export" and value.get("artifact_id") in artifacts)
                        or (
                            kind == "conversation"
                            and k in {"media", "visual_check"}
                            and value.get("answer_id") in answers
                        )
                    )
                    if remove:
                        removed.append((k, oid, owner))
                        if k == "artifact_job" and value.get("output_id"):
                            artifacts.add(value["output_id"])
                        if k == "export":
                            exports.append({"id": oid, "format": value["format"]})
                # Remove job rows only after their runner stops; leave messages
                # linked until then so history migration cannot recreate them.
                db.executemany("DELETE FROM ws_objects WHERE kind=? AND id=? AND owner=?", removed)
                identity, now = uuid4().hex, time.time()
                plan = {"jobs": sorted(jobs), "artifacts": sorted(artifacts), "exports": exports}
                db.execute(
                    "INSERT INTO ws_deletions VALUES(?,?,?,?,'pending',?,NULL,?,?)",
                    (identity, owner, kind, target, encoded(plan), now, now),
                )
        self.manager.wakeup.set()
        self.process(identity)
        return self.get(owner, identity)

    def _remove(self, relative):
        # _safe_path rejects junctions/symlinks and proves resolution stays in
        # this Web state before any recursive filesystem operation on Windows.
        path = _io_path(_safe_path(self.root, relative))
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def process(self, identity=None):
        with self.store.mutations:
            with self.manager.store.transaction() as db:
                rows = db.execute(
                    "SELECT * FROM ws_deletions WHERE state='pending'"
                    + (" AND id=?" if identity else ""),
                    (identity,) if identity else (),
                ).fetchall()
            for row in rows:
                plan = json.loads(row["payload"])
                with self.manager.activity:
                    if set(plan["jobs"]) & self.manager.active_jobs:
                        continue
                try:
                    ids = plan["jobs"] + plan["artifacts"] + [v["id"] for v in plan["exports"]]
                    if not all(re.fullmatch(r"[a-f0-9]{32}", value) for value in ids):
                        raise ValueError("Invalid managed output identity")
                    for jid in plan["jobs"]:
                        lock_path = _io_path(_safe_path(self.root, f"agent/{jid}.writer.lock"))
                        lock_path.parent.mkdir(parents=True, exist_ok=True)
                        # A child from an interrupted server may still be alive.
                        with portalocker.Lock(lock_path, timeout=0):
                            self._remove(f"agent/{jid}")
                            self._remove(f"runs/{jid}")
                        self._remove(f"agent/{jid}.writer.lock")
                    for aid in plan["artifacts"]:
                        self._remove(f"studio/{aid}")
                    for export in plan["exports"]:
                        if export["format"] not in {
                            "json",
                            "md",
                            "csv",
                            "svg",
                            "png",
                            "docx",
                            "pptx",
                        }:
                            raise ValueError("Invalid export extension")
                        self._remove(f"studio/exports/{export['id']}.{export['format']}")
                    if plan["jobs"]:
                        self._purge_migration_backups(row["owner"], plan["jobs"])
                    with self.manager.store.transaction() as db:
                        for jid in plan["jobs"]:
                            db.execute(
                                "DELETE FROM jobs WHERE job_id=? AND owner_id=?",
                                (jid, row["owner"]),
                            )
                        if row["kind"] == "conversation":
                            db.execute(
                                "DELETE FROM ws_messages WHERE conversation=?", (row["target"],)
                            )
                            db.execute(
                                "DELETE FROM ws_conversations WHERE id=? AND owner=?",
                                (row["target"], row["owner"]),
                            )
                        db.execute(
                            "UPDATE ws_deletions SET state='complete',payload='{}',error_code=NULL,updated=? WHERE id=?",
                            (time.time(), row["id"]),
                        )
                except (OSError, ValueError, sqlite3.Error, portalocker.exceptions.LockException):
                    with self.manager.store.transaction() as db:
                        db.execute(
                            "UPDATE ws_deletions SET error_code='DELETION_CLEANUP_PENDING',updated=? WHERE id=?",
                            (time.time(), row["id"]),
                        )

    def _purge_migration_backups(self, owner, jobs):
        """Erase matching legacy job rows from app-managed migration snapshots."""
        directory = _io_path(_safe_path(self.root, "backups"))
        if not directory.exists():
            return
        for entry in directory.iterdir():
            if not re.fullmatch(r"before-workspace-[a-f0-9]{32}\.sqlite", entry.name):
                continue
            path = _io_path(_safe_path(self.root, "backups/" + entry.name))
            with sqlite3.connect(path, timeout=1) as db:
                db.execute("PRAGMA secure_delete=ON")
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone():
                    db.executemany(
                        "DELETE FROM jobs WHERE job_id=? AND owner_id=?",
                        [(job, owner) for job in jobs],
                    )
