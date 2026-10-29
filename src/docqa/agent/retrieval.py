"""Bounded multi-query retrieval over one already-verified DocBase generation.

The ledger never opens the active pointer, expands a user's hard filters, or
adds RRF scores across rewrites. Each query supplies an independent fused list;
topic-aware round robin precedes one shared reranking operation.
"""

from __future__ import annotations

import math
import time
import unicodedata
from dataclasses import dataclass, field

from docqa.docbase.config import DocBaseOptions
from docqa.docbase.retrieval import CandidatePool, collect_candidates, validate_recall_selection
from docqa.domain.errors import DocQAError
from docqa.domain.rag import RetrievalHit


def normalize_query(question: str) -> str:
    """Normalize presentation whitespace without case-folding technical names."""
    if not isinstance(question, str) or not question.strip():
        raise DocQAError("INVALID_PLAN", "A subquery must be nonempty")
    if len(question.encode("utf-8")) > 16000:
        raise DocQAError("INVALID_PLAN", "A subquery exceeds 16000 UTF-8 bytes")
    return " ".join(unicodedata.normalize("NFKC", question).split())


@dataclass(frozen=True)
class Subquery:
    query: str
    topic_ids: tuple[str, ...]


@dataclass
class RetrievalBatch:
    hits: list[RetrievalHit]
    trace: dict
    new_chunk_ids: list[str]
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    empty_reason: str | None = None


@dataclass(frozen=True)
class _QueryList:
    question: str
    normalized: str
    topic_ids: tuple[str, ...]
    round_index: int
    query_index: int
    pool: CandidatePool


class CandidateLedger:
    """Hold a generation for the caller's lifetime and retain all fused windows.

    ``generation`` must be held open by its ``load_generation`` context. Source
    filters are copied into immutable tuples and applied before every route's
    ranking. Network deadlines/cancellation remain the orchestration layer's
    responsibility; providers have their own bounded I/O timeouts.
    """

    def __init__(
        self,
        generation,
        original_question: str,
        *,
        options: DocBaseOptions,
        providers,
        topic_order: list[str],
        categories: list[str] | None = None,
        doc_ids: list[str] | None = None,
        agent_allow_degraded: bool = False,
        max_pool: int = 100,
        rerank_top_n: int = 50,
        top_k: int | None = None,
        max_queries: int = 6,
    ):
        normalize_query(original_question)
        if not topic_order or len(set(topic_order)) != len(topic_order):
            raise DocQAError("INVALID_PLAN", "Topics must have distinct nonempty identifiers")
        if any(not isinstance(value, str) or not value.strip() for value in topic_order):
            raise DocQAError("INVALID_PLAN", "Topics must have distinct nonempty identifiers")
        for values in (categories, doc_ids):
            if values is not None and (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise DocQAError("INVALID_REQUEST", "Hard filters must be lists of identifiers")
        selected_count = options.top_k if top_k is None else top_k
        for name, value, ceiling in (
            ("max_pool", max_pool, 10000),
            ("rerank_top_n", rerank_top_n, max_pool),
            ("top_k", selected_count, rerank_top_n),
            ("max_queries", max_queries, 100),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise DocQAError("INVALID_CONFIG", f"Invalid Agent {name} budget")
        if not isinstance(agent_allow_degraded, bool):
            raise DocQAError("INVALID_CONFIG", "Agent degradation permission must be boolean")
        if generation.profiles["mode"] != options.mode:
            raise DocQAError("PROFILE_MISMATCH", "Demo and real retrieval generations differ")
        validate_recall_selection(generation, options)
        self.generation = generation
        self.generation_id = generation.manifest["generation_id"]
        self.question = original_question
        # Copy validated options: later mutation of the caller's config cannot
        # alter the filters/budgets/profiles of an already-running request.
        self.options = options.model_copy(deep=True)
        self.providers = providers
        self.topic_order = tuple(topic_order)
        self.categories = None if categories is None else tuple(categories)
        self.doc_ids = None if doc_ids is None else tuple(doc_ids)
        self.eligible = tuple(
            row
            for row, chunk in enumerate(generation.chunks)
            if (self.categories is None or chunk.category in self.categories)
            and (self.doc_ids is None or chunk.doc_id in self.doc_ids)
        )
        self.allow_degraded = bool(options.allow_degraded and agent_allow_degraded)
        self.max_pool, self.rerank_top_n, self.top_k = max_pool, rerank_top_n, selected_count
        self.max_queries = max_queries
        self._lists: list[_QueryList] = []
        self._seen_queries: set[str] = set()
        self._attempted_queries: list[str] = []
        self._observed_chunks: set[str] = set()
        self._warnings: list[str] = []
        self._last_round = 0
        self._rerank_cache: dict[tuple[int, ...], dict[int, float]] = {}

    @property
    def seen_queries(self) -> tuple[str, ...]:
        return tuple(self._attempted_queries)

    @property
    def query_count(self) -> int:
        return len(self._attempted_queries)

    def _ordered_lists(self, uncovered_topics):
        unknown = set(uncovered_topics) - set(self.topic_order)
        if unknown:
            raise DocQAError("INVALID_PLAN", "Unknown uncovered topic identifier")
        order = {topic: index for index, topic in enumerate(self.topic_order)}
        uncovered = set(uncovered_topics)
        return sorted(
            self._lists,
            key=lambda value: (
                not bool(uncovered.intersection(value.topic_ids)),
                min(order[topic] for topic in value.topic_ids),
                -value.round_index,
                value.query_index,
            ),
        )

    def _round_robin(self, lists):
        output, seen = [], set()
        # A duplicate occupies its position but not another list's turn. Repeated
        # rewrites therefore do not increase numeric votes or hide later lists.
        for offset in range(max((len(value.pool.rows) for value in lists), default=0)):
            for query_list in lists:
                if offset >= len(query_list.pool.rows):
                    continue
                row = query_list.pool.rows[offset]
                if row in seen:
                    continue
                seen.add(row)
                output.append(row)
                if len(output) >= self.max_pool:
                    return output
        return output

    def collect(
        self,
        subqueries: list[Subquery],
        *,
        round_index: int,
        uncovered_topics: list[str],
    ) -> RetrievalBatch:
        if (
            isinstance(round_index, bool)
            or not isinstance(round_index, int)
            or round_index != self._last_round + 1
        ):
            raise DocQAError("INVALID_PLAN", "Retrieval round indices must be consecutive from one")
        if not isinstance(subqueries, list) or not 1 <= len(subqueries) <= 2:
            raise DocQAError("INVALID_PLAN", "Each retrieval round requires one or two subqueries")
        prepared, local_seen, skipped = [], set(), []
        topics = set(self.topic_order)
        for value in subqueries:
            if not isinstance(value, Subquery):
                raise DocQAError("INVALID_PLAN", "Subqueries must use the validated contract")
            normalized = normalize_query(value.query)
            if not value.topic_ids or set(value.topic_ids) - topics:
                raise DocQAError("INVALID_PLAN", "Subquery references an unknown topic")
            if normalized in self._seen_queries or normalized in local_seen:
                skipped.append(normalized)
                continue
            local_seen.add(normalized)
            prepared.append((value, normalized))
        self._ordered_lists(uncovered_topics)  # Validate before executing any calls.
        if self.eligible and self.query_count + len(prepared) > self.max_queries:
            raise DocQAError("BUDGET_EXCEEDED", "Agent retrieval query budget exhausted")
        self._last_round = round_index
        calls = []
        if self.eligible:
            for query_index, (value, normalized) in enumerate(prepared):
                # Reserve before calling: failed/timeout requests consume budget.
                self._seen_queries.add(normalized)
                self._attempted_queries.append(normalized)
                try:
                    pool = collect_candidates(
                        self.generation,
                        value.query,
                        options=self.options,
                        providers=self.providers,
                        eligible=self.eligible,
                        mode="hybrid",
                        allow_degraded=self.allow_degraded,
                    )
                except DocQAError as exc:
                    if exc.code == "MODEL_DEADLINE_EXCEEDED":
                        raise DocQAError(
                            "AGENT_DEADLINE_EXCEEDED", "Retrieval exhausted the answer time budget"
                        ) from exc
                    raise
                if any(row not in self.eligible for row in pool.rows):
                    raise DocQAError("RETRIEVAL_FAILED", "A recall route escaped the fixed scope")
                if any(route.error_code == "PROFILE_MISMATCH" for route in pool.routes.values()):
                    raise DocQAError(
                        "PROFILE_MISMATCH", "A query encoder reported profile mismatch"
                    )
                query_list = _QueryList(
                    value.query,
                    normalized,
                    tuple(dict.fromkeys(value.topic_ids)),
                    round_index,
                    query_index,
                    pool,
                )
                self._lists.append(query_list)
                self._warnings.extend(pool.warnings)
                calls.append(
                    {
                        "query": value.query,
                        "topic_ids": list(query_list.topic_ids),
                        "routes": {k: v.model_dump(mode="json") for k, v in pool.routes.items()},
                        "candidate_count": pool.total_candidates,
                        "fused_window_count": len(pool.rows),
                        "timings": pool.timings,
                    }
                )
        ordered = self._ordered_lists(uncovered_topics)
        merged = self._round_robin(ordered)
        rerank_rows = merged[: self.rerank_top_n]
        # Select one query-list occurrence for trace/raw-score representation;
        # never sum scores produced for different subqueries or rounds.
        representative = {
            row: next(value for value in ordered if row in value.pool.rows) for row in rerank_rows
        }
        rerank_key = tuple(sorted(rerank_rows))
        rerank_scores = dict(self._rerank_cache.get(rerank_key, {}))
        reranker_state = "cached" if rerank_scores else "not_called"
        rerank_started = time.perf_counter()
        if rerank_rows and prepared and not rerank_scores:
            try:
                values = self.providers[2].rerank(
                    self.question,
                    [self.generation.chunks[row].index_text for row in rerank_rows],
                )
                if (
                    not isinstance(values, list)
                    or len(values) != len(rerank_rows)
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in values
                    )
                ):
                    raise DocQAError(
                        "MODEL_RESPONSE_INVALID", "Unified reranker scores are invalid"
                    )
                rerank_scores = dict(zip(rerank_rows, values, strict=True))
                self._rerank_cache[rerank_key] = dict(rerank_scores)
                reranker_state = "ok"
            except (DocQAError, OSError, ValueError, TypeError, OverflowError) as exc:
                code = exc.code if isinstance(exc, DocQAError) else "MODEL_RESPONSE_INVALID"
                if code == "MODEL_DEADLINE_EXCEEDED":
                    raise DocQAError(
                        "AGENT_DEADLINE_EXCEEDED", "Reranking exhausted the answer time budget"
                    ) from exc
                if not self.allow_degraded or code == "PROFILE_MISMATCH":
                    raise DocQAError(
                        "RERANK_FAILED", f"Required unified rerank failed: {code}"
                    ) from exc
                self._warnings.append(f"reranker:{code}; explicit topic-round-robin fallback")
                reranker_state = "failed"
        rerank_seconds = (
            round(time.perf_counter() - rerank_started, 6)
            if reranker_state in {"ok", "failed"}
            else 0.0
        )
        if rerank_scores:
            position = {row: index for index, row in enumerate(rerank_rows)}
            rerank_rows.sort(
                key=lambda row: (
                    -rerank_scores[row],
                    position[row],
                    self.generation.chunks[row].chunk_id,
                )
            )
        all_new = sorted(
            {self.generation.chunks[row].chunk_id for row in merged} - self._observed_chunks
        )
        self._observed_chunks.update(self.generation.chunks[row].chunk_id for row in merged)
        hits = []
        for rank, row in enumerate(rerank_rows[: self.top_k], 1):
            chunk, selected = self.generation.chunks[row], representative[row]
            pool = selected.pool
            hits.append(
                RetrievalHit(
                    **{
                        name: getattr(chunk, name)
                        for name in (
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
                    generation_id=self.generation_id,
                    route_ranks=pool.ranks[row],
                    route_scores=pool.raw_scores[row],
                    rrf_score=pool.scores[row],
                    rerank_score=rerank_scores.get(row),
                    final_rank=rank,
                )
            )
        reason = None
        if not self.eligible:
            reason = "index_empty" if not self.generation.chunks else "no_eligible_documents"
        elif not hits:
            reason = "no_matches"
        return RetrievalBatch(
            hits=hits,
            trace={
                "merge_rule": "topic-round-robin-v1",
                "generation_id": self.generation_id,
                "round_index": round_index,
                "calls": calls,
                "skipped_duplicate_queries": skipped,
                "applied_filter": {
                    "categories": None if self.categories is None else list(self.categories),
                    "doc_ids": None if self.doc_ids is None else list(self.doc_ids),
                },
                "eligible_count": len(self.eligible),
                "query_count": self.query_count,
                "candidate_pool_limit": self.max_pool,
                "candidate_pool": [self.generation.chunks[row].chunk_id for row in merged],
                "rerank_input": [
                    self.generation.chunks[row].chunk_id for row in merged[: self.rerank_top_n]
                ],
                "reranker_state": reranker_state,
                "rerank_seconds": rerank_seconds,
                "selected_chunks": [hit.chunk_id for hit in hits],
                "query_lists": [
                    {
                        "query": value.question,
                        "round_index": value.round_index,
                        "topic_ids": list(value.topic_ids),
                        "chunk_ids": [
                            self.generation.chunks[row].chunk_id for row in value.pool.rows
                        ],
                    }
                    for value in ordered
                ],
                "candidate_occurrences": {
                    self.generation.chunks[row].chunk_id: [
                        {
                            "query": value.question,
                            "round_index": value.round_index,
                            "rrf_rank": value.pool.rows.index(row) + 1,
                            "rrf_score": value.pool.scores[row],
                        }
                        for value in ordered
                        if row in value.pool.rows
                    ]
                    for row in rerank_rows
                },
            },
            new_chunk_ids=all_new,
            degraded=bool(self._warnings),
            warnings=list(dict.fromkeys(self._warnings)),
            empty_reason=reason,
        )
