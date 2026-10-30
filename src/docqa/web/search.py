"""Direct snippet search: ranked chunks with source locations, no generated answer.

This exposes the same frozen-generation hybrid retrieval the Q&A agent uses,
so users can inspect raw evidence and jump to the Markdown / original file.
Results are retrieval candidates, not verified answers or citations.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Request
from pydantic import Field, field_validator

from docqa.docbase.admission import COVERAGE_WARNING
from docqa.docbase.filters import ChunkFilter
from docqa.domain.documents import Contract

MAX_TEXT = 1200


class SearchRequest(Contract):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)
    categories: list[str] | None = Field(default=None, min_length=1, max_length=100)
    doc_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    filters: ChunkFilter | None = None
    conversation_id: str | None = Field(default=None, max_length=100)

    @field_validator("question")
    @classmethod
    def text(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Question must be nonempty text")
        return value.strip()


class SearchMetrics:
    """Bounded in-process latency log shared with the statistics panel."""

    def __init__(self, size=500):
        self.items = deque(maxlen=size)
        self.lock = threading.Lock()

    def add(self, elapsed_ms, status, hits):
        with self.lock:
            self.items.append({"at": time.time(), "ms": elapsed_ms, "status": status, "hits": hits})

    def snapshot(self):
        with self.lock:
            return list(self.items)


def _locations(chunk_hit):
    seen, labels, blocks = set(), [], []
    for span in chunk_hit.source_spans:
        if span.block_id not in blocks:
            blocks.append(span.block_id)
        for locator in span.locators:
            if locator.display_label not in seen:
                seen.add(locator.display_label)
                labels.append(locator.display_label)
    return labels[:6], blocks[:6]


def project_hit(hit, conversation_id=None):
    labels, blocks = _locations(hit)
    suffix = f"?conversation_id={conversation_id}" if conversation_id else ""
    joiner = "&" if suffix else "?"
    text = hit.display_text
    return {
        "rank": hit.final_rank,
        "chunk_id": hit.chunk_id,
        "doc_id": hit.doc_id,
        "source_name": hit.source_name,
        "category": hit.category,
        "section": hit.section,
        "text": text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…",
        "truncated": len(text) > MAX_TEXT,
        "origins": sorted({span.origin for span in hit.source_spans}),
        "locations": labels,
        "block_ids": blocks,
        "figure_ids": hit.figure_ids,
        "scores": {
            "rerank": hit.rerank_score,
            "rrf": round(hit.rrf_score, 6),
            "routes": hit.route_ranks,
        },
        "links": {
            "markdown": f"/api/v1/documents/{hit.doc_id}/content{suffix}",
            "original": f"/api/v1/documents/{hit.doc_id}/original{suffix}{joiner}inline=true",
        },
    }


def install(app, workspace, store, owner):
    metrics = workspace.search_metrics

    @app.post("/api/v1/search")
    def search(value: SearchRequest, request: Request):
        from docqa.docbase.retrieval import query

        generation = (
            store.get(value.conversation_id, owner(request))["generation_id"]
            if value.conversation_id
            else None
        )
        started = time.perf_counter()
        try:
            result = query(
                workspace.options.index,
                value.question,
                options=workspace.options.agent.retrieval,
                generation_id=generation,
                categories=value.categories,
                doc_ids=value.doc_ids,
                top_k=min(value.top_k, workspace.options.agent.retrieval.fusion_top_n),
                filters=value.filters,
            )
        except Exception:
            metrics.add(round((time.perf_counter() - started) * 1000, 1), "failed", 0)
            raise
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        metrics.add(elapsed, result.status, len(result.hits))
        return {
            "schema_version": "docqa.web-search.v1",
            "query_id": result.query_id,
            "generation_id": result.generation_id,
            "status": result.status,
            "reason": result.reason,
            "elapsed_ms": elapsed,
            "synthetic": result.synthetic,
            "applied_filter": result.applied_filter,
            "routes": {k: v.model_dump(mode="json") for k, v in result.routes.items()},
            "candidate_count": result.candidate_count,
            # Product rule: the Web never shows coverage-gap notices; quality records remain.
            "warnings": [w for w in result.warnings if w != COVERAGE_WARNING],
            "hits": [project_hit(hit, value.conversation_id) for hit in result.hits],
            "notice": "检索候选片段，不是已验证答案；请打开原文核对。",
        }
