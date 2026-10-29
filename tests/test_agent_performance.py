"""Optimization preserves source text, hard budgets and independent verification."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_agent_integration as shared

from docqa.agent.config import ChatServiceOptions
from docqa.agent.evidence import build_evidence_items, json_bytes
from docqa.agent.performance import PhaseMetrics, balanced_evidence
from docqa.agent.pipeline import answer_question, create_evidence_session
from docqa.agent.providers import DemoChatClient, HttpChatClient
from docqa.agent.sessions import SessionStore
from docqa.docbase.analyzer import Analyzer
from docqa.docbase.retrieval import query
from docqa.domain.answers import Assessment, CoverageItem, PlannedQuery
from docqa.domain.documents import Contract


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def source_items(corpus):
    index, options, built = corpus
    hits = query(index, "SD5993 心跳", options=options.retrieval).hits
    return build_evidence_items(index, built.generation_id, hits)


def test_no_new_evidence_reuses_assessment_and_keeps_verifier(corpus, tmp_path):
    class Missing(DemoChatClient):
        calls = 0

        def assess(self, question, topics, evidence, *, deadline=None):
            self.calls += 1
            return Assessment(
                coverage=[CoverageItem(topic_id="T01", state="missing")],
                followup_queries=[PlannedQuery(query="SD5993 心跳再次查询", topic_ids=["T01"])],
            )

    generator, verifier = Missing(), DemoChatClient()
    result = answer_question(
        corpus[0], "SD5993", options=corpus[1], output=tmp_path, chat_clients=(generator, verifier)
    )
    assert result.stop_reason == "no_new_evidence"
    assert generator.calls == 1
    performance = next(e for e in result.trace if e["stage"] == "performance")
    assert performance["model_tasks"] == 3
    assert "chat.verify" in performance["operations"]
    assert any(e["stage"] == "assess_reused" for e in result.trace)
    assert performance["retrieval_rounds"][-1]["reranker_state"] == "cached"
    assert all(c["seconds"] >= 0 for c in performance["calls"])


def test_balanced_order_preserves_spans_and_represents_each_chunk():
    def item(chunk, text, heading=False):
        return SimpleNamespace(chunk_id=chunk, text=text, is_heading=heading)

    a1, a2, a3 = (
        item("A", "ordinary text"),
        item("A", "SD5993 detail"),
        item("A", "other section"),
    )
    b, heading = item("B", "SD5993 recovery"), item("A", "SD5993", True)
    items = [heading, a1, a2, a3, b]
    ranked = balanced_evidence(items, "SD5993", Analyzer())
    assert ranked[:2] == [a2, b]
    assert ranked[-1] is heading
    assert {id(x) for x in ranked} == {id(x) for x in items}


def test_delivery_reserves_budget_for_real_followup_evidence(corpus, tmp_path):
    items = source_items(corpus)
    maximum = len(json_bytes(items)) + 5000
    options = corpus[1].model_copy(update={"max_evidence_bytes": maximum})
    session = create_evidence_session(corpus[0], "SD5993", options=options, output=tmp_path)
    store = SessionStore(tmp_path)
    reservation = store.reserve_query(session.session_id, caller="local-codeagent", round_no=1)
    first = store.deliver(
        session.session_id,
        reservation["reservation_id"],
        items,
        caller="local-codeagent",
        query_id=uuid4().hex,
        delivery_budget_bytes=len(json_bytes(items[0])) + 1100,
    )
    assert 0 < len(first.items) < len(items)
    second_reservation = store.reserve_query(
        session.session_id, caller="local-codeagent", round_no=2
    )
    second = store.deliver(
        session.session_id,
        second_reservation["reservation_id"],
        items,
        caller="local-codeagent",
        query_id=uuid4().hex,
    )
    assert second.items
    assert len(json_bytes(store.items(session.session_id, caller="local-codeagent"))) <= maximum
    delivered = first.items + second.items
    assert all(
        i.text
        == next(s.text for s in items if s.source_span == i.source_span and s.doc_id == i.doc_id)
        for i in delivered
    )


def test_compact_http_evidence_keeps_text_warnings_and_source_identity(corpus):
    items = source_items(corpus)
    captured = []

    class Response(Contract):
        ok: bool

    def transport(request):
        captured.append(
            json.loads(json.loads(request.content)["messages"][1]["content"])["data"]["evidence"]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                    }
                ]
            },
        )

    config = ChatServiceOptions(
        endpoint="https://mock.invalid/chat/completions",
        model="test",
        revision="1",
        retries=0,
        compact_evidence=True,
    )
    client = HttpChatClient(config, transport=httpx.MockTransport(transport))
    client._request("assess", {"evidence": items}, Response, None)
    projected = captured[0]
    for original, compact in zip(items, projected, strict=True):
        assert compact["text"] == original.text
        assert compact["coverage_warnings"] == original.coverage_warnings
        assert compact["origin"] == original.origin and compact["doc_id"] == original.doc_id
        assert "source_span" not in compact and "text_sha256" not in compact
    assert len(json_bytes(projected)) < len(json_bytes(items)) * 0.75


def test_failed_operation_is_timed_without_recording_arguments():
    metrics = PhaseMetrics()

    def failed(secret):
        raise ValueError("test failure")

    with pytest.raises(ValueError):
        metrics.call("test", failed, "must-not-be-logged")
    summary = metrics.summary()
    assert summary["calls"][0]["succeeded"] is False
    assert "must-not-be-logged" not in json.dumps(summary)
