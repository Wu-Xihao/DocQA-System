"""Structural gates and system-owned references; semantic support is a separate step."""

from __future__ import annotations

from pydantic import ValidationError

from docqa.domain.answers import AnswerDraft, Citation, SupportReport
from docqa.domain.errors import DocQAError


class DraftValidationError(DocQAError):
    """Stable rule identifiers for repair/diagnostics; never embed source text."""

    def __init__(self, code, message, rule):
        super().__init__(code, message)
        self.rule = rule


def validate_draft(draft: AnswerDraft, items, topics=None) -> None:
    allowed = {item.evidence_id: item for item in items}
    topic_ids = None if topics is None else {getattr(topic, "topic_id", topic) for topic in topics}
    ids = (
        [claim.claim_id for claim in draft.claims]
        + [value.conflict_id for value in draft.conflicts]
        + [value.edge_id for value in draft.diagram_edges]
    )
    if len(ids) != len(set(ids)):
        raise DocQAError("ANSWER_INVALID", "Claim, conflict and edge identifiers must be unique")
    used = set()
    for claim in draft.claims:
        if claim.quote_refs:
            raise DraftValidationError(
                "CITATION_INVALID",
                "External drafts require resolved exact quotes",
                "quote_selection_unresolved",
            )
        if len(claim.evidence_ids) != len(set(claim.evidence_ids)) or set(claim.quotes) != set(
            claim.evidence_ids
        ):
            raise DraftValidationError(
                "CITATION_INVALID",
                "Every claim source requires exactly one explicit quote",
                "claim_sources_and_quotes_mismatch",
            )
        if topic_ids is not None and not set(claim.topic_ids).issubset(topic_ids):
            raise DocQAError("ANSWER_INVALID", "Claim introduces unknown required topics")
        for eid, quote in claim.quotes.items():
            if eid not in allowed:
                raise DraftValidationError(
                    "CITATION_INVALID",
                    "Evidence ID was not delivered in this session",
                    "evidence_not_delivered",
                )
            if not quote.strip() or quote not in allowed[eid].text:
                raise DraftValidationError(
                    "QUOTE_NOT_FOUND",
                    "Quote is not an exact substring of delivered evidence",
                    "quote_not_exact",
                )
        if claim.kind == "quotation" and (
            len(claim.quotes) != 1 or claim.text != next(iter(claim.quotes.values()))
        ):
            raise DraftValidationError(
                "QUOTE_NOT_FOUND",
                "Quotation claim text must equal its quoted original excerpt",
                "quotation_text_mismatch",
            )
        used.update(claim.evidence_ids)
    for conflict in draft.conflicts:
        if len(set(conflict.evidence_ids)) < 2 or not set(conflict.evidence_ids).issubset(allowed):
            raise DraftValidationError(
                "CITATION_INVALID",
                "Conflict requires two distinct delivered sources",
                "conflict_sources_invalid",
            )
        if topic_ids is not None and not set(conflict.topic_ids).issubset(topic_ids):
            raise DocQAError("ANSWER_INVALID", "Conflict introduces an unknown topic")
        used.update(conflict.evidence_ids)
    claim_map = {claim.claim_id: claim for claim in draft.claims}
    for edge in draft.diagram_edges:
        if not set(edge.claim_ids).issubset(claim_map) or not set(edge.evidence_ids).issubset(
            allowed
        ):
            raise DraftValidationError(
                "CITATION_INVALID",
                "Diagram must use checked claims and delivered evidence",
                "diagram_reference_invalid",
            )
        premises = {eid for cid in edge.claim_ids for eid in claim_map[cid].evidence_ids}
        if not set(edge.evidence_ids).issubset(premises):
            raise DraftValidationError(
                "CITATION_INVALID",
                "Diagram sources differ from its supporting claims",
                "diagram_premises_mismatch",
            )
        used.update(edge.evidence_ids)
    if len(draft.used_evidence_ids) != len(set(draft.used_evidence_ids)) or used != set(
        draft.used_evidence_ids
    ):
        raise DraftValidationError(
            "CITATION_INVALID",
            "Used evidence IDs must exactly match claims, conflicts and relations",
            "used_evidence_summary_mismatch",
        )
    if topic_ids is not None and not set(draft.unanswered_topics).issubset(topic_ids):
        raise DocQAError("ANSWER_INVALID", "Unknown unanswered topic")


def validate_builtin_draft(draft: AnswerDraft, items, topics=None):
    """Reconcile only a redundant model summary after ALL actual references pass.

    External submissions still use validate_draft directly and remain strict.
    Claims, evidence IDs, quotes, conflicts and relations are never changed.
    """
    try:
        validate_draft(draft, items, topics)
        return draft, False
    except DraftValidationError as exc:
        if exc.rule != "used_evidence_summary_mismatch":
            raise
    actual = {
        eid
        for item in [*draft.claims, *draft.conflicts, *draft.diagram_edges]
        for eid in item.evidence_ids
    }
    try:
        corrected = AnswerDraft.model_validate(
            {**draft.model_dump(), "used_evidence_ids": sorted(actual)}
        )
    except ValidationError as exc:
        raise DraftValidationError(
            "CITATION_INVALID",
            "Derived citation summary exceeds the contract",
            "used_evidence_summary_overflow",
        ) from exc
    validate_draft(corrected, items, topics)
    return corrected, True


def validate_support_report(draft: AnswerDraft, report: SupportReport) -> None:
    topics = {topic.topic_id for topic in draft.required_topics}
    if (
        len(report.topic_checks) != len(topics)
        or {check.item_id for check in report.topic_checks} != topics
    ):
        raise DocQAError(
            "SUPPORT_REPORT_INVALID", "Verifier must check completeness of every required topic"
        )
    permitted = set(draft.used_evidence_ids)
    for check in report.topic_checks:
        if not set(check.evidence_ids).issubset(permitted) or (
            check.state == "supported" and not check.evidence_ids
        ):
            raise DocQAError(
                "SUPPORT_REPORT_INVALID", "Topic completeness uses unavailable support"
            )
    for source, checks, field in (
        (draft.claims, report.claim_checks, "claim_id"),
        (draft.conflicts, report.conflict_checks, "conflict_id"),
        (draft.diagram_edges, report.relation_checks, "edge_id"),
    ):
        expected = {getattr(item, field): item for item in source}
        if len(checks) != len(expected) or {check.item_id for check in checks} != set(expected):
            raise DocQAError(
                "SUPPORT_REPORT_INVALID", "Verifier must check every item exactly once"
            )
        if len({check.item_id for check in checks}) != len(checks):
            raise DocQAError("SUPPORT_REPORT_INVALID", "Verifier returned duplicate item checks")
        for check in checks:
            if set(check.evidence_ids) != set(expected[check.item_id].evidence_ids):
                raise DocQAError(
                    "SUPPORT_REPORT_INVALID", "Verifier changed an item's cited sources"
                )


def retain_supported(draft: AnswerDraft, report: SupportReport) -> AnswerDraft:
    """Remove unverified statements, including relations with removed premises."""
    good_claims = {item.item_id for item in report.claim_checks if item.state == "supported"}
    good_conflicts = {item.item_id for item in report.conflict_checks if item.state == "supported"}
    good_edges = {item.item_id for item in report.relation_checks if item.state == "supported"}
    unresolved_conflicts = {
        topic
        for value in draft.conflicts
        if value.conflict_id not in good_conflicts
        for topic in value.topic_ids
    }
    claims = [
        claim
        for claim in draft.claims
        if claim.claim_id in good_claims and not unresolved_conflicts.intersection(claim.topic_ids)
    ]
    good_claims = {claim.claim_id for claim in claims}
    conflicts = [value for value in draft.conflicts if value.conflict_id in good_conflicts]
    edges = [
        edge
        for edge in draft.diagram_edges
        if edge.edge_id in good_edges and set(edge.claim_ids).issubset(good_claims)
    ]
    used = {eid for value in [*claims, *conflicts, *edges] for eid in value.evidence_ids}
    return draft.model_copy(
        update={
            "claims": claims,
            "conflicts": conflicts,
            "diagram_edges": edges,
            "used_evidence_ids": sorted(used),
            "answer_draft": "",
        }
    )


def build_citations(draft: AnswerDraft, items, answer_id: str) -> list[Citation]:
    allowed = {item.evidence_id: item for item in items}
    occurrences = []
    for claim in draft.claims:
        occurrences.extend((eid, quote, claim.claim_id) for eid, quote in claim.quotes.items())
    for conflict in draft.conflicts:
        occurrences.extend(
            (eid, allowed[eid].text, conflict.conflict_id) for eid in conflict.evidence_ids
        )
    merged = {}
    for eid, quote, claim_id in occurrences:
        merged.setdefault((eid, quote), []).append(claim_id)
    citations = []
    for (eid, quote), claim_ids in merged.items():
        evidence = allowed[eid]
        identifier = f"C{len(citations) + 1:04d}"
        citations.append(
            Citation(
                citation_id=identifier,
                claim_ids=list(dict.fromkeys(claim_ids)),
                evidence_id=eid,
                **{
                    field: getattr(evidence, field)
                    for field in (
                        "generation_id",
                        "chunk_id",
                        "doc_id",
                        "revision_id",
                        "artifact_id",
                        "source_name",
                        "source_span",
                        "origin",
                        "figure_ids",
                    )
                },
                locators=evidence.source_span.locators,
                quote=quote,
                source_ref=f"docqa://answers/{answer_id}/citations/{identifier}",
            )
        )
    return citations
