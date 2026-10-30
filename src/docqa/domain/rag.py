"""Versioned DocBase contracts; byte budgets are not claimed as tokenizer counts."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from docqa.domain.documents import Contract, SourceLocator


class ChunkOptions(Contract):
    target_utf8_bytes: int = Field(default=2400, ge=64, le=100000)
    max_utf8_bytes: int = Field(default=3600, ge=64, le=200000)
    overlap_utf8_bytes: int = Field(default=320, ge=0, le=50000)

    @model_validator(mode="after")
    def check_budget(self):
        if self.target_utf8_bytes > self.max_utf8_bytes:
            raise ValueError("Chunk target exceeds maximum")
        if self.overlap_utf8_bytes >= self.target_utf8_bytes:
            raise ValueError("Overlap must be smaller than target")
        return self


class SourceSpan(Contract):
    block_id: str
    component: Literal["block_text", "table_row", "figure_ocr", "figure_vl"] = "block_text"
    text_start_codepoint: int = Field(ge=0)
    text_end_codepoint: int = Field(ge=0)
    source_text_sha256: str
    markdown_start_byte: int = Field(ge=0)
    markdown_end_byte: int = Field(ge=0)
    locators: list[SourceLocator] = Field(min_length=1)
    origin: Literal["native", "ocr", "vl_generated"] = "native"
    figure_id: str | None = None
    table_row_start: int | None = Field(default=None, ge=0)
    table_row_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_intervals(self):
        if self.text_start_codepoint >= self.text_end_codepoint:
            raise ValueError("Source text interval must be nonempty")
        if self.markdown_start_byte >= self.markdown_end_byte:
            raise ValueError("Markdown interval must be nonempty")
        if self.table_row_start is not None and (
            self.table_row_end is None or self.table_row_start >= self.table_row_end
        ):
            raise ValueError("Table row interval must be nonempty")
        return self


class GeneratedContext(Contract):
    kind: Literal["section", "table_header", "figure_caption"]
    text: str
    source_block_ids: list[str] = Field(default_factory=list)


class Chunk(Contract):
    schema_version: Literal["docqa.chunk.v1"] = "docqa.chunk.v1"
    chunk_id: str = ""
    chunk_profile_id: str
    doc_id: str
    revision_id: str
    artifact_id: str
    source_name: str
    category: str
    section: list[str] = Field(default_factory=list)
    display_text: str = Field(min_length=1)
    index_text: str = Field(min_length=1)
    index_text_sha256: str
    index_utf8_bytes: int = Field(ge=1)
    source_spans: list[SourceSpan] = Field(min_length=1)
    figure_ids: list[str] = Field(default_factory=list)
    generated_context: list[GeneratedContext] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_text(self):
        raw = self.index_text.encode("utf-8")
        if self.index_utf8_bytes != len(raw) or self.index_text_sha256 != sha256(raw).hexdigest():
            raise ValueError("Index text digest or byte size is inconsistent")
        return self


def chunk_identity(chunk: Chunk | dict) -> str:
    value = chunk.model_dump(mode="json") if isinstance(chunk, Chunk) else dict(chunk)
    value.pop("chunk_id", None)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


class RouteResult(Contract):
    state: Literal["ok", "empty", "failed"]
    count: int = Field(default=0, ge=0)
    error_code: str | None = None


class RetrievalHit(Contract):
    chunk_id: str
    generation_id: str
    doc_id: str
    artifact_id: str
    source_name: str
    category: str
    display_text: str
    index_text: str
    section: list[str]
    source_spans: list[SourceSpan]
    figure_ids: list[str]
    route_ranks: dict[str, int]
    route_scores: dict[str, float]
    rrf_score: float
    rerank_score: float | None = None
    final_rank: int = Field(ge=1)


class QueryResult(Contract):
    schema_version: Literal["docqa.retrieval.v1"] = "docqa.retrieval.v1"
    query_id: str
    generation_id: str
    question: str
    status: Literal["ok", "empty", "degraded"]
    reason: str | None = None
    synthetic: bool
    production_ready: bool
    retrieval_mode: str
    retrieval_profile_id: str
    applied_filter: dict[str, JsonValue]
    hits: list[RetrievalHit]
    routes: dict[str, RouteResult]
    candidate_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class BuildResult(Contract):
    schema_version: Literal["docqa.docbase-build.v1"] = "docqa.docbase-build.v1"
    generation_id: str
    generation_dir: str
    build_key: str
    publish_sequence: int
    reused: bool
    synthetic: bool
    production_ready: bool
    documents: int
    chunks: int
    cache_hits: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
