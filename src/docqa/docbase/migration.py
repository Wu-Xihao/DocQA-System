"""Offline storage migration: reuses checked embeddings and never invokes models."""

import tempfile
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.docbase.storage import (
    _digest,
    _io_path,
    load_generation,
    publish,
    read_current,
    write_generation,
)
from docqa.domain.errors import DocQAError


def migrate_vector_backend(index, backend="sqlite_vec"):
    if backend not in {"numpy", "sqlite_vec"}:
        raise DocQAError("INVALID_REQUEST", "Unknown vector backend")
    root = Path(index).resolve()
    with portalocker.Lock(_io_path(root / ".docbase.lock"), timeout=20):
        current, sequence = read_current(root)
        if not current:
            raise DocQAError("INDEX_NOT_FOUND", "No generation to migrate")
        with load_generation(root / "generations" / current) as old:
            if old.profiles.get("vector_backend", "numpy") == backend:
                return {
                    "generation_id": current,
                    "reused": True,
                    "vector_backend": backend,
                    "model_calls": 0,
                }
            profiles = dict(old.profiles)
            if backend == "numpy":
                profiles.pop("vector_backend", None)
            else:
                profiles["vector_backend"] = backend
            identity = uuid4().hex
            key = _digest({"source_generation": current, "vector_backend": backend})
            _io_path(root / "staging").mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="vector-migration-", dir=_io_path(root / "staging")
            ) as tmp:
                staging = Path(tmp)
                write_generation(
                    staging,
                    generation_id=identity,
                    chunks=old.chunks,
                    dense_vectors=old.vectors,
                    bm25=old.bm25,
                    sparse=old.sparse,
                    members=old.members,
                    profiles=profiles,
                    build_key=key,
                    source_packages={
                        m["artifact_id"]: old.directory / m["artifact_ref"] for m in old.members
                    },
                    empty=not old.chunks,
                )
                from docqa.docbase.storage import install_generation

                install_generation(root, staging, identity)
            publish(root, identity, current, sequence, key)
    return {
        "generation_id": identity,
        "previous_generation": current,
        "reused": False,
        "vector_backend": backend,
        "model_calls": 0,
    }
