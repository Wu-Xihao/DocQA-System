"""Filter before recall, retain route evidence, fuse ranks and rerank one fixed generation."""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from docqa.doc2md.common import digest
from docqa.docbase.analyzer import Analyzer
from docqa.docbase.config import DocBaseOptions
from docqa.docbase.lexical import search_bm25, search_sparse
from docqa.docbase.pipeline import _dense_value, _sparse_value
from docqa.docbase.providers import make_providers
from docqa.docbase.storage import load_generation, read_current, search_dense
from docqa.domain.errors import DocQAError
from docqa.domain.rag import QueryResult, RetrievalHit, RouteResult

MODES = {"hybrid", "rrf", "dense", "bm25", "sparse"}


def generation_directory(index: str | Path, generation_id: str | None = None) -> Path:
    root = Path(index).resolve()
    if (root / "manifest.json").is_file():
        if generation_id and root.name != generation_id:
            raise DocQAError(
                "INVALID_REQUEST", "Explicit generation differs from selected directory"
            )
        return root
    selected = generation_id or read_current(root)[0]
    if selected is None:
        raise DocQAError("INDEX_NOT_FOUND", "No published DocBase generation")
    if not re.fullmatch(r"[0-9a-f]{32}", selected):
        raise DocQAError("INVALID_REQUEST", "Invalid generation identifier")
    return root / "generations" / selected


def fuse_routes(routes: dict[str, list[tuple[int, float]]], chunks, weights, rrf_k: int):
    scores, ranks, raw = {}, {}, {}
    for route, values in routes.items():
        seen = set()
        # Repeated rows cannot add votes; tie-break is source-preserving chunk ID.
        ordered = sorted(values, key=lambda value: (-value[1], chunks[value[0]].chunk_id))
        for row, score in ordered:
            if row in seen:
                continue
            seen.add(row)
            rank = len(seen)
            scores[row] = scores.get(row, 0.0) + weights[route] / (rrf_k + rank)
            ranks.setdefault(row, {})[route] = rank
            raw.setdefault(row, {})[route] = score
    order = sorted(scores, key=lambda row: (-scores[row], chunks[row].chunk_id))
    return order, scores, ranks, raw


@dataclass
class CandidatePool:
    rows: list[int]
    scores: dict
    ranks: dict
    raw_scores: dict
    route_rows: dict
    routes: dict[str, RouteResult]
    warnings: list[str]
    total_candidates: int


def collect_candidates(
    generation, question, *, options, providers, eligible, mode="hybrid", allow_degraded=False
):
    """Return the fused window BEFORE final TOP-K; later Agent can merge subqueries."""
    dense, sparse, _ = providers
    required = ("dense", "bm25", "sparse") if mode in {"hybrid", "rrf"} else (mode,)
    analyzer = Analyzer(options.analyzer_terms)
    if analyzer.profile() != generation.profiles["analyzer"]:
        raise DocQAError("PROFILE_MISMATCH", "BM25 analyzer differs from the frozen generation")
    for route, provider in (("dense", dense), ("sparse", sparse)):
        if route in required and provider.fingerprint() != generation.profiles[route]:
            raise DocQAError(
                "PROFILE_MISMATCH",
                f"{route} query encoder differs from the frozen document encoder",
            )

    def encode_dense():
        values = dense.encode([question], input_type="query")
        if not isinstance(values, list) or len(values) != 1:
            raise DocQAError(
                "MODEL_RESPONSE_INVALID", "Query encoder returned an incomplete result"
            )
        vector = _dense_value(values[0], generation.vectors.shape[1])
        return search_dense(generation.vectors, vector, eligible, options.route_top_n)

    def encode_sparse():
        values = sparse.encode([question], input_type="query")
        if not isinstance(values, list) or len(values) != 1:
            raise DocQAError(
                "MODEL_RESPONSE_INVALID", "Sparse query encoder returned an incomplete result"
            )
        vector = _sparse_value(values[0], sparse.fingerprint()["vocab_size"])
        return search_sparse(generation.sparse, vector, eligible, options.route_top_n)

    functions = {
        "dense": encode_dense,
        "sparse": encode_sparse,
        "bm25": lambda: search_bm25(
            generation.bm25, analyzer.analyze(question), eligible, options.route_top_n
        ),
    }
    results, diagnostics, warnings = {}, {}, []
    with ThreadPoolExecutor(
        max_workers=len(required), thread_name_prefix="docbase-recall"
    ) as executor:
        futures = {route: executor.submit(functions[route]) for route in required}
        for route, future in futures.items():
            try:
                values = future.result()
                results[route] = values
                diagnostics[route] = RouteResult(
                    state="ok" if values else "empty", count=len(values)
                )
            except (DocQAError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                code = exc.code if isinstance(exc, DocQAError) else "MODEL_RESPONSE_INVALID"
                diagnostics[route] = RouteResult(state="failed", error_code=code)
                warnings.append(f"{route}:{code}")
    if warnings and not allow_degraded:
        raise DocQAError(
            "RETRIEVAL_FAILED", "A required recall route failed: " + ", ".join(warnings)
        )
    if not results:
        raise DocQAError("RETRIEVAL_FAILED", "All requested recall routes failed")
    rows, scores, ranks, raw = fuse_routes(
        results, generation.chunks, options.route_weights, options.rrf_k
    )
    return CandidatePool(
        rows[: options.fusion_top_n], scores, ranks, raw, results, diagnostics, warnings, len(rows)
    )


def query(
    index: str | Path,
    question: str,
    *,
    options: DocBaseOptions | None = None,
    generation_id: str | None = None,
    categories: list[str] | None = None,
    doc_ids: list[str] | None = None,
    top_k: int | None = None,
    mode: str = "hybrid",
    allow_degraded: bool | None = None,
    providers=None,
) -> QueryResult:
    """Return evidence, not a generated answer or a calibrated answerability claim."""
    options = options or DocBaseOptions()
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question.encode("utf-8")) > 16000
    ):
        raise DocQAError(
            "INVALID_REQUEST", "Question must be nonempty and at most 16000 UTF-8 bytes"
        )
    if mode not in MODES:
        raise DocQAError("INVALID_REQUEST", "Unknown retrieval mode")
    count = options.top_k if top_k is None else top_k
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= min(options.fusion_top_n, 1000)
    ):
        raise DocQAError("INVALID_REQUEST", "top_k exceeds the configured candidate budget")
    if any(not isinstance(values, list) for values in (categories, doc_ids) if values is not None):
        raise DocQAError("INVALID_REQUEST", "Hard filter containers must be lists")
    if any(
        not isinstance(value, str) or not value.strip()
        for values in (categories, doc_ids)
        if values is not None
        for value in values
    ):
        raise DocQAError("INVALID_REQUEST", "Hard filters must contain nonempty identifiers")
    if allow_degraded is not None and not isinstance(allow_degraded, bool):
        raise DocQAError("INVALID_REQUEST", "allow_degraded must be a boolean")
    permitted = options.allow_degraded if allow_degraded is None else allow_degraded
    with load_generation(generation_directory(index, generation_id)) as generation:
        if generation.profiles["mode"] != options.mode:
            raise DocQAError("PROFILE_MISMATCH", "Demo and real model generations cannot be mixed")
        common = dict(
            query_id=uuid4().hex,
            generation_id=generation.manifest["generation_id"],
            question=question,
            synthetic=generation.manifest["synthetic"],
            production_ready=generation.manifest["production_ready"],
            retrieval_mode=mode,
            applied_filter={"categories": categories, "doc_ids": doc_ids},
            retrieval_profile_id=digest(
                {
                    "options": options.model_dump(mode="json"),
                    "top_k": count,
                    "mode": mode,
                    "allow_degraded": permitted,
                }
            ),
        )
        eligible = [
            row
            for row, chunk in enumerate(generation.chunks)
            if (categories is None or chunk.category in categories)
            and (doc_ids is None or chunk.doc_id in doc_ids)
        ]
        if not eligible:
            return QueryResult(
                **common,
                status="empty",
                reason="index_empty" if not generation.chunks else "no_eligible_documents",
                hits=[],
                routes={},
                candidate_count=0,
            )
        services = providers or make_providers(options)
        if any(service.fingerprint().get("synthetic", False) for service in services):
            common["synthetic"], common["production_ready"] = True, False
        common["retrieval_profile_id"] = digest(
            {
                "request_profile": common["retrieval_profile_id"],
                "services": [service.fingerprint() for service in services],
            }
        )
        pool = collect_candidates(
            generation,
            question,
            options=options,
            providers=services,
            eligible=eligible,
            mode=mode,
            allow_degraded=permitted,
        )
        return _finish(generation, question, common, pool, services[2], count, mode, permitted)


def _finish(generation, question, common, pool, reranker, count, mode, permitted):
    rerank_scores = {}
    if pool.rows and mode == "hybrid":
        try:
            values = reranker.rerank(
                question, [generation.chunks[row].index_text for row in pool.rows]
            )
            if (
                not isinstance(values, list)
                or len(values) != len(pool.rows)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in values
                )
            ):
                raise DocQAError(
                    "MODEL_RESPONSE_INVALID", "Reranker returned invalid or incomplete scores"
                )
            rerank_scores = dict(zip(pool.rows, values, strict=True))
            pool.rows.sort(
                key=lambda row: (
                    -rerank_scores[row],
                    -pool.scores[row],
                    generation.chunks[row].chunk_id,
                )
            )
            pool.routes["reranker"] = RouteResult(state="ok", count=len(values))
        except (DocQAError, OSError, ValueError, TypeError) as exc:
            code = exc.code if isinstance(exc, DocQAError) else "MODEL_RESPONSE_INVALID"
            if not permitted:
                raise DocQAError("RERANK_FAILED", f"Required reranker failed: {code}") from exc
            pool.warnings.append(f"reranker:{code}; explicit RRF fallback")
            pool.routes["reranker"] = RouteResult(state="failed", error_code=code)
    hits = []
    for rank, row in enumerate(pool.rows[:count], 1):
        chunk = generation.chunks[row]
        hits.append(
            RetrievalHit(
                **{
                    field: getattr(chunk, field)
                    for field in (
                        "chunk_id",
                        "doc_id",
                        "artifact_id",
                        "source_name",
                        "category",
                        "display_text",
                        "index_text",
                        "section",
                        "source_spans",
                        "figure_ids",
                    )
                },
                generation_id=common["generation_id"],
                route_ranks=pool.ranks[row],
                route_scores=pool.raw_scores[row],
                rrf_score=pool.scores[row],
                rerank_score=rerank_scores.get(row),
                final_rank=rank,
            )
        )
    warnings = list(pool.warnings)
    if pool.warnings:
        common["production_ready"] = False
    if common["synthetic"]:
        warnings.append(
            "SYNTHETIC_DEMO: generated features and scores are not genuine learned models"
        )
    return QueryResult(
        **common,
        status="degraded" if pool.warnings else ("ok" if hits else "empty"),
        reason=None if hits else "no_matches",
        hits=hits,
        routes=pool.routes,
        candidate_count=pool.total_candidates,
        warnings=warnings,
    )


def resolve_chunk(index: str | Path, chunk_id: str, *, generation_id: str | None = None) -> dict:
    """Resolve IDs to package-owned paths; never accept a model-supplied file path."""
    with load_generation(generation_directory(index, generation_id)) as generation:
        chunk = next((value for value in generation.chunks if value.chunk_id == chunk_id), None)
        if chunk is None:
            raise DocQAError("CHUNK_NOT_FOUND", "Chunk does not belong to this generation")
        member = next(
            value for value in generation.members if value["artifact_id"] == chunk.artifact_id
        )
        artifact = generation.directory / member["artifact_ref"]
        manifest = json.loads((artifact / "manifest.json").read_bytes())
        return {
            "schema_version": "docqa.resolved-source.v1",
            "generation_id": generation.manifest["generation_id"],
            "chunk_id": chunk.chunk_id,
            "artifact_dir": str(artifact),
            "markdown_path": str(artifact / "document.md"),
            "snapshot_path": str((artifact.parent.parent / manifest["snapshot_ref"]).resolve()),
            "source_spans": [span.model_dump(mode="json") for span in chunk.source_spans],
            "figure_ids": chunk.figure_ids,
        }
