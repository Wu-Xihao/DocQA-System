"""Transparent BM25 and independent learned-sparse inverted indices."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence

from docqa.domain.errors import DocQAError


def build_bm25(chunks: Sequence, analyzer, k1: float = 1.2, b: float = 0.75) -> dict:
    if not math.isfinite(k1) or k1 <= 0 or not math.isfinite(b) or not 0 <= b <= 1:
        raise DocQAError("INVALID_CONFIG", "BM25 requires k1 > 0 and 0 <= b <= 1")
    postings: dict[str, list[list[int]]] = defaultdict(list)
    lengths: list[int] = []
    for row, chunk in enumerate(chunks):
        text = chunk if isinstance(chunk, str) else chunk.index_text
        terms = analyzer.analyze(text)
        lengths.append(len(terms))
        for term, frequency in sorted(Counter(terms).items()):
            postings[term].append([row, frequency])
    return {
        "schema_version": "docqa.bm25.v1",
        "row_count": len(chunks),
        "doc_lengths": lengths,
        "avgdl": sum(lengths) / len(lengths) if lengths else 0.0,
        "k1": k1,
        "b": b,
        "analyzer_profile": analyzer.profile(),
        "postings": dict(sorted(postings.items())),
    }


def _eligible_rows(eligible: Iterable[int] | None, size: int) -> set[int] | range:
    if eligible is None:
        return range(size)
    rows = list(eligible)
    if any(
        isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < size for row in rows
    ):
        raise DocQAError("INVALID_FILTER", "eligible rows contain an out-of-range row")
    return set(rows)


def _rank(scores: dict[int, float], limit: int) -> list[tuple[int, float]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise DocQAError("INVALID_INPUT", "retrieval limit must be a nonnegative integer")
    return sorted(
        ((row, score) for row, score in scores.items() if score > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )[:limit]


def search_bm25(
    index: dict, question_tokens: Iterable[str], eligible: Iterable[int] | None, limit: int
) -> list[tuple[int, float]]:
    size = index["row_count"]
    rows = _eligible_rows(eligible, size)
    scores: dict[int, float] = defaultdict(float)
    avgdl = index["avgdl"]
    if size == 0 or avgdl == 0:
        return _rank(scores, limit)
    k1, b = index["k1"], index["b"]
    for term in sorted(set(question_tokens)):
        posting = index["postings"].get(term, [])
        frequency = len(posting)
        if not frequency:
            continue
        # Corpus-wide IDF remains fixed when callers restrict rows/categories.
        idf = math.log(1 + (size - frequency + 0.5) / (frequency + 0.5))
        for row, tf in posting:
            if row in rows:
                normalizer = k1 * (1 - b + b * index["doc_lengths"][row] / avgdl)
                scores[row] += idf * tf * (k1 + 1) / (tf + normalizer)
    return _rank(scores, limit)


def _sparse_vector(vector: Mapping[int, float]) -> dict[int, float]:
    if not isinstance(vector, Mapping):
        raise DocQAError("INVALID_SPARSE_VECTOR", "sparse vector must be a token-to-weight mapping")
    result = {}
    for token, weight in vector.items():
        if isinstance(token, bool) or not isinstance(token, int) or token < 0 or token >= 2**31:
            raise DocQAError("INVALID_SPARSE_VECTOR", "sparse token IDs must be nonnegative int32")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise DocQAError(
                "INVALID_SPARSE_VECTOR", "sparse weights must be finite and nonnegative"
            )
        if weight:
            result[token] = float(weight)
    return result


def build_sparse(vectors: list[dict[int, float]]) -> dict:
    postings: dict[str, list[list[int | float]]] = defaultdict(list)
    counts = []
    for row, vector in enumerate(vectors):
        normalized = _sparse_vector(vector)
        counts.append(len(normalized))
        for token, weight in sorted(normalized.items()):
            postings[str(token)].append([row, weight])
    return {
        "schema_version": "docqa.sparse.v1",
        "row_count": len(vectors),
        "nonzero_counts": counts,
        "postings": dict(sorted(postings.items(), key=lambda item: int(item[0]))),
    }


def search_sparse(
    index: dict, query: dict[int, float], eligible: Iterable[int] | None, limit: int
) -> list[tuple[int, float]]:
    rows = _eligible_rows(eligible, index["row_count"])
    scores: dict[int, float] = defaultdict(float)
    for token, query_weight in sorted(_sparse_vector(query).items()):
        for row, document_weight in index["postings"].get(str(token), []):
            if row in rows:
                scores[row] += query_weight * document_weight
    if any(not math.isfinite(score) for score in scores.values()):
        raise DocQAError("INVALID_SPARSE_VECTOR", "sparse inner product overflowed")
    return _rank(scores, limit)
