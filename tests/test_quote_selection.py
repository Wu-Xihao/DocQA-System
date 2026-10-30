"""Server-owned source selections avoid model copy errors without relaxing verification."""

import json
from types import SimpleNamespace

import httpx
import pytest
import test_agent_integration as shared
from test_agent_performance import source_items

from docqa.agent.citations import validate_draft
from docqa.agent.config import ChatServiceOptions
from docqa.agent.pipeline import answer_question
from docqa.agent.providers import AssessedDraft, DemoChatClient, HttpChatClient
from docqa.agent.quotes import bind_quote_selections, quote_catalog
from docqa.agent.sessions import SessionStore
from docqa.domain.answers import AnswerDraft, DraftClaim, SupportDecision, SupportReport
from docqa.domain.errors import DocQAError


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def evidence(corpus):
    return [
        item.model_copy(update={"evidence_id": f"E{i:06d}"})
        for i, item in enumerate(source_items(corpus), 1)
    ]


def test_catalog_has_exact_original_spans_and_content_bound_source_ids():
    text = "First line: 10.0 ms; not 100 ms.\nSecond\tline\u00a0has  spaces.\n" * 40
    items = [SimpleNamespace(evidence_id="E000001", text=text)]
    catalog = quote_catalog(items)
    assert len(catalog["E000001"]) > 1
    assert all(row["text"] in text and len(row["text"]) <= 600 for row in catalog["E000001"])
    assert quote_catalog(items) == catalog
    assert quote_catalog([{"evidence_id": "E000001", "text": text}]) == catalog
    assert (
        quote_catalog([SimpleNamespace(evidence_id="E000002", text=text)])["E000002"][0]["quote_id"]
        != catalog["E000001"][0]["quote_id"]
    )
    assert (
        quote_catalog([SimpleNamespace(evidence_id="E000001", text=text.replace("10.0", "10.1"))])[
            "E000001"
        ][0]["quote_id"]
        != catalog["E000001"][0]["quote_id"]
    )


def selected_draft(corpus):
    items = evidence(corpus)
    item = next(item for item in items if not item.is_heading)
    choice = quote_catalog(items)[item.evidence_id][0]
    draft = AnswerDraft(
        claims=[
            DraftClaim(
                claim_id="F1",
                topic_ids=["T01"],
                text="A synthetic paraphrase",
                kind="fact",
                evidence_ids=[item.evidence_id],
                quote_refs={item.evidence_id: choice["quote_id"]},
            )
        ],
        used_evidence_ids=[item.evidence_id],
    )
    return items, draft, choice


def test_binding_preserves_claim_and_resolves_exact_quote_then_external_validation_passes(corpus):
    items, draft, choice = selected_draft(corpus)
    with pytest.raises(DocQAError) as invalid:
        validate_draft(draft, items, ["T01"])
    assert invalid.value.rule == "quote_selection_unresolved"
    resolved, count = bind_quote_selections(draft, items)
    assert count == 1 and resolved.claims[0].quote_refs == {}
    assert resolved.claims[0].text == draft.claims[0].text
    assert resolved.claims[0].quotes == {draft.claims[0].evidence_ids[0]: choice["text"]}
    assert draft.claims[0].quotes == {}  # Input is not changed.
    validate_draft(resolved, items, ["T01"])


@pytest.mark.parametrize(
    "mistake,rule",
    [
        ("unknown", "quote_selection_unknown"),
        ("other_source", "quote_selection_unknown"),
        ("conflict", "quote_selection_conflict"),
        ("missing", "quote_selection_sources_mismatch"),
    ],
)
def test_bad_selections_are_not_silently_fixed(corpus, mistake, rule):
    items, draft, _ = selected_draft(corpus)
    claim = draft.claims[0]
    eid = claim.evidence_ids[0]
    if mistake == "unknown":
        claim.quote_refs[eid] = "Qnot-delivered"
    elif mistake == "other_source":
        other = next(item for item in items if item.evidence_id != eid)
        claim.quote_refs[eid] = quote_catalog([other])[other.evidence_id][0]["quote_id"]
    elif mistake == "conflict":
        claim.quotes[eid] = "This fabricated text must not be accepted."
    else:
        claim.quote_refs["unknown-evidence"] = claim.quote_refs.pop(eid)
    with pytest.raises(DocQAError) as invalid:
        bind_quote_selections(draft, items)
    assert invalid.value.rule == rule


def test_resolving_a_selector_cannot_turn_a_paraphrase_into_a_verbatim_quotation(corpus):
    items, draft, _ = selected_draft(corpus)
    draft.claims[0].kind = "quotation"
    resolved, _ = bind_quote_selections(draft, items)
    with pytest.raises(DocQAError) as invalid:
        validate_draft(resolved, items, ["T01"])
    assert invalid.value.rule == "quotation_text_mismatch"


@pytest.mark.parametrize(
    "compact,repair,supported",
    [(False, False, True), (True, False, True), (True, True, True), (True, False, False)],
)
def test_model_selects_sources_with_no_manual_copy_and_still_uses_independent_verifier(
    corpus, tmp_path, compact, repair, supported
):
    demo, tasks = DemoChatClient(), []

    def respond(request):
        task = json.loads(json.loads(request.content)["messages"][1]["content"])
        data = task["data"]
        tasks.append(task["task"])
        assert len(request.content) < 131072
        if task["task"] in {"assess_and_compose", "compose"}:
            assessment = demo.assess(data["question"], data["topics"], data["evidence"])
            draft = demo.compose(
                data["question"],
                data["topics"],
                data["evidence"],
                assessment,
                data["output_format"],
            )
            assert draft.claims
            for claim in draft.claims:
                claim.kind = "fact"
                if repair and task["task"] == "assess_and_compose":
                    claim.quotes = {
                        eid: "Fabricated unmatchable text." for eid in claim.evidence_ids
                    }
                else:
                    claim.quote_refs = {
                        eid: data["quote_catalog"][eid][0]["quote_id"] for eid in claim.evidence_ids
                    }
                    claim.quotes = {}
            if task["task"] == "compose":
                assert data["repair_feedback"]["validation_rule"] == (
                    "quote_not_exact" if repair else "unsupported_items"
                )
            result = (
                AssessedDraft(assessment=assessment, draft=draft)
                if task["task"] == "assess_and_compose"
                else draft
            )
        else:
            assert task["task"] == "verify"
            assert data["quote_support_rule"]
            for claim in data["draft"]["claims"]:
                assert not claim["quote_refs"] and claim["quotes"]
                for eid, quote in claim["quotes"].items():
                    original = next(
                        item["text"] for item in data["evidence"] if item["evidence_id"] == eid
                    )
                    assert quote in original
            # Explicit synthetic support decisions, not an actual semantic evaluation.
            used = data["draft"]["used_evidence_ids"]
            result = SupportReport(
                claim_checks=[
                    SupportDecision(
                        item_id=c["claim_id"],
                        state="supported" if supported else "unsupported",
                        evidence_ids=c["evidence_ids"],
                    )
                    for c in data["draft"]["claims"]
                ],
                topic_checks=[
                    SupportDecision(
                        item_id=t["topic_id"],
                        state="supported" if supported else "uncertain",
                        evidence_ids=used if supported else [],
                    )
                    for t in data["draft"]["required_topics"]
                ],
                conflict_checks=[],
                relation_checks=[],
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": result.model_dump_json()},
                    }
                ]
            },
        )

    client = HttpChatClient(
        ChatServiceOptions(
            endpoint="https://synthetic.invalid/chat",
            model="mock",
            revision="1",
            retries=0,
            compact_evidence=compact,
        ),
        transport=httpx.MockTransport(respond),
    )
    options = corpus[1].model_copy(update={"combine_assessment_and_compose": True})
    answer = answer_question(
        corpus[0], "SD5993", output=tmp_path, options=options, chat_clients=(client, client)
    )
    if not supported:
        assert answer.status != "answered" and not answer.claims and not answer.citations
        assert tasks == ["assess_and_compose", "verify", "compose", "verify"]
        return
    assert answer.status == "answered", answer.stop_reason
    assert answer.citations
    citation = answer.citations[0]
    source = SessionStore(tmp_path).resolve_citation(answer.answer_id, citation.citation_id)
    assert source["quote"] == citation.quote
    assert tasks == (
        ["assess_and_compose", "compose", "verify"] if repair else ["assess_and_compose", "verify"]
    )
    assert any(event["stage"] == "quote_selections_resolved" for event in answer.trace)
    assert not any(claim.quote_refs for claim in answer.claims)


def test_old_manual_quotes_remain_strict(corpus):
    items, draft, _ = selected_draft(corpus)
    draft.claims[0].quote_refs = {}
    draft.claims[0].quotes = {draft.claims[0].evidence_ids[0]: "synthetic unknown 99.9"}
    resolved, count = bind_quote_selections(draft, items)
    assert count == 0
    with pytest.raises(DocQAError) as invalid:
        validate_draft(resolved, items, ["T01"])
    assert invalid.value.code == "QUOTE_NOT_FOUND"
