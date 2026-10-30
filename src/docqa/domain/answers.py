"""Knowledge-consumption contracts: source-owned evidence and checkable claims."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from docqa.domain.documents import Contract, SourceLocator
from docqa.domain.rag import SourceSpan

OutputFormat = Literal["markdown", "table", "mermaid"]
AnswerStatus = Literal[
    "answered", "partial", "insufficient_evidence", "conflicting_evidence", "failed", "cancelled"
]


class QuestionTopic(Contract):
    topic_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    text: str = Field(min_length=1, max_length=2000)


class PlannedQuery(Contract):
    query: str = Field(min_length=1, max_length=4000)
    topic_ids: list[str] = Field(min_length=1, max_length=12)


class QueryPlan(Contract):
    intent: Literal["fact", "process", "comparison", "summary", "other"] = "fact"
    required_topics: list[QuestionTopic] = Field(min_length=1, max_length=12)
    exact_terms: list[str] = Field(default_factory=list, max_length=32)
    subqueries: list[PlannedQuery] = Field(min_length=1, max_length=2)
    output_format: OutputFormat = "markdown"

    @model_validator(mode="after")
    def check_topics(self):
        ids = [topic.topic_id for topic in self.required_topics]
        if len(ids) != len(set(ids)) or any(
            not set(query.topic_ids).issubset(ids) for query in self.subqueries
        ):
            raise ValueError("Plan has duplicate/unknown topic IDs")
        return self


class CoverageItem(Contract):
    topic_id: str
    state: Literal["supported", "partial", "missing", "conflicting"]
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=2000)


class EvidenceConflict(Contract):
    conflict_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    topic_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=2, max_length=20)
    description: str = Field(min_length=1, max_length=4000)


class Assessment(Contract):
    coverage: list[CoverageItem] = Field(min_length=1, max_length=12)
    followup_queries: list[PlannedQuery] = Field(default_factory=list, max_length=2)
    conflicts: list[EvidenceConflict] = Field(default_factory=list, max_length=20)


class EvidenceItem(Contract):
    evidence_id: str = ""
    generation_id: str
    chunk_id: str
    doc_id: str
    revision_id: str
    artifact_id: str
    source_name: str
    category: str
    source_span: SourceSpan
    text: str = Field(min_length=1)
    text_sha256: str
    origin: Literal["native", "ocr", "vl_generated"]
    figure_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    coverage_warnings: list[str] = Field(default_factory=list)
    is_heading: bool = False
    source_ref: str = ""

    @model_validator(mode="after")
    def check_source(self):
        if sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("Evidence text digest differs")
        if self.origin != self.source_span.origin:
            raise ValueError("Evidence origin differs from source span")
        return self


class EvidencePack(Contract):
    schema_version: Literal["docqa.evidence-pack.v1"] = "docqa.evidence-pack.v1"
    session_id: str
    generation_id: str
    query_id: str
    round_no: int = Field(ge=1)
    items: list[EvidenceItem]
    complete: bool
    omitted_count: int = Field(ge=0)
    payload_utf8_bytes: int = Field(ge=0)
    budget_utf8_bytes: int = Field(ge=0)
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)


class HardFilters(Contract):
    categories: list[str] | None = None
    doc_ids: list[str] | None = None


class EvidenceSession(Contract):
    schema_version: Literal["docqa.evidence-session.v1"] = "docqa.evidence-session.v1"
    session_id: str
    index_path: str
    generation_id: str
    question: str
    caller: str
    hard_filters: HardFilters
    required_topics: list[QuestionTopic] = Field(default_factory=list, max_length=12)
    created_at: str
    expires_at: str
    state: Literal["open", "closed", "expired"] = "open"
    synthetic: bool = False
    max_queries: int = Field(ge=1)
    max_rounds: int = Field(ge=1)
    max_finalize_attempts: int = Field(default=2, ge=1, le=2)
    max_evidence_bytes: int = Field(ge=1000)
    queries_used: int = Field(default=0, ge=0)
    finalize_attempts_used: int = Field(default=0, ge=0)
    next_evidence_no: int = Field(default=1, ge=1)


class DraftClaim(Contract):
    claim_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    topic_ids: list[str] = Field(min_length=1, max_length=12)
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    quotes: dict[str, str] = Field(default_factory=dict, max_length=20)
    quote_refs: dict[str, str] = Field(
        default_factory=dict,
        max_length=20,
        description="Built-in generation: evidence_id -> quote_id from quote_catalog. Leave quotes empty; the server copies the selected exact source. External submissions must provide resolved quotes and leave quote_refs empty.",
    )
    kind: Literal["fact", "quotation", "inference"] = "fact"


class DiagramEdge(Contract):
    edge_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    source_label: str = Field(min_length=1, max_length=120)
    target_label: str = Field(min_length=1, max_length=120)
    direction: Literal["directed", "undirected", "unknown"]
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class AnswerDraft(Contract):
    schema_version: Literal["docqa.answer-draft.v1"] = "docqa.answer-draft.v1"
    requested_format: OutputFormat = "markdown"
    required_topics: list[QuestionTopic] = Field(default_factory=list, max_length=12)
    claims: list[DraftClaim] = Field(default_factory=list, max_length=50)
    # Preview only: final text is rendered from checked claims, never trusted verbatim.
    answer_draft: str = Field(default="", max_length=16000)
    used_evidence_ids: list[str] = Field(default_factory=list, max_length=200)
    unanswered_topics: list[str] = Field(default_factory=list, max_length=12)
    conflicts: list[EvidenceConflict] = Field(default_factory=list, max_length=20)
    diagram_edges: list[DiagramEdge] = Field(default_factory=list, max_length=50)


class SupportDecision(Contract):
    item_id: str
    state: Literal["supported", "unsupported", "uncertain"]
    evidence_ids: list[str]
    reason: str = Field(default="", max_length=2000)


class SupportReport(Contract):
    schema_version: Literal["docqa.support-report.v1"] = "docqa.support-report.v1"
    claim_checks: list[SupportDecision]
    topic_checks: list[SupportDecision] = Field(default_factory=list)
    conflict_checks: list[SupportDecision] = Field(default_factory=list)
    relation_checks: list[SupportDecision] = Field(default_factory=list)


class Citation(Contract):
    citation_id: str
    claim_ids: list[str]
    evidence_id: str
    generation_id: str
    chunk_id: str
    doc_id: str
    revision_id: str
    artifact_id: str
    source_name: str
    locators: list[SourceLocator]
    source_span: SourceSpan
    quote: str
    origin: Literal["native", "ocr", "vl_generated"]
    figure_ids: list[str] = Field(default_factory=list)
    source_ref: str
    markdown_path: str = ""
    snapshot_path: str = ""
    image_paths: list[str] = Field(default_factory=list)


class AnswerResult(Contract):
    schema_version: Literal["docqa.answer.v1"] = "docqa.answer.v1"
    answer_id: str
    session_id: str
    generation_id: str
    question: str
    status: AnswerStatus
    requested_format: OutputFormat
    claims: list[DraftClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    unanswered_topics: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    diagram_edges: list[DiagramEdge] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    support_report: SupportReport | None = None
    verification: Literal["semantic_checked", "extractive_demo", "not_applicable"]
    verified: bool
    synthetic: bool
    production_ready: bool
    degraded: bool = False
    rounds_used: int = Field(ge=0)
    queries_used: int = Field(ge=0)
    stop_reason: str
    warnings: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    answer_markdown: str = ""

    @model_validator(mode="after")
    def check_verification_labels(self):
        if self.synthetic and (self.verified or self.production_ready):
            raise ValueError("Synthetic answers cannot claim semantic or production verification")
        if self.verified and (
            self.verification != "semantic_checked" or self.support_report is None
        ):
            raise ValueError("Verified answer requires semantic support checks")
        if self.production_ready and (
            not self.verified or self.degraded or self.status != "answered"
        ):
            raise ValueError(
                "Production label requires a complete, verified and non-degraded answer"
            )
        return self
