"""Standalone DocBase writer: verified sources -> complete immutable generation."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.doc2md.common import digest
from docqa.doc2md.pipeline import verify_artifact
from docqa.doc2md.security import is_within
from docqa.docbase.analyzer import Analyzer
from docqa.docbase.chunker import chunk_artifact, chunk_profile
from docqa.docbase.config import DocBaseOptions
from docqa.docbase.lexical import build_bm25, build_sparse
from docqa.docbase.providers import make_providers
from docqa.docbase.storage import _io_path, load_generation, publish, read_current, write_generation
from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError
from docqa.domain.rag import BuildResult


def _json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def implementation_profile() -> str:
    directory = Path(__file__).parent
    sources = [*directory.rglob("*.py"), directory.parent / "domain" / "rag.py"]
    return digest(
        {
            p.relative_to(directory.parent).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted(sources)
        }
    )


def select_artifacts(source: str | Path | list[Path] | None) -> list[Path]:
    """Select explicit artifacts, a doc2md root's latest registrations, or a source-list JSON."""
    if source is None:
        return []
    if isinstance(source, list):
        return [Path(path).resolve() for path in source]
    selected = Path(source).resolve()
    if selected.is_file() and selected.suffix.lower() == ".json":
        try:
            if selected.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Source list is too large")
            value = json.loads(selected.read_bytes())
            if value["schema_version"] != "docqa.docbase-sources.v1" or not isinstance(
                value["artifacts"], list
            ):
                raise ValueError("Invalid source list")
            if not all(isinstance(item, str) and item.strip() for item in value["artifacts"]):
                raise ValueError("Invalid artifact path")
            return [(selected.parent / path).resolve() for path in value["artifacts"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DocQAError("INVALID_REQUEST", "Invalid DocBase artifact source list") from exc
    if (selected / "manifest.json").is_file():
        return [selected]
    catalog = selected / "catalog.sqlite"
    if catalog.is_file() and (selected / "artifacts").is_dir():
        try:
            connection = sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
            try:
                documents = connection.execute(
                    "SELECT doc_id,revision_id FROM documents ORDER BY doc_id"
                ).fetchall()
                result = []
                for doc_id, revision_id in documents:
                    row = connection.execute(
                        "SELECT artifact_id FROM artifacts WHERE doc_id=? AND revision_id=? ORDER BY rowid DESC LIMIT 1",
                        (doc_id, revision_id),
                    ).fetchone()
                    if row is None:
                        raise DocQAError(
                            "SOURCE_NOT_READY",
                            "A registered source revision has no parsed artifact",
                        )
                    result.append(selected / "artifacts" / row[0])
                return result
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise DocQAError(
                "INVALID_REQUEST", "Input is not a readable doc2md source catalog"
            ) from exc
    raise DocQAError(
        "INVALID_REQUEST",
        "Use a verified doc2md artifact, doc2md root, or explicit source-list JSON; plain Markdown lacks source contracts",
    )


def _dense_value(value, dimension: int) -> list[float]:
    if not isinstance(value, list) or len(value) != dimension:
        raise ValueError("Dense dimension mismatch")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in value
    ):
        raise ValueError("Invalid dense value")
    norm = math.hypot(*value)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid dense norm")
    return [float(item) / norm for item in value]


def _sparse_value(value, vocab_size: int) -> dict[int, float]:
    if not isinstance(value, dict):
        raise ValueError("Invalid sparse vector")
    result = {}
    for token, weight in value.items():
        if isinstance(token, bool) or not isinstance(token, (str, int)):
            raise ValueError("Invalid sparse token")
        index = int(token)
        if str(index) != str(token) or not 0 <= index < vocab_size:
            raise ValueError("Sparse token outside bound vocabulary")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (float, int))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError("Invalid sparse weight")
        if weight:
            result[index] = float(weight)
    if not result:
        raise ValueError("Sparse vector has no positive weights")
    return result


def encode_cached(client, texts: list[str], route: str, root: Path, code: str, batch_size: int):
    profile = client.fingerprint()
    validate = (
        (lambda value: _dense_value(value, profile["dimension"]))
        if route == "dense"
        else (lambda value: _sparse_value(value, profile["vocab_size"]))
    )
    cache = root / "cache" / route
    cache.mkdir(parents=True, exist_ok=True)
    entries, pending, keys, hits = {}, {}, [], 0
    for text in texts:
        key = digest(
            {"text": text, "input_type": "document", "profile": profile, "implementation": code}
        )
        keys.append(key)
        if key in entries or key in pending:
            hits += 1
            continue
        path = cache / (key + ".json")
        if path.exists():
            try:
                with path.open("rb") as stream:
                    data = stream.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError("Model cache exceeds limit")
                value = json.loads(data)
                if (
                    value["schema_version"] != "docqa.model-cache.v1"
                    or value["key"] != key
                    or digest(value["payload"]) != value["payload_sha256"]
                ):
                    raise ValueError("Invalid cache identity")
                entries[key] = validate(value["payload"])
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                raise DocQAError(
                    "MODEL_CACHE_CORRUPT", "Cached model representation failed validation"
                ) from exc
            hits += 1
        else:
            pending[key] = text
    items = list(pending.items())
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        vectors = client.encode([text for _, text in batch], input_type="document")
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            raise DocQAError("MODEL_RESPONSE_INVALID", "Encoder returned an incomplete batch")
        for (key, _), vector in zip(batch, vectors, strict=True):
            try:
                vector = validate(vector)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                raise DocQAError(
                    "MODEL_RESPONSE_INVALID", "Encoder representation violates its profile"
                ) from exc
            stored = (
                {str(token): weight for token, weight in vector.items()}
                if route == "sparse"
                else vector
            )
            payload = {
                "schema_version": "docqa.model-cache.v1",
                "key": key,
                "payload": stored,
                "payload_sha256": digest(stored),
            }
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".tmp", dir=cache, encoding="utf-8", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(cache / (key + ".json"))
            entries[key] = vector
    return [entries[key] for key in keys], hits


def _source_record(path: Path):
    verification = verify_artifact(path)
    if verification["status"] != "complete":
        raise DocQAError(
            "SOURCE_NOT_READY",
            "DocBase only accepts complete doc2md artifacts; resolve parsing quality warnings first",
        )
    ir = DocumentIR.model_validate_json((path / "document.ir.json").read_bytes())
    source_map = json.loads((path / "source_map.json").read_bytes())
    member = {
        key: getattr(ir, key)
        for key in ("doc_id", "revision_id", "artifact_id", "category", "source_name")
    }
    member["artifact_ref"] = f"sources/artifacts/{ir.artifact_id}"
    return ir, source_map, member


def _build_result(manifest, directory, sequence, *, reused, cache_hits=None):
    return BuildResult(
        generation_id=manifest["generation_id"],
        generation_dir=str(directory),
        build_key=manifest["build_key"],
        publish_sequence=sequence,
        reused=reused,
        synthetic=manifest["synthetic"],
        production_ready=manifest["production_ready"],
        documents=manifest["member_count"],
        chunks=manifest["chunk_count"],
        cache_hits=cache_hits or {},
        warnings=["SYNTHETIC_DEMO: mechanics only; not genuine learned model retrieval"]
        if manifest["synthetic"]
        else [],
    )


def build_index(
    source: str | Path | list[Path] | None,
    output: str | Path = "runtime/docbase",
    *,
    options: DocBaseOptions | None = None,
    replace: bool = False,
    allow_empty: bool = False,
    providers=None,
    progress=None,
    publication_guard=None,
) -> BuildResult:
    """Merge explicitly selected documents by ID; replace is an explicit complete target set."""
    try:
        return _build_index(
            source,
            output,
            options or DocBaseOptions(),
            replace,
            allow_empty,
            providers,
            progress,
            publication_guard,
        )
    except OSError as exc:
        raise DocQAError("STORAGE_IO_FAILED", "Cannot read/write DocBase assets") from exc
    except sqlite3.Error as exc:
        raise DocQAError("CATALOG_FAILED", "Cannot read/write DocBase registry") from exc
    except portalocker.exceptions.LockException as exc:
        raise DocQAError(
            "LOCK_TIMEOUT", "Another DocBase writer holds this knowledge base", retryable=True
        ) from exc


def _build_index(
    source, output, options, replace, allow_empty, providers, progress, publication_guard=None
):
    paths = select_artifacts(source)
    root = Path(output).resolve()
    for path in paths:
        if is_within(root, path) or is_within(path, root):
            raise DocQAError(
                "INVALID_OUTPUT", "Source artifacts and DocBase output must be separate"
            )
    if not paths and not (replace and allow_empty):
        raise DocQAError(
            "EMPTY_BUILD_REJECTED",
            "No selected documents; explicit replace and allow_empty are required to publish an empty generation",
        )
    root.mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(exist_ok=True)
    (root / "generations").mkdir(exist_ok=True)
    emit = progress or (lambda stage, current, total: None)
    dense, sparse, reranker = providers or make_providers(options)
    analyzer = Analyzer(options.analyzer_terms)
    code = implementation_profile()
    profiles = {
        "schema_version": "docqa.docbase-profile.v1",
        "mode": options.mode,
        "chunk": chunk_profile(options.chunk),
        "analyzer": analyzer.profile(),
        "dense": dense.fingerprint(),
        "sparse": sparse.fingerprint(),
        "reranker": reranker.fingerprint(),
        "bm25": {"k1": options.bm25_k1, "b": options.bm25_b},
        "implementation": code,
    }
    with portalocker.Lock(root / ".docbase.lock", timeout=20):
        current, sequence = read_current(root)
        selected = {}
        if current and not replace:
            with load_generation(root / "generations" / current) as previous:
                for member in previous.members:
                    selected[member["doc_id"]] = previous.directory / member["artifact_ref"]
        explicit_ids = set()
        for index, path in enumerate(paths):
            emit("verify_sources", index, len(paths))
            ir, _, _ = _source_record(path)
            if ir.doc_id in explicit_ids:
                raise DocQAError(
                    "DUPLICATE_DOCUMENT",
                    "Input contains multiple artifacts for the same document ID; select one version explicitly",
                )
            explicit_ids.add(ir.doc_id)
            selected[ir.doc_id] = path
        chunks, members, packages = [], [], {}
        for index, (doc_id, path) in enumerate(sorted(selected.items())):
            emit("chunk", index, len(selected))
            ir, source_map, member = _source_record(path)
            document_chunks = chunk_artifact(ir, source_map, options.chunk)
            if not document_chunks:
                raise DocQAError(
                    "NO_INDEXABLE_CONTENT",
                    "A selected document produced no traceable indexable chunks",
                )
            chunks.extend(document_chunks)
            members.append(member)
            packages[ir.artifact_id] = path
        chunks.sort(key=lambda chunk: chunk.chunk_id)
        build_key = digest(
            {
                "members": members,
                "profiles": profiles,
                "chunks": [chunk.chunk_id for chunk in chunks],
            }
        )
        if current:
            with load_generation(root / "generations" / current) as previous:
                if previous.manifest["build_key"] == build_key:
                    with publication_guard() if publication_guard else nullcontext():
                        return _build_result(
                            previous.manifest, previous.directory, sequence, reused=True
                        )
        texts = [chunk.index_text for chunk in chunks]
        emit("dense", 0, len(texts))
        vectors, dense_hits = encode_cached(
            dense, texts, "dense", root, code, options.dense.batch_size
        )
        emit("dense", len(texts), len(texts))
        sparse_vectors, sparse_hits = encode_cached(
            sparse, texts, "sparse", root, code, options.sparse.batch_size
        )
        emit("sparse", len(texts), len(texts))
        bm25 = build_bm25(chunks, analyzer, options.bm25_k1, options.bm25_b)
        sparse_index = build_sparse(sparse_vectors)
        generation_id = uuid4().hex
        target = root / "generations" / generation_id
        with tempfile.TemporaryDirectory(prefix="build-", dir=_io_path(root / "staging")) as name:
            staging = Path(name)
            emit("write_and_validate", 0, 1)
            manifest = write_generation(
                staging,
                generation_id=generation_id,
                chunks=chunks,
                dense_vectors=vectors,
                bm25=bm25,
                sparse=sparse_index,
                members=members,
                profiles=profiles,
                build_key=build_key,
                source_packages=packages,
                empty=not chunks,
            )
            # write_generation already fsyncs the complete package with long-path-safe I/O.
            staging.replace(_io_path(target))
        with publication_guard() if publication_guard else nullcontext():
            new_sequence = publish(root, generation_id, current, sequence, build_key)
        emit("published", 1, 1)
        return _build_result(
            manifest,
            target,
            new_sequence,
            reused=False,
            cache_hits={"dense": dense_hits, "sparse": sparse_hits},
        )
