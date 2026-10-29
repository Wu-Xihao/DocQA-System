"""SQLite-vec local persistent vector database inside an immutable generation."""

import json
import math
import sqlite3

import numpy as np

from docqa.domain.errors import DocQAError


def connection(path, *, readonly=False):
    from docqa.docbase.storage import _io_path, _sqlite_uri

    try:
        import sqlite_vec
    except ImportError as exc:
        raise DocQAError(
            "VECTOR_DB_DEPENDENCY_MISSING",
            "Install sqlite-vec to use the configured vector database",
        ) from exc
    db = sqlite3.connect(_sqlite_uri(path) if readonly else _io_path(path), uri=readonly)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        return db
    except BaseException:
        db.close()
        raise


def create(path, vectors):
    db = connection(path)
    try:
        db.execute(
            f"CREATE VIRTUAL TABLE vectors USING vec0(embedding float[{vectors.shape[1]}] distance_metric=cosine)"
        )
        db.executemany(
            "INSERT INTO vectors(rowid,embedding) VALUES(?,?)",
            ((i, row.tobytes()) for i, row in enumerate(vectors)),
        )
        db.commit()
    finally:
        db.close()


def verify(path, vectors):
    db = connection(path, readonly=True)
    try:
        count = 0
        for identity, value in db.execute("SELECT rowid,embedding FROM vectors ORDER BY rowid"):
            if count >= len(vectors) or identity != count or value != vectors[count].tobytes():
                raise ValueError("Vector database differs from bound embedding rows")
            count += 1
        if count != len(vectors):
            raise ValueError("Vector database row count differs")
    finally:
        db.close()


def search(path, vector, eligible, limit, *, dimension, rows):
    q = np.asarray(vector, dtype=np.float32)
    if (
        q.shape != (dimension,)
        or not np.isfinite(q).all()
        or not math.isclose(
            float(np.linalg.norm(q.astype(np.float64))), 1, rel_tol=2e-5, abs_tol=2e-5
        )
    ):
        raise DocQAError("MODEL_OUTPUT_INVALID", "Invalid normalized query embedding")
    allowed = sorted(set(eligible))
    if (
        type(limit) is not int
        or limit < 1
        or any(type(i) is not int or not 0 <= i < rows for i in allowed)
    ):
        raise DocQAError("INVALID_REQUEST", "Invalid vector query scope")
    if not allowed:
        return []
    db = connection(path, readonly=True)
    try:
        # Restrict rows BEFORE KNN; a single JSON parameter avoids SQL variable limits.
        result = db.execute(
            "SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=? AND rowid IN (SELECT value FROM json_each(?)) ORDER BY distance",
            (q.tobytes(), min(limit, len(allowed)), json.dumps(allowed)),
        ).fetchall()
        return sorted(
            [(identity, float(1 - distance)) for identity, distance in result],
            key=lambda item: (-item[1], item[0]),
        )
    finally:
        db.close()
