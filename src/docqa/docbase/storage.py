"""Immutable DocBase generation packages and a single SQLite publication pointer.

The caller owns the cross-process writer lock and the staging -> generations rename.
There is deliberately no active.json, no in-place update and no automatic deletion.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from docqa.domain.errors import DocQAError

SCHEMA = "docqa.generation.v1"
REQUIRED_FILES = {
    "catalog.sqlite",
    "profiles.json",
    "members.json",
    "dense/vectors.npy",
    "dense/row_ids.jsonl",
    "bm25/index.json",
    "sparse/index.json",
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _io_path(path: Path) -> Path:
    """Use Win32 extended paths internally; portable manifest paths stay unchanged."""
    path = Path(path).absolute()
    if os.name != "nt" or str(path).startswith("\\\\?\\"):
        return path
    value = str(path)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _sqlite_uri(path: Path) -> str:
    # pathlib encodes a Win32 extended prefix as URI authority "%3F", which
    # SQLite correctly rejects. SQLite's Windows VFS handles long absolute paths.
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).as_uri() + "?mode=ro&immutable=1"


def _digest(value: Any) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value) + b"\n")


def _checksum(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def _read_json(path: Path, *, maximum: int = 256 * 1024 * 1024) -> Any:
    if path.stat().st_size > maximum:
        raise ValueError("JSON metadata exceeds the supported size")
    return json.loads(path.read_bytes())


def _safe_path(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Package path must be a portable relative path")
    components = PurePosixPath(relative)
    if (
        relative != components.as_posix()
        or components.is_absolute()
        or not components.parts
        or any(part in {".", ".."} for part in components.parts)
    ):
        raise ValueError("Package path escapes its generation")
    path = directory.joinpath(*components.parts)
    # Reject links even when the target happens to remain inside the package.
    current = directory
    for part in components.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("Package links are not permitted")
    path.resolve().relative_to(directory.resolve())
    return path


def _rows(value: Any, count: int) -> list[list[Any]]:
    if not isinstance(value, list):
        raise ValueError("Posting must be a list")
    previous = -1
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("Posting entry must have row and weight")
        row = entry[0]
        if type(row) is not int or not previous < row < count:
            raise ValueError("Posting rows must be ordered, unique and in range")
        previous = row
    return value


def _validate_postings(bm25: dict, sparse: dict, count: int) -> None:
    """Validate transparent v1 posting formats without reinterpreting model tokens."""
    if not isinstance(bm25, dict) or not isinstance(sparse, dict):
        raise ValueError("Index files must be objects")
    if (
        bm25.get("schema_version") != "docqa.bm25.v1"
        or sparse.get("schema_version") != "docqa.sparse.v1"
    ):
        raise ValueError("Unsupported posting schema")
    lengths = bm25["doc_lengths"]
    if (
        not isinstance(lengths, list)
        or len(lengths) != count
        or any(type(length) is not int or length < 0 for length in lengths)
    ):
        raise ValueError("Invalid BM25 document lengths")
    if bm25.get("row_count") != count:
        raise ValueError("BM25 corpus size differs")
    postings = bm25["postings"]
    if not isinstance(postings, dict):
        raise ValueError("BM25 postings must be an object")
    totals = [0] * count
    for term, values in postings.items():
        if not isinstance(term, str) or not term:
            raise ValueError("BM25 term must be nonempty")
        for row, frequency in _rows(values, count):
            if type(frequency) is not int or frequency <= 0:
                raise ValueError("BM25 frequencies must be positive integers")
            totals[row] += frequency
    if totals != lengths:
        raise ValueError("BM25 frequencies disagree with document lengths")
    if "avgdl" in bm25:
        expected = sum(lengths) / count if count else 0.0
        if not math.isclose(bm25["avgdl"], expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("BM25 average length differs")
    for name, default in (("k1", 1.2), ("b", 0.75)):
        value = bm25.get(name, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid BM25 scoring parameter")
        if (name == "k1" and value <= 0) or (name == "b" and not 0 <= value <= 1):
            raise ValueError("Invalid BM25 scoring parameter")
    sparse_postings = sparse["postings"]
    if not isinstance(sparse_postings, dict):
        raise ValueError("Sparse postings must be an object")
    sparse_counts = [0] * count
    for token, values in sparse_postings.items():
        if not isinstance(token, str) or not token.isdecimal() or str(int(token)) != token:
            raise ValueError("Sparse tokens must be canonical nonnegative integer strings")
        for row, weight in _rows(values, count):
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(weight)
                or weight <= 0
            ):
                raise ValueError("Sparse weights must be positive finite numbers")
            sparse_counts[row] += 1
    if sparse.get("row_count") != count:
        raise ValueError("Sparse corpus size differs")
    if sparse.get("nonzero_counts") != sparse_counts:
        raise ValueError("Sparse nonzero counts differ from postings")


def _validate_vectors(vectors: np.ndarray, count: int, dimension: int | None = None) -> None:
    if vectors.dtype != np.dtype("float32") or vectors.ndim != 2:
        raise ValueError("Dense vectors must be a float32 matrix")
    if vectors.shape[0] != count or vectors.shape[1] < 1:
        raise ValueError("Dense shape differs from catalog")
    if dimension is not None and vectors.shape[1] != dimension:
        raise ValueError("Dense dimension differs from its profile")
    for start in range(0, count, 4096):
        batch = vectors[start : start + 4096]
        if not np.isfinite(batch).all():
            raise ValueError("Dense vector contains a nonfinite number")
        norms = np.linalg.norm(batch.astype(np.float64), axis=1)
        if not np.allclose(norms, 1.0, rtol=2e-5, atol=2e-5):
            raise ValueError("Dense vectors must already be L2 normalized")


def _catalog(directory: Path, chunks: list, members: list[dict]) -> None:
    connection = sqlite3.connect(directory / "catalog.sqlite")
    try:
        connection.executescript(
            "PRAGMA foreign_keys=ON; PRAGMA user_version=1;"
            "CREATE TABLE chunks(row_id INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE NOT NULL, "
            "payload TEXT NOT NULL);"
            "CREATE TABLE members(row_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);"
        )
        with connection:
            for row, chunk in enumerate(chunks):
                value = chunk.model_dump(mode="json")
                connection.execute(
                    "INSERT INTO chunks VALUES(?,?,?)",
                    (row, value["chunk_id"], _json_bytes(value).decode("utf-8")),
                )
            for row, member in enumerate(members):
                connection.execute(
                    "INSERT INTO members VALUES(?,?)",
                    (row, _json_bytes(member).decode("utf-8")),
                )
    finally:
        connection.close()


def copy_source_package(source: Path, destination: Path) -> str:
    """Copy one verified doc2md artifact and its immutable blob preserving relative links."""
    from docqa.doc2md.pipeline import verify_artifact

    source, destination = _io_path(source), _io_path(destination)
    verify_artifact(source)
    manifest = _read_json(source / "manifest.json")
    artifact_id = manifest["artifact_id"]
    root = destination / "sources"
    target = root / "artifacts" / artifact_id
    if target.exists():
        raise ValueError("Source artifact already exists in staging")
    target.mkdir(parents=True)
    for relative in ["manifest.json", *manifest["files"]]:
        path = _safe_path(source, relative)
        output = _safe_path(target, relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
    blob = _safe_path(source.parent.parent, manifest["snapshot_ref"])
    output_blob = _safe_path(root, manifest["snapshot_ref"])
    output_blob.parent.mkdir(parents=True, exist_ok=True)
    if output_blob.exists():
        if _checksum(output_blob) != manifest["content_sha256"]:
            raise ValueError("Shared source snapshot differs from its content identity")
    else:
        shutil.copyfile(blob, output_blob)
    verify_artifact(target)
    return target.relative_to(destination).as_posix()


def write_generation(
    staging: Path,
    *,
    generation_id: str,
    chunks: list,
    dense_vectors: Any,
    bm25: dict,
    sparse: dict,
    members: list[dict],
    profiles: dict,
    build_key: str,
    source_packages: dict[str, Path] | None = None,
    empty: bool = False,
) -> dict:
    """Write a self-contained candidate. This function never publishes it."""
    if not _IDENTIFIER.fullmatch(generation_id):
        raise DocQAError("INVALID_GENERATION", "Invalid generation ID")
    staging = _io_path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for artifact_id, source in (source_packages or {}).items():
            reference = copy_source_package(Path(source), staging)
            if Path(reference).name != artifact_id:
                raise ValueError("Source package key differs from its artifact identity")
        if not chunks and not empty:
            raise ValueError("Empty generation requires explicit empty=True")
        vectors = np.ascontiguousarray(dense_vectors, dtype=np.float32)
        if not chunks and vectors.size == 0:
            dimension = profiles.get("dense", {}).get("dimension")
            if type(dimension) is not int or dimension < 1:
                raise ValueError("Empty generation still needs a declared dense dimension")
            vectors = np.empty((0, dimension), dtype=np.float32)
        _validate_vectors(vectors, len(chunks))
        _validate_postings(bm25, sparse, len(chunks))
        _catalog(staging, chunks, members)
        _write_json(staging / "profiles.json", profiles)
        _write_json(staging / "members.json", members)
        (staging / "dense").mkdir(exist_ok=True)
        np.save(staging / "dense/vectors.npy", vectors, allow_pickle=False)
        (staging / "dense/row_ids.jsonl").write_bytes(
            b"".join(
                _json_bytes({"row_id": row, "chunk_id": chunk.chunk_id}) + b"\n"
                for row, chunk in enumerate(chunks)
            )
        )
        _write_json(staging / "bm25/index.json", bm25)
        _write_json(staging / "sparse/index.json", sparse)
        files = {}
        for path in sorted(staging.rglob("*")):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Package links are not permitted")
            if path.is_file():
                relative = path.relative_to(staging).as_posix()
                if relative == "manifest.json":
                    raise ValueError("Staging already contains a manifest")
                files[relative] = {"sha256": _checksum(path), "size": path.stat().st_size}
        manifest = {
            "schema_version": SCHEMA,
            "generation_id": generation_id,
            "build_key": build_key,
            "chunk_count": len(chunks),
            "member_count": len(members),
            "empty": not chunks,
            "synthetic": profiles.get("mode") == "demo"
            or any(
                profiles.get(provider, {}).get("synthetic", False)
                for provider in ("dense", "sparse", "reranker")
            ),
            "profiles_sha256": _digest(profiles),
            "dense": {"dtype": "float32", "shape": list(vectors.shape), "order": "C"},
            "files": files,
        }
        manifest["production_ready"] = not manifest["synthetic"]
        _write_json(staging / "manifest.json", manifest)
        verify_generation(staging)
        for path in staging.rglob("*"):
            if path.is_file():
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
        return manifest
    except DocQAError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise DocQAError(
            "GENERATION_INVALID", f"Cannot write generation ({type(exc).__name__})"
        ) from exc


@dataclass
class Generation:
    directory: Path
    chunks: list
    vectors: np.ndarray
    bm25: dict
    sparse: dict
    members: list[dict]
    profiles: dict
    manifest: dict

    def close(self) -> None:
        mapping = getattr(self.vectors, "_mmap", None)
        if mapping is not None:
            mapping.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _verify_sources(directory: Path, chunks: list, members: list[dict], profiles: dict) -> None:
    from docqa.doc2md.pipeline import verify_artifact
    from docqa.docbase.chunker import chunk_artifact, resolve_span_text
    from docqa.domain.documents import DocumentIR
    from docqa.domain.rag import ChunkOptions

    artifacts = {}
    documents = set()
    for member in members:
        identity = member["doc_id"]
        if identity in documents:
            raise ValueError("Duplicate document member")
        documents.add(identity)
        artifact_id = member["artifact_id"]
        expected = f"sources/artifacts/{artifact_id}"
        if member.get("artifact_ref", expected) != expected:
            raise ValueError("Member artifact path is not canonical")
        artifact = _safe_path(directory, expected)
        if verify_artifact(artifact)["status"] != "complete":
            raise ValueError("Incomplete doc2md sources cannot enter an index")
        ir = DocumentIR.model_validate_json((artifact / "document.ir.json").read_bytes())
        if (ir.doc_id, ir.revision_id, ir.artifact_id) != (
            member["doc_id"],
            member["revision_id"],
            artifact_id,
        ):
            raise ValueError("Member version differs from its source")
        if (ir.category, ir.source_name) != (member["category"], member["source_name"]):
            raise ValueError("Member metadata differs from its source snapshot")
        source_map = _read_json(artifact / "source_map.json")
        artifacts[artifact_id] = (ir, source_map, (artifact / "document.md").read_bytes(), member)
    for chunk in chunks:
        if chunk.artifact_id not in artifacts:
            raise ValueError("Chunk references an absent artifact")
        ir, source_map, markdown, member = artifacts[chunk.artifact_id]
        if (chunk.doc_id, chunk.revision_id, chunk.source_name) != (
            ir.doc_id,
            ir.revision_id,
            ir.source_name,
        ):
            raise ValueError("Chunk version differs from its source")
        if getattr(chunk, "category", None) != member["category"]:
            raise ValueError("Chunk category differs from member snapshot")
        blocks = {block.block_id: block for block in ir.blocks}
        figures = {figure.figure_id for figure in ir.figures}
        entries = {entry["block_id"]: entry for entry in source_map["entries"]}
        for span in chunk.source_spans:
            value = span.model_dump(mode="json") if hasattr(span, "model_dump") else span
            block = blocks[value["block_id"]]
            entry = entries[block.block_id]
            # Exact available field names are bound to SourceSpan v1 by its contract.
            if "start_byte" in value:
                start, end = value["start_byte"], value["end_byte"]
                if not entry["start_byte"] <= start < end <= entry["end_byte"]:
                    raise ValueError("Source byte span escapes its original block")
                markdown[start:end].decode("utf-8")
            if "markdown_start_byte" in value:
                start, end = value["markdown_start_byte"], value["markdown_end_byte"]
                if (start, end) != (entry["start_byte"], entry["end_byte"]):
                    raise ValueError(
                        "Source byte interval must exactly identify its original block"
                    )
                markdown[start:end].decode("utf-8")
            if value.get("figure_ids") and not set(value["figure_ids"]).issubset(figures):
                raise ValueError("Source references unknown figures")
            text = resolve_span_text(ir, span)
            if sha256(text.encode("utf-8")).hexdigest() != span.source_text_sha256:
                raise ValueError("Source span text digest differs from its source component")
    if artifacts:
        options = ChunkOptions.model_validate(profiles["chunk"]["options"])
        projected = []
        for ir, source_map, _, _ in artifacts.values():
            projected.extend(chunk_artifact(ir, source_map, options))
        expected = {chunk.chunk_id: chunk.model_dump(mode="json") for chunk in projected}
        actual = {chunk.chunk_id: chunk.model_dump(mode="json") for chunk in chunks}
        if actual != expected:
            raise ValueError("Chunk catalog differs from the deterministic source projection")


def load_generation(directory: Path, *, verify_sources: bool = True) -> Generation:
    """Load only after all manifest, row, model, posting and source gates succeed."""
    from docqa.domain.rag import Chunk, chunk_identity

    directory = _io_path(Path(directory).resolve())
    vectors = None
    try:
        manifest = _read_json(directory / "manifest.json", maximum=16 * 1024 * 1024)
        if not isinstance(manifest, dict) or manifest["schema_version"] != SCHEMA:
            raise ValueError("Unsupported generation manifest")
        if not _IDENTIFIER.fullmatch(manifest["generation_id"]):
            raise ValueError("Invalid generation identity")
        if directory.parent.name == "generations" and directory.name != manifest["generation_id"]:
            raise ValueError("Generation directory identity differs")
        files = manifest["files"]
        if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files):
            raise ValueError("Manifest omits required files")
        actual_files = set()
        for path in directory.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Package links are not permitted")
            if path.is_file() and path != directory / "manifest.json":
                actual_files.add(path.relative_to(directory).as_posix())
        if actual_files != set(files):
            raise ValueError("Unlisted or missing generation files")
        for relative, metadata in files.items():
            path = _safe_path(directory, relative)
            if (
                not path.is_file()
                or path.stat().st_size != metadata["size"]
                or _checksum(path) != metadata["sha256"]
            ):
                raise ValueError("Generation file missing or modified")
        connection = sqlite3.connect(_sqlite_uri(directory / "catalog.sqlite"), uri=True)
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported catalog schema")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Catalog integrity check failed")
            rows = connection.execute(
                "SELECT row_id,chunk_id,payload FROM chunks ORDER BY row_id"
            ).fetchall()
            chunks = []
            for expected, (row, identity, payload) in enumerate(rows):
                chunk = Chunk.model_validate_json(payload)
                if (
                    row != expected
                    or identity != chunk.chunk_id
                    or chunk_identity(chunk) != identity
                ):
                    raise ValueError("Catalog row mapping differs")
                chunks.append(chunk)
            if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
                raise ValueError("Duplicate chunk identity")
            stored_members = [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM members ORDER BY row_id")
            ]
        finally:
            connection.close()
        count = len(chunks)
        if count != manifest["chunk_count"] or manifest["empty"] is not (count == 0):
            raise ValueError("Generation count or empty state differs")
        profiles = _read_json(directory / "profiles.json")
        if _digest(profiles) != manifest["profiles_sha256"]:
            raise ValueError("Profile identity differs")
        synthetic = profiles.get("mode") == "demo" or any(
            profiles.get(provider, {}).get("synthetic", False)
            for provider in ("dense", "sparse", "reranker")
        )
        if manifest.get("synthetic") is not synthetic or manifest.get("production_ready") is not (
            not synthetic
        ):
            raise ValueError("Generation capability labels differ from provider profiles")
        members = _read_json(directory / "members.json")
        if members != stored_members or len(members) != manifest["member_count"]:
            raise ValueError("Catalog member snapshot differs")
        row_ids = [
            json.loads(line)
            for line in (directory / "dense/row_ids.jsonl").read_bytes().splitlines()
        ]
        if row_ids != [
            {"row_id": row, "chunk_id": chunk.chunk_id} for row, chunk in enumerate(chunks)
        ]:
            raise ValueError("Dense row mapping differs from catalog")
        vectors = np.load(directory / "dense/vectors.npy", mmap_mode="r", allow_pickle=False)
        _validate_vectors(vectors, count, profiles.get("dense", {}).get("dimension"))
        if manifest["dense"] != {"dtype": "float32", "shape": list(vectors.shape), "order": "C"}:
            raise ValueError("Dense manifest metadata differs")
        bm25 = _read_json(directory / "bm25/index.json")
        sparse = _read_json(directory / "sparse/index.json")
        _validate_postings(bm25, sparse, count)
        if not vectors.flags.c_contiguous:
            raise ValueError("Dense array must use declared C row order")
        vocabulary_size = profiles.get("sparse", {}).get("vocab_size")
        if vocabulary_size is not None and any(
            int(token) >= vocabulary_size for token in sparse["postings"]
        ):
            raise ValueError("Sparse token falls outside the bound vocabulary")
        if "analyzer" in profiles:
            from docqa.docbase.analyzer import Analyzer
            from docqa.docbase.lexical import build_bm25

            analyzer_profile = profiles["analyzer"]
            analyzer = Analyzer(
                analyzer_profile["terms"],
                max_token_utf8_bytes=analyzer_profile["max_token_utf8_bytes"],
            )
            if (
                analyzer.profile() != analyzer_profile
                or bm25["analyzer_profile"] != analyzer_profile
            ):
                raise ValueError("BM25 analyzer is incompatible with generation profile")
            if build_bm25(chunks, analyzer, bm25["k1"], bm25["b"]) != bm25:
                raise ValueError("BM25 postings differ from bound chunk texts")
            if profiles.get("bm25", {"k1": bm25["k1"], "b": bm25["b"]}) != {
                "k1": bm25["k1"],
                "b": bm25["b"],
            }:
                raise ValueError("BM25 scoring profile differs from its index")
        if verify_sources:
            _verify_sources(directory, chunks, members, profiles)
        return Generation(directory, chunks, vectors, bm25, sparse, members, profiles, manifest)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, DocQAError) as exc:
        if vectors is not None:
            mapping = getattr(vectors, "_mmap", None)
            if mapping is not None:
                mapping.close()
        raise DocQAError(
            "GENERATION_CORRUPT", f"Generation validation failed ({type(exc).__name__})"
        ) from exc


def verify_generation(directory: Path, *, verify_sources: bool = True) -> dict:
    with load_generation(directory, verify_sources=verify_sources) as generation:
        return {
            "valid": True,
            "generation_id": generation.manifest["generation_id"],
            "chunk_count": len(generation.chunks),
            "member_count": len(generation.members),
            "files_checked": len(generation.manifest["files"]),
        }


def search_dense(
    vectors: np.ndarray, query: list[float] | np.ndarray, eligible, limit: int
) -> list[tuple[int, float]]:
    """Exact filtered cosine search in bounded row blocks, ties ordered by row ID."""
    if type(limit) is not int or limit < 1:
        raise DocQAError("INVALID_REQUEST", "Dense limit must be a positive integer")
    try:
        q = np.asarray(query, dtype=np.float32)
    except (ValueError, TypeError) as exc:
        raise DocQAError("MODEL_OUTPUT_INVALID", "Dense query vector must be numeric") from exc
    if q.ndim != 1 or q.shape[0] != vectors.shape[1] or not np.isfinite(q).all():
        raise DocQAError(
            "MODEL_OUTPUT_INVALID", "Dense query vector has invalid dimension or values"
        )
    norm = np.linalg.norm(q.astype(np.float64))
    if not math.isclose(float(norm), 1.0, rel_tol=2e-5, abs_tol=2e-5):
        raise DocQAError("MODEL_OUTPUT_INVALID", "Dense query vector must be L2 normalized")
    rows = list(eligible)
    if any(type(row) is not int or not 0 <= row < len(vectors) for row in rows):
        raise DocQAError("INVALID_REQUEST", "Dense filter contains an invalid row")
    rows = sorted(set(rows))
    best = []
    for start in range(0, len(rows), 4096):
        batch_rows = rows[start : start + 4096]
        scores = vectors[batch_rows] @ q
        for row, score in zip(batch_rows, scores, strict=True):
            item = (float(score), -row, row)
            if len(best) < limit:
                heapq.heappush(best, item)
            elif item > best[0]:
                heapq.heapreplace(best, item)
    return [(row, score) for score, _, row in sorted(best, reverse=True)]


def _registry(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "registry.sqlite", timeout=20)
    connection.execute("PRAGMA busy_timeout=20000")
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS publication("
        "id INTEGER PRIMARY KEY CHECK(id=1),generation_id TEXT,publish_sequence INTEGER NOT NULL);"
        "INSERT OR IGNORE INTO publication VALUES(1,NULL,0);"
        "CREATE TABLE IF NOT EXISTS publication_events("
        "sequence INTEGER PRIMARY KEY,generation_id TEXT NOT NULL,build_key TEXT NOT NULL);"
    )
    return connection


def read_current(root: Path) -> tuple[str | None, int]:
    try:
        connection = _registry(Path(root))
        try:
            row = connection.execute(
                "SELECT generation_id,publish_sequence FROM publication WHERE id=1"
            ).fetchone()
            return row[0], row[1]
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DocQAError("REGISTRY_FAILED", "Cannot read DocBase registry") from exc


def publish(
    root: Path,
    generation_id: str,
    expected_id: str | None,
    expected_sequence: int,
    build_key: str,
) -> int:
    """CAS a fully prepared final package. Sequence prevents ABA publication."""
    root = Path(root)
    if not _IDENTIFIER.fullmatch(generation_id):
        raise DocQAError("INVALID_GENERATION", "Invalid generation ID")
    verify_generation(root / "generations" / generation_id)
    manifest = _read_json(root / "generations" / generation_id / "manifest.json")
    if manifest["build_key"] != build_key:
        raise DocQAError("GENERATION_INVALID", "Publication build key differs from package")
    connection = _registry(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT generation_id,publish_sequence FROM publication WHERE id=1"
        ).fetchone()
        if current != (expected_id, expected_sequence):
            raise DocQAError(
                "PUBLISH_STALE", "DocBase changed after this build began", retryable=True
            )
        sequence = expected_sequence + 1
        connection.execute(
            "UPDATE publication SET generation_id=?,publish_sequence=? WHERE id=1",
            (generation_id, sequence),
        )
        connection.execute(
            "INSERT INTO publication_events VALUES(?,?,?)", (sequence, generation_id, build_key)
        )
        connection.commit()
        return sequence
    except DocQAError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise DocQAError("REGISTRY_FAILED", "Cannot publish DocBase generation") from exc
    finally:
        connection.close()
