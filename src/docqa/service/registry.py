"""Service-specific desired state; DocBase remains the sole publication authority."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError


class Registry:
    def __init__(self, root: Path, *, clock=None):
        self.root = Path(root)
        self.clock = clock or time.time
        self.local = threading.local()
        self.path = self.root / "service.sqlite"
        _io_path(self.root).mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DocQAError("SERVICE_SCHEMA_UNSUPPORTED", "Unsupported Service schema")
            for table, key in (("sources", "source_id"), ("documents", "doc_id")):
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table}("
                    f"{key} TEXT PRIMARY KEY,path_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        existing = getattr(self.local, "connection", None)
        if existing is not None:
            yield existing
            return
        connection = sqlite3.connect(_io_path(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            self.local.connection = connection
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise DocQAError(
                "SERVICE_REGISTRY_FAILED",
                "Service metadata transaction failed",
                retryable="locked" in str(exc).lower(),
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            self.local.connection = None
            connection.close()

    @staticmethod
    def _table(table):
        if table not in {"sources", "documents"}:
            raise ValueError("Invalid Service table")
        return "source_id" if table == "sources" else "doc_id"

    def list(self, table):
        key = self._table(table)
        with self.transaction() as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(f"SELECT payload FROM {table} ORDER BY {key}")
            ]

    def get(self, table, identifier):
        key = self._table(table)
        with self.transaction() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE {key}=?", (identifier,)
            ).fetchone()
        if row is None:
            raise DocQAError(
                "SOURCE_NOT_FOUND" if table == "sources" else "DOCUMENT_NOT_FOUND",
                "Unknown Service identifier",
            )
        return json.loads(row[0])

    def put(self, table, value):
        key = self._table(table)
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.transaction() as connection:
            connection.execute(
                f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT({key}) DO UPDATE SET "
                "path_key=excluded.path_key,payload=excluded.payload",
                (value[key], value["path_key"], payload),
            )

    def meta(self, key, default=None):
        with self.transaction() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set_meta(self, key, value):
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False, allow_nan=False)),
            )

    def bump(self):
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='desired_revision'"
            ).fetchone()
            revision = (int(json.loads(row[0])) if row else 0) + 1
            connection.execute(
                "INSERT INTO metadata VALUES('desired_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(revision),),
            )
        return revision
