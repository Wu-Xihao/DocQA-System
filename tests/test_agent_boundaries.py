"""Deadline, evidence budgets, hostile previews and support coverage boundaries."""

import time

import pytest
import test_agent_integration as shared

from docqa.agent.evidence import json_bytes
from docqa.agent.pipeline import (
    answer_question,
    create_evidence_session,
    finalize_answer,
    retrieve_in_session,
)
from docqa.agent.planning import conservative_plan
from docqa.agent.providers import DemoChatClient
from docqa.agent.rendering import render_answer
from docqa.agent.sessions import SessionStore, bounded_filters
from docqa.domain.answers import (
    AnswerDraft,
    DiagramEdge,
    DraftClaim,
    EvidenceConflict,
    HardFilters,
    SupportDecision,
)
from docqa.domain.errors import DocQAError


@pytest.fixture(scope="module", name="corpus")
def shared_corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_quote_existence_does_not_substitute_for_support(corpus, tmp_path):
    index, options, _ = corpus

    class Unsupported(DemoChatClient):
        def verify(self, question, draft, evidence, *, deadline=None):
            report = super().verify(question, draft, evidence, deadline=deadline)
            return report.model_copy(
                update={
                    "claim_checks": [
                        value.model_copy(update={"state": "unsupported"})
                        for value in report.claim_checks
                    ]
                }
            )

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=options,
        chat_clients=(DemoChatClient(), Unsupported(role="verifier")),
    )
    assert result.status == "insufficient_evidence" and not result.claims
    assert "UNSUPPORTED_ITEMS_REMOVED" in result.warnings


def test_finalization_freezes_queries_before_verifier(corpus, tmp_path):
    index, options, _ = corpus
    session = create_evidence_session(index, "SD5993", output=tmp_path, options=options)
    retrieve_in_session(session.session_id, "SD5993", output=tmp_path, options=options)
    store = SessionStore(tmp_path)
    store.reserve_finalize(session.session_id, caller="local-codeagent")
    with pytest.raises(DocQAError) as error:
        store.reserve_query(session.session_id, caller="local-codeagent")
    assert error.value.code == "SESSION_FINALIZING"


def test_source_links_across_windows_drives(corpus, tmp_path, monkeypatch):
    index, options, _ = corpus
    result = answer_question(index, "SD5993", output=tmp_path, options=options)

    def different_drives(*args):
        raise ValueError("path is on another drive")

    monkeypatch.setattr("docqa.agent.rendering.os.path.relpath", different_drives)
    text = render_answer(result, answer_dir=tmp_path)
    assert "[Markdown 原文](file:///" in text


def test_composer_cannot_hide_explicit_assessment_conflict(corpus, tmp_path):
    index, options, _ = corpus

    class HiddenConflict(DemoChatClient):
        def assess(self, question, topics, evidence, *, deadline=None):
            assessment = super().assess(question, topics, evidence, deadline=deadline)
            ids = [item.evidence_id for item in evidence if not item.is_heading][:2]
            return assessment.model_copy(
                update={
                    "conflicts": [
                        EvidenceConflict(
                            conflict_id="X1",
                            topic_ids=["T01"],
                            evidence_ids=ids,
                            description="Explicit observed conflict even though coverage says supported",
                        )
                    ]
                }
            )

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=options,
        chat_clients=(HiddenConflict(), DemoChatClient(role="verifier")),
    )
    assert result.status == "failed" and result.stop_reason == "ANSWER_INVALID"
    assert not result.claims and not result.citations


@pytest.mark.parametrize("state", ["failed", "reserved", "degraded", "allowed_degraded"])
def test_external_finalization_retains_query_diagnostics(corpus, tmp_path, state):
    index, options, _ = corpus
    session = create_evidence_session(index, "SD5993", output=tmp_path, options=options)
    pack = retrieve_in_session(session.session_id, "SD5993", output=tmp_path, options=options)
    item = next(value for value in pack.items if "SD5993" in value.text and not value.is_heading)
    draft = AnswerDraft(
        claims=[
            DraftClaim(
                claim_id="F1",
                topic_ids=["T01"],
                text=item.text,
                kind="quotation",
                evidence_ids=[item.evidence_id],
                quotes={item.evidence_id: item.text},
            )
        ],
        used_evidence_ids=[item.evidence_id],
    )
    store = SessionStore(tmp_path)
    reservation = store.reserve_query(session.session_id, caller="local-codeagent", round_no=2)
    arguments = (session.session_id, reservation["reservation_id"])
    if state == "failed":
        store.query_failed(*arguments, caller="local-codeagent", error_code="MODEL_TIMEOUT")
    elif "degraded" in state:
        store.deliver(
            *arguments,
            [],
            caller="local-codeagent",
            query_id="degraded-query",
            degraded=True,
            warnings=["RERANKER_UNAVAILABLE"],
        )
    if state == "allowed_degraded":
        options = options.model_copy(
            update={
                "allow_degraded": True,
                "retrieval": options.retrieval.model_copy(update={"allow_degraded": True}),
            }
        )
        result = finalize_answer(session.session_id, draft, output=tmp_path, options=options)
        assert result.degraded and result.rounds_used == 2 and not result.production_ready
        assert "RERANKER_UNAVAILABLE" in result.warnings
    else:
        with pytest.raises(DocQAError) as error:
            finalize_answer(session.session_id, draft, output=tmp_path, options=options)
        assert (
            error.value.code
            == {
                "failed": "SESSION_RETRIEVAL_FAILED",
                "reserved": "SESSION_QUERY_PENDING",
                "degraded": "DEGRADED_NOT_ALLOWED",
            }[state]
        )


def test_true_fact_cannot_establish_topic_completeness(corpus, tmp_path):
    index, options, _ = corpus

    class Incomplete(DemoChatClient):
        def verify(self, question, draft, evidence, *, deadline=None):
            report = super().verify(question, draft, evidence, deadline=deadline)
            return report.model_copy(
                update={
                    "topic_checks": [
                        check.model_copy(update={"state": "uncertain"})
                        for check in report.topic_checks
                    ]
                }
            )

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=options,
        chat_clients=(DemoChatClient(), Incomplete(role="verifier")),
    )
    assert result.status == "partial" and result.claims and result.unanswered_topics == ["T01"]


def test_model_task_budget_is_enforced(corpus, tmp_path):
    index, options, _ = corpus
    result = answer_question(
        index, "SD5993", output=tmp_path, options=options.model_copy(update={"max_model_calls": 1})
    )
    assert result.status == "failed" and result.stop_reason == "AGENT_MODEL_BUDGET_EXCEEDED"


def test_late_result_is_never_published_as_answer(corpus, tmp_path):
    index, options, _ = corpus

    class Slow(DemoChatClient):
        def assess(self, question, topics, evidence, *, deadline=None):
            time.sleep(0.02)
            return super().assess(question, topics, evidence)

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=options.model_copy(update={"deadline_seconds": 0.01}),
        chat_clients=(Slow(), DemoChatClient(role="verifier")),
    )
    assert result.status == "failed" and result.stop_reason == "AGENT_DEADLINE_EXCEEDED"


def test_evidence_bytes_are_cumulative(corpus, tmp_path):
    index, options, _ = corpus
    options = options.model_copy(update={"max_evidence_bytes": 3500})
    session = create_evidence_session(index, "SD5993", output=tmp_path, options=options)
    first = retrieve_in_session(session.session_id, "SD5993", output=tmp_path, options=options)
    second = retrieve_in_session(session.session_id, "TLS", output=tmp_path, options=options)
    items = SessionStore(tmp_path).items(session.session_id, caller="local-codeagent")
    assert len(json_bytes(items)) <= options.max_evidence_bytes
    assert second.omitted_count or not second.items
    assert len({item.evidence_id for item in items}) == len(items)
    assert first.payload_utf8_bytes == len(json_bytes(first))


@pytest.mark.parametrize("upper,requested", [(["protocol"], ["security"]), ([], ["protocol"])])
def test_hard_scope_cannot_expand(upper, requested):
    with pytest.raises(DocQAError) as error:
        bounded_filters(HardFilters(categories=upper), HardFilters(categories=requested))
    assert error.value.code == "SESSION_FILTER_VIOLATION"


@pytest.mark.parametrize("invalid", [[], False, "", 0])
def test_false_values_are_not_an_unrestricted_scope(corpus, tmp_path, invalid):
    index, _, _ = corpus
    with pytest.raises(DocQAError) as error:
        SessionStore(tmp_path).create(index, "question", "caller", hard_filters=invalid)
    assert error.value.code == "INVALID_REQUEST"


def test_demo_plan_preserves_identifiers(corpus, tmp_path):
    index, options, _ = corpus
    result = answer_question(
        index, "SD5993 FE_HEART_Start", output=tmp_path, options=options, use_planner=True
    )
    assert result.status == "answered"
    assert (
        "FE_HEART_Start"
        in next(event for event in result.trace if event["stage"] == "plan")["exact_terms"]
    )


def test_preview_injection_is_not_rendered(corpus, tmp_path):
    index, options, _ = corpus

    class Preview(DemoChatClient):
        def compose(self, *args, **kwargs):
            return (
                super()
                .compose(*args, **kwargs)
                .model_copy(
                    update={"answer_draft": "UNSOURCED-FAKE-FACT <script>alert(1)</script>"}
                )
            )

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=options,
        chat_clients=(Preview(), DemoChatClient(role="verifier")),
    )
    assert result.status == "answered" and "UNSOURCED-FAKE-FACT" not in result.answer_markdown


@pytest.mark.parametrize("direction", ["directed", "undirected", "unknown"])
def test_checked_mermaid_directions_and_safe_labels(corpus, tmp_path, direction):
    index, options, _ = corpus
    result = answer_question(index, "SD5993", output=tmp_path, options=options)
    claim = result.claims[0]
    result.diagram_edges = [
        DiagramEdge(
            edge_id="R1",
            source_label='start"\nclick N1 javascript:evil()',
            target_label="end",
            direction=direction,
            claim_ids=[claim.claim_id],
            evidence_ids=claim.evidence_ids,
        )
    ]
    result.requested_format = "mermaid"
    result.support_report.relation_checks = [
        SupportDecision(item_id="R1", state="supported", evidence_ids=claim.evidence_ids)
    ]
    text = render_answer(result)
    assert "\nclick N1" not in text
    if direction == "unknown":
        assert "方向未知" in text and "```mermaid" not in text
    elif direction == "undirected":
        assert " N1 --- N2" in text and " N1 --> N2" not in text
    else:
        assert " N1 --> N2" in text
    result.support_report.relation_checks = []
    with pytest.raises(DocQAError):
        render_answer(result)


def test_finalization_cannot_change_original_topics(corpus, tmp_path):
    index, options, _ = corpus
    session = create_evidence_session(index, "SD5993", output=tmp_path, options=options)
    with pytest.raises(DocQAError) as error:
        finalize_answer(
            session.session_id,
            AnswerDraft(),
            output=tmp_path,
            options=options,
            topics=conservative_plan("unrelated").required_topics,
        )
    assert error.value.code == "ANSWER_INVALID"
