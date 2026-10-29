"""One local writer, durable jobs, safe reconciliation and immutable publication.

Lock order: worker -> DocBase writer -> short mutation guard -> SQLite.
Source-management calls only take the mutation guard and never the worker lock.
No network work holds the mutation guard. An OS lock, rather than lease expiry,
prevents a live old worker and a restarted worker from publishing concurrently.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.doc2md.common import digest
from docqa.doc2md.pipeline import parse_document, verify_artifact
from docqa.doc2md.security import is_within
from docqa.docbase.admission import has_coverage_gaps
from docqa.docbase.pipeline import build_index
from docqa.docbase.storage import _io_path, load_generation, read_current, verify_generation
from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError
from docqa.service.config import ServiceOptions, normalize_types
from docqa.service.queue import JobQueue
from docqa.service.registry import Registry
from docqa.service.scanner import canonical_path, scan_directory, scan_file, validate_watch_root


def _key(path):
    return os.path.normcase(str(Path(path).absolute()))


class Service:
    def __init__(
        self,
        state="runtime/service",
        *,
        options=None,
        clock=None,
        parse_function=None,
        build_function=None,
    ):
        self.root = canonical_path(Path(state), must_exist=False)
        self.options = options or ServiceOptions()
        self.clock = clock or time.time
        _io_path(self.root).mkdir(parents=True, exist_ok=True)
        self.parsed = self.root / "parsed"
        self.index = self.root / "index"
        self.registry = Registry(self.root, clock=self.clock)
        self.queue = JobQueue(
            self.root,
            clock=self.clock,
            max_pending=self.options.max_pending_jobs,
            max_attempts=self.options.max_attempts,
            retry_base_seconds=self.options.retry_base_seconds,
        )
        self.parse_function = parse_function or parse_document
        self.build_function = build_function or build_index
        self.owner = f"{os.getpid()}-{uuid4().hex}"

    def _try_enqueue(self, kind, payload, key, *, priority):
        if kind == "build" and not payload:
            payload = {"revision": self.registry.meta("desired_revision", 0)}
        try:
            return self.queue.enqueue(kind, payload, key, priority=priority)
        except DocQAError as exc:
            if exc.code != "SERVICE_QUEUE_FULL":
                raise
            return None  # Desired state is the durable outbox, drained after existing jobs.

    @contextmanager
    def _mutation(self):
        try:
            with portalocker.Lock(self.root / ".mutation.lock", timeout=10):
                with self.registry.transaction():
                    yield
        except portalocker.exceptions.LockException as exc:
            raise DocQAError("SERVICE_BUSY", "Service metadata is busy", retryable=True) from exc

    def _authorize(self, source):
        return validate_watch_root(source["path"], self.options.allowed_source_roots, [self.root])

    def add_source(
        self,
        path,
        *,
        recursive=True,
        types=None,
        category="default",
        interval_seconds=None,
    ):
        types = self.options.types if types is None else types
        interval_seconds = (
            self.options.scan_interval_seconds if interval_seconds is None else interval_seconds
        )
        if not isinstance(category, str) or not category.strip() or len(category) > 256:
            raise DocQAError("INVALID_REQUEST", "Category must contain 1..256 characters")
        if type(recursive) is not bool or not 1 <= interval_seconds <= 604800:
            raise DocQAError("INVALID_REQUEST", "Invalid scan interval or recursive flag")
        try:
            selected_types = normalize_types(types)
        except (ValueError, TypeError, AttributeError) as exc:
            raise DocQAError("INVALID_REQUEST", "Invalid document types") from exc
        if {"doc", "ppt"} & set(selected_types) and not self.options.parse.allow_legacy_office:
            raise DocQAError("UNSUPPORTED_FORMAT", "Legacy types need explicit Office conversion")
        selected = validate_watch_root(path, self.options.allowed_source_roots, [self.root])
        with self._mutation():
            for existing in self.registry.list("sources"):
                other = Path(existing["path"])
                if existing["path_key"] == _key(selected):
                    if existing["removed"]:
                        existing.update(
                            enabled=True,
                            removed=False,
                            recursive=recursive,
                            types=selected_types,
                            category=category.strip(),
                            interval_seconds=float(interval_seconds),
                            next_scan_at=self.clock(),
                        )
                        self.registry.put("sources", existing)
                        for doc in self.registry.list("documents"):
                            if (
                                doc["source_id"] == existing["source_id"]
                                and doc["removal_reason"] == "source_removed"
                                and Path(doc["path"]).suffix.lower().lstrip(".") in selected_types
                                and (recursive or Path(doc["path"]).parent == selected)
                            ):
                                doc.update(
                                    removed=False,
                                    removal_reason=None,
                                    state="pending",
                                    category=category.strip(),
                                    target_version=doc["target_version"] + 1,
                                )
                                self.registry.put("documents", doc)
                        self.registry.bump()
                        self._try_enqueue(
                            "scan",
                            {"source_id": existing["source_id"]},
                            f"scan:{existing['source_id']}",
                            priority=10,
                        )
                    return existing
                if is_within(other, selected) or is_within(selected, other):
                    raise DocQAError(
                        "SOURCE_ROOT_OVERLAP", "Overlapping monitored roots are ambiguous"
                    )
            now = self.clock()
            source = dict(
                source_id=uuid4().hex,
                path=str(selected),
                path_key=_key(selected),
                recursive=recursive,
                types=selected_types,
                category=category.strip(),
                interval_seconds=float(interval_seconds),
                enabled=True,
                removed=False,
                next_scan_at=now,
                last_scan_at=None,
                last_success_at=None,
                scan_error=None,
                probe_signature=None,
                created_at=now,
            )
            self.registry.put("sources", source)
            self.registry.bump()
        self.enqueue_scan(source["source_id"])
        return source

    def list_sources(self):
        return self.registry.list("sources")

    def update_source(
        self,
        source_id,
        *,
        enabled=None,
        category=None,
        interval_seconds=None,
        types=None,
        recursive=None,
    ):
        if recursive is not None and type(recursive) is not bool:
            raise DocQAError("INVALID_REQUEST", "Recursive must be Boolean")
        try:
            selected_types = normalize_types(types) if types is not None else None
        except (ValueError, TypeError, AttributeError) as exc:
            raise DocQAError("INVALID_REQUEST", "Invalid document types") from exc
        if (
            selected_types
            and {"doc", "ppt"} & set(selected_types)
            and not self.options.parse.allow_legacy_office
        ):
            raise DocQAError("UNSUPPORTED_FORMAT", "Legacy types need explicit conversion")
        policy_changed = False
        with self._mutation():
            source = self.registry.get("sources", source_id)
            if source["removed"]:
                raise DocQAError("SOURCE_REMOVED", "Source was removed")
            for key, value in (("types", selected_types), ("recursive", recursive)):
                if value is not None and value != source[key]:
                    source[key] = value
                    policy_changed = True
            if policy_changed:
                for doc in self.registry.list("documents"):
                    if doc["source_id"] != source_id:
                        continue
                    eligible = Path(doc["path"]).suffix.lower().lstrip(".") in source["types"] and (
                        source["recursive"] or Path(doc["path"]).parent == Path(source["path"])
                    )
                    if not eligible and not doc["removed"]:
                        doc.update(removed=True, removal_reason="source_policy", state="removed")
                    elif eligible and doc["removal_reason"] == "source_policy":
                        doc.update(
                            removed=False,
                            removal_reason=None,
                            state="pending",
                            target_version=doc["target_version"] + 1,
                        )
                    self.registry.put("documents", doc)
                self.registry.bump()
            if enabled is not None:
                if type(enabled) is not bool:
                    raise DocQAError("INVALID_REQUEST", "Enabled must be Boolean")
                source["enabled"] = enabled
            if interval_seconds is not None:
                if not 1 <= interval_seconds <= 604800:
                    raise DocQAError("INVALID_REQUEST", "Invalid scan interval")
                source["interval_seconds"] = float(interval_seconds)
            if category is not None:
                if not isinstance(category, str) or not category.strip() or len(category) > 256:
                    raise DocQAError("INVALID_REQUEST", "Invalid category")
                if category.strip() != source["category"]:
                    source["category"] = category.strip()
                    for doc in self.registry.list("documents"):
                        if doc["source_id"] == source_id and not doc["removed"]:
                            doc["category"] = source["category"]
                            doc["target_version"] += 1
                            doc["state"] = "pending"
                            self.registry.put("documents", doc)
                    self.registry.bump()
            source["next_scan_at"] = self.clock()
            self.registry.put("sources", source)
        if source["enabled"]:
            self.enqueue_scan(source_id)
        if policy_changed:
            self.enqueue_build()
        return source

    def remove_source(self, source_id):
        with self._mutation():
            source = self.registry.get("sources", source_id)
            source.update(enabled=False, removed=True)
            self.registry.put("sources", source)
            for doc in self.registry.list("documents"):
                if doc["source_id"] == source_id and not doc["removed"]:
                    doc.update(removed=True, removal_reason="source_removed", state="removed")
                    doc["target_version"] += 1
                    self.registry.put("documents", doc)
            self.registry.bump()
        self.enqueue_build()
        return source

    def enqueue_scan(self, source_id=None):
        if source_id is None:
            return [
                self.enqueue_scan(item["source_id"])
                for item in self.list_sources()
                if item["enabled"] and not item["removed"]
            ]
        source = self.registry.get("sources", source_id)
        if not source["enabled"] or source["removed"]:
            raise DocQAError("SOURCE_DISABLED", "Source is not enabled")
        return self.queue.enqueue(
            "scan", {"source_id": source_id}, f"scan:{source_id}", priority=10
        )

    def discover(self, source_id):
        source = self.registry.get("sources", source_id)
        self._authorize(source)
        result = self._snapshot(source)
        return dict(
            source_id=source_id,
            complete=result.complete,
            files=[asdict(value) for value in result.entries.values()],
            errors=result.errors,
            ignored=result.ignored,
        )

    def _snapshot(self, source, *, hash_contents=True):
        return scan_directory(
            Path(source["path"]),
            recursive=source["recursive"],
            types=tuple(source["types"]),
            max_files=self.options.max_files,
            max_file_bytes=self.options.parse.max_file_mb * 1048576,
            hash_contents=hash_contents,
        )

    def _new_document(self, source, entry):
        now = self.clock()
        return dict(
            doc_id=str(uuid4()),
            source_id=source["source_id"],
            path=entry.path,
            path_key=_key(entry.path),
            category=source["category"],
            observed_sha256=entry.content_sha256,
            observed_since=now,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            desired_sha256=None,
            target_version=0,
            good_version=None,
            analysis_epoch=0,
            artifact_dir=None,
            latest_attempt_artifact=None,
            removed=False,
            removal_reason=None,
            missing_count=0,
            missing_since=None,
            state="settling",
            error_code=None,
            created_at=now,
            last_seen_at=now,
        )

    def _queue_parse(self, doc):
        key = f"parse:{doc['doc_id']}:{doc['target_version']}"
        latest = self.queue.latest(key)
        if latest and latest["state"] in {"failed", "cancelled"}:
            return latest
        return self._try_enqueue(
            "parse",
            {"doc_id": doc["doc_id"], "target_version": doc["target_version"]},
            key,
            priority=50,
        )

    def _observe(self, source, entry, doc, *, force=False):
        now = self.clock()
        doc = doc or self._new_document(source, entry)
        doc.update(
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            last_seen_at=now,
            missing_count=0,
            missing_since=None,
        )
        if doc["removal_reason"] == "missing":
            doc.update(removed=False, removal_reason=None, state="settling", desired_sha256=None)
        if doc["removed"]:
            self.registry.put("documents", doc)
            return doc
        if doc["observed_sha256"] != entry.content_sha256:
            doc.update(observed_sha256=entry.content_sha256, observed_since=now, state="settling")
        stable = force or now - doc["observed_since"] >= self.options.settle_seconds
        if stable and (doc["desired_sha256"] != entry.content_sha256 or force):
            doc["desired_sha256"] = entry.content_sha256
            doc["target_version"] += 1
            doc.update(state="pending", error_code=None)
            self.registry.bump()
        self.registry.put("documents", doc)
        if stable and doc["state"] == "pending":
            self._queue_parse(doc)
        return doc

    def _scan(self, source_id):
        source = self.registry.get("sources", source_id)
        if not source["enabled"] or source["removed"]:
            return {"status": "skipped", "reason": "source_disabled"}
        now = self.clock()
        try:
            self._authorize(source)
            snapshot = self._snapshot(source)
        except DocQAError as exc:
            with self._mutation():
                current = self.registry.get("sources", source_id)
                current.update(
                    last_scan_at=now,
                    next_scan_at=now + source["interval_seconds"],
                    scan_error=exc.code,
                )
                self.registry.put("sources", current)
            raise
        with self._mutation():
            current = self.registry.get("sources", source_id)
            if current["removed"] or not current["enabled"]:
                return {"status": "skipped", "reason": "source_disabled"}
            if any(current[key] != source[key] for key in ("category", "types", "recursive")):
                raise DocQAError(
                    "SERVICE_TARGET_STALE", "Source policy changed during scanning", retryable=True
                )
            docs = {
                item["path_key"]: item
                for item in self.registry.list("documents")
                if item["source_id"] == source_id
            }
            observed = [
                self._observe(source, value, docs.get(_key(value.path)))
                for value in snapshot.entries.values()
            ]
            removed = 0
            if snapshot.complete:
                for key, doc in docs.items():
                    if key in snapshot.entries or doc["removed"]:
                        continue
                    doc["missing_count"] += 1
                    if doc["missing_since"] is None:
                        doc["missing_since"] = now
                    if (
                        doc["missing_count"] >= self.options.missing_confirmations
                        and now - doc["missing_since"] >= self.options.settle_seconds
                    ):
                        doc.update(removed=True, removal_reason="missing", state="missing")
                        doc["target_version"] += 1
                        self.registry.bump()
                        removed += 1
                    self.registry.put("documents", doc)
            waiting = any(item["state"] == "settling" for item in observed) or any(
                item["missing_count"] and not item["removed"] for item in docs.values()
            )
            delay = max(0.1, self.options.settle_seconds) if waiting else source["interval_seconds"]
            current.update(
                last_scan_at=now,
                next_scan_at=now + delay,
                scan_error=None if snapshot.complete else "SCAN_INCOMPLETE",
            )
            if snapshot.complete:
                current["last_success_at"] = now
            self.registry.put("sources", current)
        if removed:
            self._try_enqueue("build", {}, "build", priority=100)
        if not snapshot.complete:
            raise DocQAError("SCAN_INCOMPLETE", "Incomplete traversal retained all missing members")
        return dict(
            status="ok",
            observed=len(observed),
            removed=removed,
            settling=sum(item["state"] == "settling" for item in observed),
            ignored=snapshot.ignored,
        )

    def enqueue_parse(self, path, *, source_id=None, force=False):
        selected = canonical_path(Path(path), directory=False)
        candidates = [self.registry.get("sources", source_id)] if source_id else self.list_sources()
        source = next(
            (
                item
                for item in candidates
                if is_within(selected, Path(item["path"]))
                and item["enabled"]
                and not item["removed"]
            ),
            None,
        )
        if source is None:
            raise DocQAError(
                "SOURCE_ROOT_NOT_ALLOWED", "Register and enable the containing source first"
            )
        self._authorize(source)
        if not source["recursive"] and selected.parent != Path(source["path"]):
            raise DocQAError("SOURCE_ROOT_NOT_ALLOWED", "Nonrecursive source excludes nested files")
        if selected.suffix.lower().lstrip(".") not in source["types"]:
            raise DocQAError("UNSUPPORTED_FORMAT", "Document type is outside the source policy")
        entry = scan_file(
            selected, types=source["types"], max_file_bytes=self.options.parse.max_file_mb * 1048576
        )
        with self._mutation():
            doc = next(
                (
                    value
                    for value in self.registry.list("documents")
                    if value["path_key"] == _key(selected)
                ),
                None,
            )
            if doc and doc["removed"] and doc["removal_reason"] != "missing":
                raise DocQAError("DOCUMENT_REMOVED", "Restore the document before parsing")
            if force and doc:
                doc["analysis_epoch"] += 1
            # Explicit file parsing skips the cross-scan debounce; parser snapshots still validate writes.
            doc = self._observe(source, entry, doc, force=force or doc is None)
            if doc["desired_sha256"] != entry.content_sha256:
                doc = self._observe(source, entry, doc, force=True)
            if doc["state"] in {"failed", "review_required"}:
                doc["target_version"] += 1
                doc["state"] = "pending"
                self.registry.put("documents", doc)
                self.registry.bump()
            return self._queue_parse(doc) or dict(
                state="deferred", doc_id=doc["doc_id"], reason="SERVICE_QUEUE_FULL"
            )

    def list_documents(self, *, source_id=None, include_removed=False):
        return [
            item
            for item in self.registry.list("documents")
            if (source_id is None or item["source_id"] == source_id)
            and (include_removed or not item["removed"])
        ]

    def document(self, doc_id):
        doc = self.registry.get("documents", doc_id)
        active = self._active_members()
        member = active.get(doc_id)
        doc["published_artifact_id"] = member["artifact_id"] if member else None
        doc["published_revision_id"] = member["revision_id"] if member else None
        doc["in_current_index"] = member is not None
        doc["target_satisfied"] = (
            not member
            if doc["removed"]
            else bool(
                member
                and doc["good_version"] == doc["target_version"]
                and Path(doc["artifact_dir"]).name == member["artifact_id"]
            )
        )
        return doc

    def remove_document(self, doc_id):
        with self._mutation():
            doc = self.registry.get("documents", doc_id)
            doc.update(removed=True, removal_reason="manual", state="removed")
            doc["target_version"] += 1
            self.registry.put("documents", doc)
            self.registry.bump()
        self.enqueue_build()
        return doc

    def restore_document(self, doc_id):
        with self._mutation():
            doc = self.registry.get("documents", doc_id)
            source = self.registry.get("sources", doc["source_id"])
            if source["removed"] or not source["enabled"]:
                raise DocQAError("SOURCE_DISABLED", "Cannot restore from a disabled source")
            self._authorize(source)
            canonical_path(doc["path"], directory=False)
            doc.update(removed=False, removal_reason=None, state="pending")
            doc["target_version"] += 1
            self.registry.put("documents", doc)
            self.registry.bump()
        return self.enqueue_parse(doc["path"], source_id=doc["source_id"])

    def images(self, doc_id):
        doc = self.registry.get("documents", doc_id)
        artifact = doc["latest_attempt_artifact"] or doc["artifact_dir"]
        if not artifact:
            return dict(doc_id=doc_id, figures=[], status="not_parsed")
        verify_artifact(Path(artifact))
        ir = DocumentIR.model_validate_json(
            _io_path(Path(artifact) / "document.ir.json").read_bytes()
        )
        assets = {item.asset_id: item for item in ir.assets}
        figures = []
        for figure in ir.figures:
            value = figure.model_dump(mode="json")
            asset = assets[figure.asset_id]
            value["image_path"] = str(Path(artifact) / asset.relative_path)
            value["image_sha256"] = asset.sha256
            figures.append(value)
        return dict(
            doc_id=doc_id,
            artifact_dir=artifact,
            figures=figures,
            status=doc["state"],
            old_index_retained=bool(
                doc["artifact_dir"] and doc["good_version"] != doc["target_version"]
            ),
        )

    def reparse_images(self, doc_id):
        doc = self.registry.get("documents", doc_id)
        return self.enqueue_parse(doc["path"], source_id=doc["source_id"], force=True)

    def enqueue_build(self):
        return self.queue.enqueue(
            "build", {"revision": self.registry.meta("desired_revision", 0)}, "build", priority=100
        )

    def _parse(self, payload):
        doc = self.registry.get("documents", payload["doc_id"])
        target = payload["target_version"]
        source = self.registry.get("sources", doc["source_id"])
        if (
            doc["removed"]
            or source["removed"]
            or not source["enabled"]
            or target != doc["target_version"]
        ):
            return {"status": "skipped", "reason": "target_superseded"}
        root = self._authorize(source)
        canonical_path(doc["path"], directory=False)
        options = self.options.parse.model_copy(
            deep=True,
            update={
                "allowed_roots": [root],
                "category": "auto" if source["category"] == "auto" else doc["category"],
                "analysis_revision": f"{self.options.parse.analysis_revision}.service-{doc['analysis_epoch']}",
            },
        )
        try:
            result = self.parse_function(
                doc["path"], self.parsed, options=options, doc_id=doc["doc_id"]
            )
            ir = DocumentIR.model_validate_json(
                _io_path(Path(result.artifact_dir) / "document.ir.json").read_bytes()
            )
            if ir.content_sha256 != doc["desired_sha256"]:
                self._try_enqueue(
                    "scan",
                    {"source_id": source["source_id"]},
                    f"scan:{source['source_id']}",
                    priority=10,
                )
                raise DocQAError(
                    "SERVICE_TARGET_STALE", "File changed after discovery", retryable=True
                )
            with self._mutation():
                current = self.registry.get("documents", doc["doc_id"])
                if current["target_version"] != target or current["removed"]:
                    return {"status": "skipped", "reason": "target_superseded"}
                current["latest_attempt_artifact"] = result.artifact_dir
                current["category"] = ir.category
                if result.eligible_for_indexing:
                    current.update(
                        artifact_dir=result.artifact_dir,
                        good_version=target,
                        state="ready",
                        error_code=None,
                    )
                else:
                    current.update(state="review_required", error_code="SOURCE_NOT_READY")
                self.registry.put("documents", current)
            if result.eligible_for_indexing:
                self._try_enqueue("build", {}, "build", priority=100)
            else:
                raise DocQAError(
                    "SOURCE_NOT_READY",
                    "Image/OCR/parse quality requires review; old member retained",
                )
            return dict(
                status="ok",
                doc_id=doc["doc_id"],
                artifact_dir=result.artifact_dir,
                artifact_id=result.artifact_id,
                reused=result.reused,
                counts=result.counts,
            )
        except DocQAError as exc:
            with self._mutation():
                current = self.registry.get("documents", doc["doc_id"])
                if current["target_version"] == target and not current["removed"]:
                    current["error_code"] = exc.code
                    if current["state"] != "review_required":
                        current["state"] = "pending" if exc.retryable else "failed"
                    self.registry.put("documents", current)
            raise

    @contextmanager
    def _publication_guard(self, desired_revision):
        with self._mutation():
            if self.registry.meta("desired_revision", 0) != desired_revision:
                raise DocQAError(
                    "SERVICE_TARGET_STALE", "Desired state changed during build", retryable=True
                )
            yield

    def _build(self):
        with self._mutation():
            revision = self.registry.meta("desired_revision", 0)
            self.registry.set_meta("build_attempt_revision", revision)
            docs = self.registry.list("documents")
            paths = [
                Path(item["artifact_dir"])
                for item in docs
                if not item["removed"] and item["artifact_dir"]
            ]
        if self.options.retrieval.incomplete_policy in {"native_only", "validated_parts"}:
            from docqa.docbase.pipeline import _source_record

            selected = {
                doc["doc_id"]: Path(doc["artifact_dir"])
                for doc in docs
                if not doc["removed"] and doc["artifact_dir"]
            }
            for doc in docs:
                if (
                    doc["removed"]
                    or doc["state"] != "review_required"
                    or not doc["latest_attempt_artifact"]
                ):
                    continue
                candidate = canonical_path(doc["latest_attempt_artifact"], directory=True)
                if not is_within(candidate, self.parsed):
                    raise DocQAError(
                        "SOURCE_ACCESS_DENIED", "Candidate is outside managed parse assets"
                    )
                try:
                    ir, _, _ = _source_record(candidate, self.options.retrieval.incomplete_policy)
                except DocQAError as error:
                    if error.code in {"SOURCE_NOT_READY", "NO_INDEXABLE_CONTENT"}:
                        continue
                    raise
                if ir.doc_id == doc["doc_id"] and ir.content_sha256 == doc["desired_sha256"]:
                    selected[doc["doc_id"]] = candidate
            paths = list(selected.values())
        result = self.build_function(
            paths,
            self.index,
            options=self.options.retrieval,
            replace=True,
            allow_empty=True,
            publication_guard=lambda: self._publication_guard(revision),
        )
        with self._mutation():
            self.registry.set_meta("assembled_revision", revision)
            if all(
                item["removed"] or item["good_version"] == item["target_version"] for item in docs
            ):
                self.registry.set_meta("synced_revision", revision)
            self.registry.set_meta("last_build", result.model_dump(mode="json"))
        return dict(status="ok", desired_revision=revision, build=result.model_dump(mode="json"))

    def _active_members(self):
        if not (self.index / "registry.sqlite").exists():
            return {}
        generation, _ = read_current(self.index)
        if generation is None:
            return {}
        with load_generation(self.index / "generations" / generation) as selected:
            return {item["doc_id"]: item for item in selected.members}

    def _ingestion_diagnostics(self):
        documents = [item for item in self.list_documents() if not item["removed"]]
        states = Counter(item["state"] for item in documents)
        blocked = [item for item in documents if item["state"] in {"review_required", "failed"}]
        details = []
        for doc in blocked[:20]:
            item = {
                "doc_id": doc["doc_id"],
                "state": doc["state"],
                "error_code": doc.get("error_code"),
                "warning_counts": {},
            }
            artifact = doc.get("latest_attempt_artifact")
            # A tool failure may leave an older completed attempt in metadata.
            # Do not present its warnings as the cause of this newer failure.
            if artifact and doc["state"] == "review_required":
                try:
                    directory = canonical_path(artifact, directory=True)
                    if not is_within(directory, self.parsed):
                        raise ValueError("Diagnostic report is outside managed parse assets")
                    report = canonical_path(directory / "quality_report.json", directory=False)
                    if report.stat().st_size > 1024 * 1024:
                        raise ValueError("Diagnostic report exceeds supported size")
                    value = json.loads(_io_path(report).read_bytes())
                    warnings = value.get("warnings", [])
                    if not isinstance(warnings, list):
                        raise ValueError("Malformed diagnostic warnings")
                    counts = Counter(
                        warning["code"]
                        for warning in warnings
                        if isinstance(warning, dict)
                        and isinstance(warning.get("code"), str)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", warning["code"])
                    )
                    item["warning_counts"] = dict(list(sorted(counts.items()))[:64])
                except (OSError, DocQAError, ValueError, TypeError, AttributeError):
                    item["diagnostic_error"] = "QUALITY_REPORT_UNAVAILABLE"
            details.append(item)
        return {
            "discovered_documents": len(documents),
            "document_states": dict(sorted(states.items())),
            "blocked_documents": len(blocked),
            "review_required_documents": states.get("review_required", 0),
            "partial_documents": 0,
            "blockers": details,
            "blockers_truncated": len(blocked) > len(details),
        }

    def index_status(self):
        diagnostics = self._ingestion_diagnostics()
        desired = self.registry.meta("desired_revision", 0)
        assembled = self.registry.meta("assembled_revision", 0)
        diagnostics.update(
            desired_revision=desired,
            assembled_revision=assembled,
            synced_revision=self.registry.meta("synced_revision", 0),
        )
        diagnostics["ingestion_status"] = (
            "processing"
            if any(diagnostics["document_states"].get(state) for state in ("pending", "settling"))
            else "blocked"
            if diagnostics["blocked_documents"]
            else "build_failed"
            if desired != assembled and self.registry.meta("failed_build_revision") == desired
            else "awaiting_publication"
            if desired != assembled
            else "ready"
            if diagnostics["discovered_documents"]
            else "no_documents"
        )
        if not self.index.exists():
            return dict(
                index_path=str(self.index),
                generation_id=None,
                documents=0,
                indexed_documents=0,
                status="not_built",
                **diagnostics,
            )
        generation, sequence = read_current(self.index)
        if generation is None:
            return dict(
                index_path=str(self.index),
                generation_id=None,
                documents=0,
                indexed_documents=0,
                status="not_built",
                **diagnostics,
            )
        with load_generation(self.index / "generations" / generation) as selected:
            partial_members = [member for member in selected.members if has_coverage_gaps(member)]
            diagnostics["partial_documents"] = len(partial_members)
            docs = {doc["doc_id"]: doc for doc in self.list_documents() if not doc["removed"]}
            admitted = {
                member["doc_id"]
                for member in partial_members
                if member["doc_id"] in docs
                and docs[member["doc_id"]]["state"] == "review_required"
                and Path(docs[member["doc_id"]].get("latest_attempt_artifact") or "").name
                == member["artifact_id"]
            }
            diagnostics["blocked_documents"] -= len(admitted)
            diagnostics["coverage_gaps"] = [
                detail for detail in diagnostics["blockers"] if detail["doc_id"] in admitted
            ]
            diagnostics["blockers"] = [
                detail for detail in diagnostics["blockers"] if detail["doc_id"] not in admitted
            ]
            diagnostics["blockers_truncated"] = diagnostics["blocked_documents"] > len(
                diagnostics["blockers"]
            )
            diagnostics["coverage_gaps_truncated"] = len(admitted) > len(
                diagnostics["coverage_gaps"]
            )
            if (
                partial_members
                and not diagnostics["blocked_documents"]
                and desired == assembled
                and not any(
                    diagnostics["document_states"].get(state) for state in ("pending", "settling")
                )
            ):
                diagnostics["ingestion_status"] = "ready_with_gaps"
            return dict(
                index_path=str(self.index),
                generation_id=generation,
                publish_sequence=sequence,
                documents=len(selected.members),
                indexed_documents=len(selected.members),
                chunks=len(selected.chunks),
                vector_backend=selected.profiles.get("vector_backend", "numpy"),
                recall_routes=selected.profiles.get("recall_routes", ["dense", "bm25", "sparse"]),
                synthetic=selected.manifest["synthetic"],
                status="ok",
                **diagnostics,
            )

    def verify_index(self, generation_id=None):
        if generation_id is None:
            generation_id = self.index_status()["generation_id"]
        from docqa.docbase.retrieval import generation_directory

        return verify_generation(generation_directory(self.index, generation_id))

    def _configuration(self):
        parse_profile = digest(
            self.options.parse.model_dump(mode="json", exclude={"allowed_roots"})
        )
        retrieval_profile = digest(self.options.retrieval.model_dump(mode="json"))
        with self._mutation():
            prior = self.registry.meta("parse_settings")
            if prior is not None and prior != parse_profile:
                for doc in self.registry.list("documents"):
                    if not doc["removed"]:
                        doc.update(state="pending", target_version=doc["target_version"] + 1)
                        self.registry.put("documents", doc)
                self.registry.bump()
            self.registry.set_meta("parse_settings", parse_profile)
            previous_index = self.registry.meta("retrieval_settings")
            if previous_index is not None and previous_index != retrieval_profile:
                self.registry.bump()
                self._try_enqueue("build", {}, "build", priority=100)
            self.registry.set_meta("retrieval_settings", retrieval_profile)

    def _schedule(self):
        self._configuration()
        for source in self.list_sources():
            if not source["enabled"] or source["removed"]:
                continue
            due = self.clock() >= source["next_scan_at"]
            try:
                self._authorize(source)
                probe = self._snapshot(source, hash_contents=False)
                signature = digest(
                    [(key, item.size, item.mtime_ns) for key, item in sorted(probe.entries.items())]
                )
                signature = digest([signature, probe.complete])
                changed = signature != source["probe_signature"]
            except DocQAError:
                signature, changed = None, due
            if due or changed:
                queued = self._try_enqueue(
                    "scan",
                    {"source_id": source["source_id"]},
                    f"scan:{source['source_id']}",
                    priority=10,
                )
                with self._mutation():
                    current = self.registry.get("sources", source["source_id"])
                    if queued:
                        current["probe_signature"] = signature
                    self.registry.put("sources", current)
        self._repair_outbox()

    def _repair_outbox(self):
        # Repair a crash between a desired-state write and enqueueing its job.
        for doc in self.list_documents():
            source = self.registry.get("sources", doc["source_id"])
            if source["enabled"] and not source["removed"] and doc["state"] == "pending":
                self._queue_parse(doc)
        sources = {item["source_id"]: item for item in self.list_sources()}
        active = [doc for doc in self.list_documents() if sources[doc["source_id"]]["enabled"]]

        def executable(doc):
            if doc["state"] == "settling":
                return True
            if doc["state"] != "pending":
                return False
            latest = self.queue.latest(f"parse:{doc['doc_id']}:{doc['target_version']}")
            return latest is None or latest["state"] not in {"failed", "cancelled"}

        if self.registry.meta("desired_revision", 0) != self.registry.meta(
            "assembled_revision", 0
        ) and not any(executable(doc) for doc in active):
            if self.registry.meta("failed_build_revision") != self.registry.meta(
                "desired_revision", 0
            ):
                latest = self.queue.latest("build")
                revision = self.registry.meta("desired_revision", 0)
                if not (
                    latest
                    and latest["state"] == "cancelled"
                    and latest["payload"].get("revision") == revision
                ):
                    self._try_enqueue("build", {"revision": revision}, "build", priority=100)

    def _process_unlocked(self):
        job = self.queue.claim(self.owner)
        if job is None:
            return None
        self.queue.heartbeat(job["job_id"], self.owner)
        started = time.monotonic()
        try:
            if job["kind"] == "scan":
                result = self._scan(job["payload"]["source_id"])
            elif job["kind"] == "parse":
                result = self._parse(job["payload"])
            elif job["kind"] == "build":
                result = self._build()
            else:
                raise DocQAError("INVALID_REQUEST", "Unknown Service job kind")
            record = self.queue.finish(job["job_id"], self.owner, result)
        except (DocQAError, OSError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, DocQAError) else "SERVICE_JOB_FAILED"
            record = self.queue.fail(
                job["job_id"],
                self.owner,
                code,
                retryable=isinstance(exc, DocQAError) and exc.retryable,
            )
            if record["state"] == "failed":
                with self._mutation():
                    if job["kind"] == "parse":
                        doc = self.registry.get("documents", job["payload"]["doc_id"])
                        if (
                            doc["target_version"] == job["payload"]["target_version"]
                            and not doc["removed"]
                        ):
                            if doc["state"] not in {"ready", "review_required"}:
                                doc["state"] = "failed"
                            doc["error_code"] = code
                            self.registry.put("documents", doc)
                    elif job["kind"] == "build":
                        self.registry.set_meta(
                            "failed_build_revision", self.registry.meta("build_attempt_revision")
                        )
        self.registry.set_meta("worker_heartbeat", self.clock())
        self.registry.set_meta(
            "last_job",
            dict(
                job_id=job["job_id"],
                state=record["state"],
                duration_seconds=round(time.monotonic() - started, 6),
            ),
        )
        return record

    @contextmanager
    def _worker(self):
        try:
            with portalocker.Lock(self.root / ".worker.lock", timeout=0):
                self.queue.recover_running()
                self.registry.set_meta("worker_owner", self.owner)
                self.registry.set_meta("worker_heartbeat", self.clock())
                stopped = threading.Event()

                def heartbeat():
                    while not stopped.wait(2):
                        try:
                            self.registry.set_meta("worker_heartbeat", self.clock())
                        except (OSError, DocQAError):
                            return

                pulse = threading.Thread(
                    target=heartbeat, daemon=True, name="docqa-service-heartbeat"
                )
                pulse.start()
                try:
                    yield
                finally:
                    stopped.set()
                    pulse.join(timeout=11)
                    self.registry.set_meta("worker_stopped_at", self.clock())
        except portalocker.exceptions.LockException as exc:
            raise DocQAError(
                "SERVICE_ALREADY_RUNNING", "Another process owns the Service worker"
            ) from exc

    def process_next(self):
        with self._worker():
            self._configuration()
            result = self._process_unlocked()
            self._repair_outbox()
            return result

    def run_once(self):
        with self._worker():
            self._schedule()
            jobs = []
            for _ in range(self.options.max_jobs_per_tick):
                job = self._process_unlocked()
                if job is None:
                    break
                jobs.append(job)
                self._repair_outbox()
            return dict(
                processed=len(jobs),
                failed=sum(job["state"] == "failed" for job in jobs),
                retry_wait=sum(job["state"] == "retry_wait" for job in jobs),
                jobs=jobs,
            )

    def request_stop(self):
        self.registry.set_meta("stop_requested", True)
        return {"stop_requested": True, "mode": "cooperative_between_jobs"}

    def run(self, *, poll_seconds=3, max_ticks=None):
        if not 0.1 <= poll_seconds <= 3600 or (max_ticks is not None and max_ticks < 1):
            raise DocQAError("INVALID_REQUEST", "Invalid polling budget")
        with self._worker():
            self.registry.set_meta("stop_requested", False)
            ticks, failed = 0, 0
            while max_ticks is None or ticks < max_ticks:
                if self.registry.meta("stop_requested", False):
                    break
                self._schedule()
                for _ in range(self.options.max_jobs_per_tick):
                    if self.registry.meta("stop_requested", False):
                        break
                    job = self._process_unlocked()
                    if job is None:
                        break
                    failed += job["state"] == "failed"
                    self._repair_outbox()
                ticks += 1
                if max_ticks is None or ticks < max_ticks:
                    # Wait in short slices so stop remains responsive between model calls.
                    until = time.monotonic() + poll_seconds
                    while time.monotonic() < until and not self.registry.meta(
                        "stop_requested", False
                    ):
                        time.sleep(min(0.25, max(0, until - time.monotonic())))
            return dict(
                ticks=ticks,
                failed=failed,
                stop_requested=self.registry.meta("stop_requested", False),
            )

    def status(self):
        documents = self.list_documents(include_removed=True)
        active = self._active_members()

        def satisfied(doc):
            member = active.get(doc["doc_id"])
            return (
                not member
                if doc["removed"]
                else bool(
                    member
                    and doc["good_version"] == doc["target_version"]
                    and doc["artifact_dir"]
                    and Path(doc["artifact_dir"]).name == member["artifact_id"]
                )
            )

        return dict(
            schema_version="docqa.service-status.v1",
            root=str(self.root),
            sources=self.list_sources(),
            documents=len(documents),
            desired_revision=self.registry.meta("desired_revision", 0),
            assembled_revision=self.registry.meta("assembled_revision", 0),
            synced_revision=self.registry.meta("synced_revision", 0),
            unsatisfied_documents=[doc["doc_id"] for doc in documents if not satisfied(doc)],
            jobs=self.queue.stats(),
            index=self.index_status(),
            worker_owner=self.registry.meta("worker_owner"),
            worker_heartbeat=self.registry.meta("worker_heartbeat"),
            last_job=self.registry.meta("last_job"),
            stop_requested=self.registry.meta("stop_requested", False),
            implementation="single_host_os_lock_polling_no_gc",
        )
