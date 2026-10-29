"""Local evidence sessions with transactional budgets and immutable answer refs.

This database is deliberately separate from DocBase publication metadata. The
current DocBase implementation retains historical generations; answer_refs are
retention records, not an implemented garbage-collection lease protocol.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from docqa.agent.evidence import (
    json_bytes,
    pack_candidates,
    revalidate_evidence,
    revalidate_evidence_batch,
)
from docqa.docbase.retrieval import generation_directory
from docqa.docbase.storage import _io_path, load_generation
from docqa.domain.answers import AnswerResult, EvidenceItem, EvidenceSession, HardFilters
from docqa.domain.errors import DocQAError

_ID = re.compile(r"[0-9a-f]{32}\Z")


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _filters(value):
    try:
        value = HardFilters.model_validate({} if value is None else value)
        for values in (value.categories, value.doc_ids):
            if values is not None and (
                len(values) > 10000 or any(not text.strip() for text in values)
            ):
                raise ValueError("Invalid hard-filter identifiers")
        return value
    except (ValidationError, ValueError, TypeError) as exc:
        raise DocQAError("INVALID_REQUEST", "Invalid hard filters") from exc


def bounded_filters(upper: HardFilters, requested: HardFilters | None) -> HardFilters:
    """An absent query filter inherits its upper bound; an empty list stays empty."""
    requested = _filters(requested)
    values = {}
    for key in ("categories", "doc_ids"):
        ceiling, selected = getattr(upper, key), getattr(requested, key)
        if ceiling is not None and selected is not None and not set(selected).issubset(ceiling):
            raise DocQAError("SESSION_FILTER_VIOLATION", "Query expands the session hard scope")
        values[key] = ceiling if selected is None else selected
    return HardFilters(**values)


class SessionStore:
    def __init__(self, root: str | Path, *, clock=None):
        self.root = Path(root).resolve()
        _io_path(self.root).mkdir(parents=True, exist_ok=True)
        self.database = self.root / "sessions.sqlite"
        self.clock = clock or time.time
        with closing(self._connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise DocQAError("SESSION_SCHEMA_UNSUPPORTED", "Unknown evidence-session schema")
            connection.executescript(
                "PRAGMA user_version=1;"
                "CREATE TABLE IF NOT EXISTS sessions("
                "session_id TEXT PRIMARY KEY,payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS queries("
                "reservation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,query_no INTEGER NOT NULL,"
                "round_no INTEGER NOT NULL,filters TEXT NOT NULL,state TEXT NOT NULL,"
                "result TEXT,error_code TEXT,UNIQUE(session_id,query_no),"
                "FOREIGN KEY(session_id) REFERENCES sessions(session_id));"
                "CREATE TABLE IF NOT EXISTS evidence("
                "session_id TEXT NOT NULL,evidence_id TEXT NOT NULL,payload TEXT NOT NULL,"
                "sha256 TEXT NOT NULL,PRIMARY KEY(session_id,evidence_id),"
                "FOREIGN KEY(session_id) REFERENCES sessions(session_id));"
                "CREATE TABLE IF NOT EXISTS answer_refs("
                "answer_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,generation_id TEXT NOT NULL,"
                "index_path TEXT NOT NULL,created_at TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,"
                "FOREIGN KEY(session_id) REFERENCES sessions(session_id));"
            )

    def _connect(self):
        connection = sqlite3.connect(_io_path(self.database), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _identifier(value, kind="session"):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise DocQAError("INVALID_REQUEST", f"Invalid {kind} identifier")

    def _session(self, connection, session_id, caller, require_open):
        self._identifier(session_id)
        row = connection.execute(
            "SELECT payload FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise DocQAError("SESSION_NOT_FOUND", "Unknown evidence session")
        try:
            session = EvidenceSession.model_validate_json(row["payload"])
        except (ValidationError, ValueError, TypeError) as exc:
            raise DocQAError("SESSION_CORRUPT", "Invalid saved evidence session") from exc
        if caller is not None and session.caller != caller:
            raise DocQAError("SESSION_OWNER_MISMATCH", "Caller does not own this session")
        expired = self.clock() >= datetime.fromisoformat(session.expires_at).timestamp()
        if require_open and (session.state != "open" or expired):
            code = "SESSION_EXPIRED" if expired or session.state == "expired" else "SESSION_CLOSED"
            raise DocQAError(code, "Evidence session no longer accepts operations")
        if expired and session.state == "open":
            session = session.model_copy(update={"state": "expired"})
        return session

    @staticmethod
    def _update(connection, session):
        connection.execute(
            "UPDATE sessions SET payload=? WHERE session_id=?",
            (json_bytes(session).decode("utf-8"), session.session_id),
        )

    def create(
        self,
        index: str | Path,
        question: str,
        caller: str,
        *,
        generation_id: str | None = None,
        hard_filters: HardFilters | dict | None = None,
        max_queries: int = 6,
        max_rounds: int = 3,
        max_finalize_attempts: int = 2,
        max_evidence_bytes: int = 24000,
        ttl_seconds: int = 3600,
        required_topics=None,
    ) -> EvidenceSession:
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question.encode("utf-8")) > 16000
            or not isinstance(caller, str)
            or not caller.strip()
            or len(caller) > 200
            or type(ttl_seconds) is not int
            or not 1 <= ttl_seconds <= 604800
        ):
            raise DocQAError("INVALID_REQUEST", "Invalid question, caller or session TTL")
        for value, minimum, maximum in (
            (max_queries, 1, 100),
            (max_rounds, 1, 10),
            (max_finalize_attempts, 1, 2),
            (max_evidence_bytes, 1000, 4_194_304),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise DocQAError("INVALID_REQUEST", "Invalid session budget")
        filters = _filters(hard_filters)
        root = Path(index).resolve()
        with load_generation(generation_directory(root, generation_id)) as generation:
            selected = generation.manifest["generation_id"]
            synthetic = generation.manifest["synthetic"]
        now = self.clock()
        session = EvidenceSession(
            session_id=uuid4().hex,
            index_path=str(root),
            generation_id=selected,
            question=question,
            caller=caller,
            hard_filters=filters,
            required_topics=required_topics or [],
            created_at=_iso(now),
            expires_at=_iso(now + ttl_seconds),
            synthetic=synthetic,
            max_queries=max_queries,
            max_rounds=max_rounds,
            max_finalize_attempts=max_finalize_attempts,
            max_evidence_bytes=max_evidence_bytes,
        )
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES(?,?)",
                (session.session_id, json_bytes(session).decode("utf-8")),
            )
        return session

    def get(self, session_id, *, caller=None, require_open=True) -> EvidenceSession:
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, require_open)
            self._update(connection, session)
            return session

    def set_plan_topics(self, session_id, topics, *, caller):
        """Trusted built-in planning only, before any query or finalization work."""
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, True)
            if session.queries_used or session.finalize_attempts_used:
                raise DocQAError(
                    "SESSION_PLAN_FIXED", "Cannot change required topics after evidence retrieval"
                )
            session = EvidenceSession.model_validate(
                {**session.model_dump(), "required_topics": topics}
            )
            self._update(connection, session)
            return session

    def reserve_query(self, session_id, *, caller, filters=None, round_no=1) -> dict:
        """Consume one request before model/network work, even if that work fails."""
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, True)
            if session.finalize_attempts_used:
                raise DocQAError(
                    "SESSION_FINALIZING", "Finalization has frozen this session's query ledger"
                )
            if type(round_no) is not int or not 1 <= round_no <= session.max_rounds:
                raise DocQAError("SESSION_BUDGET_EXCEEDED", "Round is outside the session budget")
            maximum = connection.execute(
                "SELECT MAX(round_no) FROM queries WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            if (maximum is None and round_no != 1) or (
                maximum is not None and round_no not in {maximum, maximum + 1}
            ):
                raise DocQAError("INVALID_REQUEST", "Query rounds must proceed monotonically")
            if session.queries_used >= session.max_queries:
                raise DocQAError("SESSION_BUDGET_EXCEEDED", "Session query budget exhausted")
            effective = bounded_filters(session.hard_filters, filters)
            reservation = uuid4().hex
            session = session.model_copy(update={"queries_used": session.queries_used + 1})
            self._update(connection, session)
            connection.execute(
                "INSERT INTO queries VALUES(?,?,?,?,?,'reserved',NULL,NULL)",
                (
                    reservation,
                    session_id,
                    session.queries_used,
                    round_no,
                    json_bytes(effective).decode("utf-8"),
                ),
            )
            return {
                "reservation_id": reservation,
                "query_no": session.queries_used,
                "round_no": round_no,
                "filters": effective.model_dump(mode="json"),
            }

    def query_failed(self, session_id, reservation_id, *, caller, error_code):
        with self._transaction() as connection:
            self._session(connection, session_id, caller, True)
            row = connection.execute(
                "SELECT state FROM queries WHERE session_id=? AND reservation_id=?",
                (session_id, reservation_id),
            ).fetchone()
            if row is None or row["state"] != "reserved":
                raise DocQAError("SESSION_RESERVATION_INVALID", "Query reservation is not active")
            connection.execute(
                "UPDATE queries SET state='failed',error_code=? WHERE reservation_id=?",
                (str(error_code)[:200], reservation_id),
            )

    def deliver(
        self,
        session_id,
        reservation_id,
        items,
        *,
        caller,
        query_id,
        round_no=None,
        degraded=False,
        warnings=None,
        delivery_budget_bytes=None,
    ):
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, True)
            row = connection.execute(
                "SELECT * FROM queries WHERE session_id=? AND reservation_id=?",
                (session_id, reservation_id),
            ).fetchone()
            if row is None or row["state"] != "reserved":
                raise DocQAError("SESSION_RESERVATION_INVALID", "Query reservation is not active")
            if round_no is not None and row["round_no"] != round_no:
                raise DocQAError("INVALID_REQUEST", "Delivery round differs from reservation")
            effective = _filters(json.loads(row["filters"]))
            candidates = [EvidenceItem.model_validate(item) for item in items]
            existing_rows = connection.execute(
                "SELECT payload FROM evidence WHERE session_id=? ORDER BY evidence_id",
                (session_id,),
            ).fetchall()
            existing = [
                EvidenceItem.model_validate_json(value["payload"]) for value in existing_rows
            ]

            def identity(item):
                return (
                    item.doc_id,
                    item.revision_id,
                    item.artifact_id,
                    json_bytes(item.source_span),
                )

            seen = {identity(item) for item in existing}
            unique = []
            for item in candidates:
                if identity(item) not in seen:
                    unique.append(item)
                    seen.add(identity(item))
            candidates = unique
            for item in candidates:
                if (
                    effective.categories is not None and item.category not in effective.categories
                ) or (effective.doc_ids is not None and item.doc_id not in effective.doc_ids):
                    raise DocQAError("SESSION_FILTER_VIOLATION", "Evidence escapes the query scope")
            remaining = max(0, session.max_evidence_bytes - len(json_bytes(existing)))
            if delivery_budget_bytes is not None:
                if type(delivery_budget_bytes) is not int or delivery_budget_bytes <= 0:
                    raise DocQAError("INVALID_REQUEST", "Delivery budget must be positive bytes")
                remaining = min(remaining, delivery_budget_bytes)
            common = dict(
                session_id=session_id,
                generation_id=session.generation_id,
                query_id=query_id,
                round_no=row["round_no"],
                next_evidence_no=session.next_evidence_no,
                degraded=degraded,
                warnings=warnings,
            )
            try:
                pack = pack_candidates(candidates, budget_utf8_bytes=remaining, **common)
            except DocQAError as exc:
                if exc.code != "EVIDENCE_BUDGET_TOO_SMALL":
                    raise
                pack = pack_candidates([], budget_utf8_bytes=session.max_evidence_bytes, **common)
                pack = pack.model_copy(
                    update={
                        "complete": not candidates,
                        "omitted_count": len(candidates),
                        "warnings": [*pack.warnings, "SESSION_EVIDENCE_BUDGET_EXHAUSTED"],
                    }
                )
                # Serialize again after changing metadata so the reported byte count stays exact.
                from docqa.agent.evidence import _sized_pack

                values = pack.model_dump()
                values.pop("payload_utf8_bytes")
                pack = _sized_pack(**values)
            for item in pack.items:
                payload = json_bytes(item)
                connection.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?)",
                    (
                        session_id,
                        item.evidence_id,
                        payload.decode("utf-8"),
                        sha256(payload).hexdigest(),
                    ),
                )
            session = session.model_copy(
                update={"next_evidence_no": session.next_evidence_no + len(pack.items)}
            )
            self._update(connection, session)
            connection.execute(
                "UPDATE queries SET state='delivered',result=? WHERE reservation_id=?",
                (json_bytes(pack).decode("utf-8"), reservation_id),
            )
            return pack

    def link_query(self, session_id, reservation_id, *, caller, combined_into):
        """Record a query whose results were delivered by a joint reranking pack."""
        with self._transaction() as connection:
            self._session(connection, session_id, caller, True)
            row = connection.execute(
                "SELECT state FROM queries WHERE session_id=? AND reservation_id=?",
                (session_id, reservation_id),
            ).fetchone()
            if row is None or row["state"] != "reserved":
                raise DocQAError("SESSION_RESERVATION_INVALID", "Query reservation is not active")
            connection.execute(
                "UPDATE queries SET state='delivered',result=? WHERE reservation_id=?",
                (json.dumps({"combined_into": combined_into}), reservation_id),
            )

    def items(self, session_id, *, caller, require_open=True) -> list[EvidenceItem]:
        with self._transaction() as connection:
            self._session(connection, session_id, caller, require_open)
            rows = connection.execute(
                "SELECT * FROM evidence WHERE session_id=? ORDER BY evidence_id", (session_id,)
            ).fetchall()
            result = []
            for row in rows:
                raw = row["payload"].encode("utf-8")
                if sha256(raw).hexdigest() != row["sha256"]:
                    raise DocQAError("SESSION_CORRUPT", "Delivered evidence checksum differs")
                try:
                    item = EvidenceItem.model_validate_json(raw)
                except (ValidationError, ValueError) as exc:
                    raise DocQAError(
                        "SESSION_CORRUPT", "Delivered evidence schema differs"
                    ) from exc
                if item.evidence_id != row["evidence_id"]:
                    raise DocQAError("SESSION_CORRUPT", "Delivered evidence identity differs")
                result.append(item)
            return result

    def reserve_finalize(self, session_id, *, caller) -> EvidenceSession:
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, True)
            if session.finalize_attempts_used >= session.max_finalize_attempts:
                raise DocQAError("SESSION_BUDGET_EXCEEDED", "Finalization repair budget exhausted")
            session = session.model_copy(
                update={"finalize_attempts_used": session.finalize_attempts_used + 1}
            )
            self._update(connection, session)
            return session

    def query_diagnostics(self, session_id, *, caller):
        """Retain failures/degradation across separate external CLI invocations."""
        with self._transaction() as connection:
            self._session(connection, session_id, caller, True)
            rows = connection.execute(
                "SELECT state,result,error_code,round_no FROM queries WHERE session_id=?",
                (session_id,),
            ).fetchall()
        degraded, warnings, failures = False, [], []
        for row in rows:
            if row["state"] == "reserved":
                raise DocQAError("SESSION_QUERY_PENDING", "A reserved query has not completed")
            if row["state"] == "failed":
                failures.append(row["error_code"] or "SESSION_QUERY_FAILED")
            elif row["result"]:
                payload = json.loads(row["result"])
                degraded = degraded or bool(payload.get("degraded", False))
                warnings.extend(payload.get("warnings", []))
        return {
            "degraded": degraded,
            "warnings": list(dict.fromkeys(warnings)),
            "failures": failures,
            "rounds": max((row["round_no"] for row in rows), default=0),
        }

    def close(self, session_id, *, caller):
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, False)
            if session.state == "open":
                session = session.model_copy(update={"state": "closed"})
                self._update(connection, session)
            return session

    def save_answer(self, session_id, result, markdown, *, caller) -> dict:
        """Publish files, answer_ref and session close under one local write lock.

        A crash before SQLite commit can leave an unreferenced immutable directory;
        loaders only expose IDs with committed answer_refs, so it is never visible.
        """
        result = AnswerResult.model_validate(result)
        self._identifier(result.answer_id, "answer")
        if result.session_id != session_id or result.answer_markdown != markdown:
            raise DocQAError("ANSWER_INVALID", "Saved result or Markdown does not match")
        payload = json_bytes(result) + b"\n"
        rendered = markdown.encode("utf-8")
        if len(payload) > 16_777_216 or len(rendered) > 4_194_304:
            raise DocQAError("ANSWER_INVALID", "Answer exceeds persistence byte limits")
        target = self.root / "answers" / result.answer_id
        staging = self.root / "staging" / uuid4().hex
        with self._transaction() as connection:
            session = self._session(connection, session_id, caller, True)
            if (session.question, session.generation_id) != (result.question, result.generation_id):
                raise DocQAError("ANSWER_INVALID", "Answer differs from the fixed session")
            if session.finalize_attempts_used < 1 or (session.synthetic and result.verified):
                raise DocQAError(
                    "ANSWER_INVALID", "Answer was not finalized or mislabels synthetic support"
                )
            if _io_path(target).exists():
                raise DocQAError("ANSWER_ALREADY_EXISTS", "Immutable answer ID already exists")
            _io_path(staging).mkdir(parents=True)
            files = {"answer.json": payload, "answer.md": rendered}
            manifest = {
                "schema_version": "docqa.saved-answer.v1",
                "answer_id": result.answer_id,
                "session_id": session_id,
                "generation_id": session.generation_id,
                "files": {
                    name: {"size": len(data), "sha256": sha256(data).hexdigest()}
                    for name, data in files.items()
                },
            }
            files["manifest.json"] = json_bytes(manifest) + b"\n"
            for name, data in files.items():
                path = _io_path(staging / name)
                path.write_bytes(data)
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
            _io_path(target.parent).mkdir(parents=True, exist_ok=True)
            _io_path(staging).rename(_io_path(target))
            connection.execute(
                "INSERT INTO answer_refs VALUES(?,?,?,?,?,?)",
                (
                    result.answer_id,
                    session_id,
                    session.generation_id,
                    session.index_path,
                    _iso(self.clock()),
                    sha256(files["manifest.json"]).hexdigest(),
                ),
            )
            self._update(connection, session.model_copy(update={"state": "closed"}))
        return {
            "answer_id": result.answer_id,
            "answer_dir": str(target),
            "answer_path": str(target / "answer.json"),
            "markdown_path": str(target / "answer.md"),
        }

    def _answer_reference(self, answer_id):
        self._identifier(answer_id, "answer")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM answer_refs WHERE answer_id=?", (answer_id,)
            ).fetchone()
        if row is None:
            raise DocQAError("ANSWER_NOT_FOUND", "Unknown saved answer")
        return dict(row)

    def load_answer(self, answer_id) -> AnswerResult:
        reference = self._answer_reference(answer_id)
        directory = _io_path(self.root / "answers" / answer_id)
        try:
            raw_manifest = (directory / "manifest.json").read_bytes()
            if sha256(raw_manifest).hexdigest() != reference["manifest_sha256"]:
                raise ValueError("Answer manifest checksum differs")
            manifest = json.loads(raw_manifest)
            if (
                manifest["schema_version"],
                manifest["answer_id"],
                manifest["session_id"],
                manifest["generation_id"],
            ) != (
                "docqa.saved-answer.v1",
                answer_id,
                reference["session_id"],
                reference["generation_id"],
            ) or set(manifest["files"]) != {"answer.json", "answer.md"}:
                raise ValueError("Invalid answer manifest identity")
            for name, checksum in manifest["files"].items():
                raw = (directory / name).read_bytes()
                if len(raw) != checksum["size"] or sha256(raw).hexdigest() != checksum["sha256"]:
                    raise ValueError("Answer file checksum differs")
            result = AnswerResult.model_validate_json((directory / "answer.json").read_bytes())
            if (result.answer_id, result.session_id, result.generation_id) != (
                answer_id,
                reference["session_id"],
                reference["generation_id"],
            ) or result.answer_markdown != (directory / "answer.md").read_text(encoding="utf-8"):
                raise ValueError("Answer projection differs")
            return result
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DocQAError("ANSWER_CORRUPT", "Saved answer integrity validation failed") from exc

    def resolve_evidence(self, session_id, evidence_id, *, caller):
        session = self.get(session_id, caller=caller)
        item = next(
            (
                item
                for item in self.items(session_id, caller=caller)
                if item.evidence_id == evidence_id
            ),
            None,
        )
        if item is None:
            raise DocQAError("EVIDENCE_NOT_FOUND", "Evidence was not delivered to this session")
        return {
            "session_id": session_id,
            "evidence_id": evidence_id,
            **revalidate_evidence(session.index_path, item),
        }

    def resolve_citation(self, answer_id, citation_id):
        return self.resolve_citations(answer_id, [citation_id])[citation_id]

    def resolve_citations(self, answer_id, citation_ids=None):
        """Validate a saved answer and all selected citations with one source scan."""
        result = self.load_answer(answer_id)
        selected = (
            result.citations
            if citation_ids is None
            else [item for item in result.citations if item.citation_id in citation_ids]
        )
        if citation_ids is not None and {item.citation_id for item in selected} != set(
            citation_ids
        ):
            raise DocQAError("CITATION_NOT_FOUND", "Citation does not belong to this answer")
        if not selected:
            return {}
        reference = self._answer_reference(answer_id)
        items = self.items(reference["session_id"], caller=None, require_open=False)
        by_id = {item.evidence_id: item for item in items}
        if any(citation.evidence_id not in by_id for citation in selected):
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Answer evidence is not retained")
        retained = [by_id[citation.evidence_id] for citation in selected]
        sources = revalidate_evidence_batch(reference["index_path"], retained)
        resolved = {}
        for citation, evidence, source in zip(selected, retained, sources, strict=True):
            if (
                not citation.quote.strip()
                or citation.quote not in evidence.text
                or citation.source_span != evidence.source_span
            ):
                raise DocQAError(
                    "EVIDENCE_SOURCE_INVALID", "Citation differs from retained evidence"
                )
            for field in (
                "generation_id",
                "chunk_id",
                "doc_id",
                "revision_id",
                "artifact_id",
                "source_name",
                "origin",
                "figure_ids",
            ):
                if getattr(citation, field) != getattr(evidence, field):
                    raise DocQAError(
                        "EVIDENCE_SOURCE_INVALID",
                        "Citation identity differs from retained evidence",
                    )
            start = evidence.text.index(citation.quote)
            resolved[citation.citation_id] = {
                "answer_id": answer_id,
                "citation_id": citation.citation_id,
                "quote": citation.quote,
                "quote_start_codepoint": evidence.source_span.text_start_codepoint + start,
                "quote_end_codepoint": evidence.source_span.text_start_codepoint
                + start
                + len(citation.quote),
                "quote_coordinate_basis": evidence.source_span.component,
                **source,
            }
        return resolved
