"""Citation repair preserves exact quotes, delivered IDs and independent support gates."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient
from test_agent_performance import source_items

from docqa.agent.citations import validate_builtin_draft, validate_draft
from docqa.agent.config import ChatServiceOptions
from docqa.agent.pipeline import answer_question
from docqa.agent.providers import AssessedDraft, DemoChatClient, HttpChatClient
from docqa.docbase.pipeline import build_index
from docqa.docbase.storage import load_generation
from docqa.domain.answers import AnswerDraft, DraftClaim
from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.config import WebOptions


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def sample_draft(corpus):
    items = source_items(corpus)
    items = [
        item.model_copy(update={"evidence_id": f"E{number:04d}"})
        for number, item in enumerate(items, 1)
    ]
    item = next(item for item in items if not item.is_heading and item.origin == "native")
    return items, AnswerDraft(
        claims=[
            DraftClaim(
                claim_id="F1",
                topic_ids=["T01"],
                text=item.text,
                evidence_ids=[item.evidence_id],
                quotes={item.evidence_id: item.text},
                kind="quotation",
            )
        ]
    )


@pytest.mark.parametrize("summary", [[], ["not-actually-used"], ["duplicate"]])
def test_only_derived_summary_is_reconciled_and_external_validation_stays_strict(corpus, summary):
    items, draft = sample_draft(corpus)
    eid = draft.claims[0].evidence_ids[0]
    draft.used_evidence_ids = [eid, eid] if summary == ["duplicate"] else summary
    with pytest.raises(DocQAError) as invalid:
        validate_draft(draft, items, ["T01"])
    assert invalid.value.code == "CITATION_INVALID"
    result, changed = validate_builtin_draft(draft, items, ["T01"])
    assert changed and result.used_evidence_ids == [eid]
    assert result.claims == draft.claims
    assert draft.used_evidence_ids == ([eid, eid] if summary == ["duplicate"] else summary)
    validate_draft(result, items, ["T01"])


@pytest.mark.parametrize(
    "mistake,rule",
    [
        ("unknown", "evidence_not_delivered"),
        ("shortened", "evidence_not_delivered"),
        ("quote", "quote_not_exact"),
        ("quote_keys", "claim_sources_and_quotes_mismatch"),
    ],
)
def test_summary_reconciliation_never_fixes_or_hides_invalid_actual_references(
    corpus, mistake, rule
):
    items, draft = sample_draft(corpus)
    claim = draft.claims[0]
    eid = claim.evidence_ids[0]
    if mistake in {"unknown", "shortened"}:
        key = "C0001" if mistake == "unknown" else "E1"
        claim.evidence_ids, claim.quotes = [key], {key: claim.text}
    elif mistake == "quote":
        claim.quotes = {eid: "synthetic fabricated quote that is absent"}
    else:
        claim.quotes = {}
    with pytest.raises(DocQAError) as invalid:
        validate_builtin_draft(draft, items, ["T01"])
    assert invalid.value.rule == rule


def test_builtin_summary_repair_still_calls_verifier_and_publishes_real_source_links(
    corpus, tmp_path
):
    class Generator(DemoChatClient):
        calls = 0

        def compose(self, *args, **kwargs):
            self.calls += 1
            return super().compose(*args, **kwargs).model_copy(update={"used_evidence_ids": []})

    class Verifier(DemoChatClient):
        calls = 0

        def verify(self, *args, **kwargs):
            self.calls += 1
            return super().verify(*args, **kwargs)

    generator, verifier = Generator(), Verifier()
    answer = answer_question(
        corpus[0], "SD5993", output=tmp_path, options=corpus[1], chat_clients=(generator, verifier)
    )
    assert answer.status == "answered" and answer.citations
    assert generator.calls == verifier.calls == 1
    assert any(event["stage"] == "citation_bookkeeping_reconciled" for event in answer.trace)
    assert all(Path(citation.snapshot_path).is_file() for citation in answer.citations)


@pytest.mark.parametrize("compact,persistent", [(False, False), (True, False), (True, True)])
def test_http_repair_receives_specific_rule_previous_draft_and_allowlist(
    corpus, tmp_path, compact, persistent
):
    demo, tasks = DemoChatClient(), []

    def respond(request):
        task = json.loads(json.loads(request.content)["messages"][1]["content"])
        data = task["data"]
        tasks.append(task["task"])
        if task["task"] == "assess_and_compose":
            assessment = demo.assess(data["question"], data["topics"], data["evidence"])
            draft = demo.compose(
                data["question"],
                data["topics"],
                data["evidence"],
                assessment,
                data["output_format"],
            )
            assert data["citation_contract"]["allowed_evidence_ids"]
            first = draft.claims[0]
            first.evidence_ids, first.quotes = ["C0001"], {"C0001": first.text}
            draft.used_evidence_ids = ["C0001"]
            result = AssessedDraft(assessment=assessment, draft=draft)
        elif task["task"] == "compose":
            feedback = data["repair_feedback"]
            assert data["repair"] is True
            assert feedback["validation_rule"] == "evidence_not_delivered"
            assert feedback["validation_code"] == "CITATION_INVALID"
            assert feedback["previous_draft"]["claims"][0]["evidence_ids"] == ["C0001"]
            allowed = data["citation_contract"]["allowed_evidence_ids"]
            assert "C0001" not in allowed and all(value.startswith("E") for value in allowed)
            result = demo.compose(
                data["question"],
                data["topics"],
                data["evidence"],
                data["assessment"],
                data["output_format"],
            )
            if persistent:
                first = result.claims[0]
                first.evidence_ids, first.quotes = ["C0001"], {"C0001": first.text}
                result.used_evidence_ids = ["C0001"]
        else:
            result = demo.verify(data["question"], data["draft"], data["evidence"])
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

    config = ChatServiceOptions(
        endpoint="https://mock.invalid/chat",
        model="synthetic",
        revision="1",
        retries=0,
        compact_evidence=compact,
    )
    clients = tuple(
        HttpChatClient(config, role=role, transport=httpx.MockTransport(respond))
        for role in ["generator", "verifier"]
    )
    options = corpus[1].model_copy(update={"combine_assessment_and_compose": True})
    answer = answer_question(
        corpus[0], "SD5993", output=tmp_path, options=options, chat_clients=clients
    )
    assert any(event.get("validation_rule") == "evidence_not_delivered" for event in answer.trace)
    if persistent:
        assert answer.status == "failed" and answer.stop_reason == "CITATION_INVALID"
        assert not answer.claims and not answer.citations and "verify" not in tasks
        assert any(event["stage"] == "citation_validation_failed" for event in answer.trace)
    else:
        assert answer.status == "answered" and answer.citations
        assert tasks == ["assess_and_compose", "compose", "verify"]


@pytest.mark.parametrize("routes", [["dense", "bm25"], ["dense", "bm25", "sparse"]])
def test_documents_expose_routes_from_selected_generation(corpus, tmp_path, routes):
    options = corpus[1].model_copy(deep=True)
    options.retrieval.recall_routes = routes
    options.retrieval.route_weights = {route: 1.0 for route in routes}
    with load_generation(Path(corpus[2].generation_dir)) as original:
        artifacts = [original.directory / member["artifact_ref"] for member in original.members]
        build_index(artifacts, tmp_path / "index", options=options.retrieval)
    app = create_app(WebOptions(index=tmp_path / "index", state=tmp_path / "web", agent=options))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        client.headers["X-DocQA-Token"] = bootstrap["csrf_token"]
        response = client.get("/api/v1/documents")
        assert response.status_code == 200, response.text
        assert response.json()["recall_routes"] == routes


def test_browser_route_label_uses_api_routes_and_never_assumes_two_routes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for shipped frontend function check")
    source = Path(__file__).resolve().parents[1] / "src/docqa/web/static/js/dom.js"
    url = "data:text/javascript;base64," + base64.b64encode(source.read_bytes()).decode()
    code = f"import {{recallLabel}} from '{url}'; console.log(JSON.stringify([['dense','bm25'],['dense','bm25','sparse'],[],null,['dense','bm25','bad']].map(recallLabel)));"
    result = subprocess.run(
        [node, "--input-type=module"],
        input=code,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "两路检索",
        "三路检索",
        "检索路线未确认",
        "检索路线未确认",
        "检索路线未确认",
    ]
