"""HTTP Mock integration, not a real model/semantic accuracy certification."""

import json
from pathlib import Path

import httpx
import pytest
import test_agent_integration as shared

from docqa.agent.config import AgentOptions, ChatServiceOptions
from docqa.agent.pipeline import answer_question
from docqa.agent.providers import DemoChatClient, HttpChatClient
from docqa.agent.sessions import SessionStore
from docqa.docbase.config import DocBaseOptions, ServiceOptions
from docqa.docbase.pipeline import build_index
from docqa.docbase.providers import make_providers
from docqa.docbase.storage import load_generation
from docqa.domain.answers import (
    AnswerDraft,
    Assessment,
    EvidenceConflict,
    SupportDecision,
    SupportReport,
)


@pytest.fixture(scope="module", name="real_protocol")
def protocol_fixture(tmp_path_factory):
    index, _, built = shared.corpus.__wrapped__(tmp_path_factory)
    with load_generation(Path(built.generation_dir)) as generation:
        sources = [generation.directory / value["artifact_ref"] for value in generation.members]

    def service(role, **values):
        return ServiceOptions(
            endpoint=f"https://retrieval.invalid/{role}",
            model=f"mock-{role}",
            revision="1",
            tokenizer_revision="1",
            retries=0,
            **values,
        )

    options = DocBaseOptions(
        dense=service("dense", dimension=2),
        sparse=service("sparse", vocab_size=1000),
        reranker=service("reranker"),
    )

    def retrieval_http(request):
        body = json.loads(request.content)
        if request.url.path == "/reranker":
            result = {
                "results": [
                    {"index": i, "relevance_score": 1} for i in range(len(body["documents"]))
                ]
            }
        else:
            field, value = (
                ("embedding", [3, 4])
                if request.url.path == "/dense"
                else ("lexical_weights", {"10": 1.0})
            )
            result = {"data": [{"index": i, field: value} for i in range(len(body["input"]))]}
        return httpx.Response(200, json=result)

    providers = make_providers(options, transport=httpx.MockTransport(retrieval_http))
    target = tmp_path_factory.mktemp("ahttp") / "kb"
    build_index(sources, target, options=options, providers=providers)
    return target, options, providers


def test_http_generate_assess_verify_and_saved_source_chain(real_protocol, tmp_path):
    index, retrieval, providers = real_protocol
    tasks = []
    fake = DemoChatClient()

    def chat_http(request):
        body = json.loads(request.content)
        assert [message["role"] for message in body["messages"]] == ["system", "user"]
        task = json.loads(body["messages"][1]["content"])
        tasks.append(task["task"])
        data = task["data"]
        if task["task"] == "assess":
            result = fake.assess(data["question"], data["topics"], data["evidence"])
        elif task["task"] == "compose":
            result = fake.compose(
                data["question"],
                data["topics"],
                data["evidence"],
                data["assessment"],
                data["output_format"],
            )
        else:
            result = fake.verify(data["question"], data["draft"], data["evidence"])
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

    service = ChatServiceOptions(
        endpoint="https://chat.invalid/chat/completions",
        model="http-mock-model",
        revision="1",
        retries=0,
    )
    clients = tuple(
        HttpChatClient(service, role=role, transport=httpx.MockTransport(chat_http))
        for role in ("generator", "verifier")
    )
    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=AgentOptions(retrieval=retrieval),
        chat_clients=clients,
        retrieval_providers=providers,
    )
    assert tasks == ["assess", "compose", "verify"]
    assert result.status == "answered" and result.verified and not result.synthetic
    assert result.verification == "semantic_checked"
    assert SessionStore(tmp_path).resolve_citation(result.answer_id, "C0001")["snapshot_path"]


def test_mock_conflict_keeps_both_sources(real_protocol, tmp_path):
    index, retrieval, providers = real_protocol

    class ConflictClient(DemoChatClient):
        def fingerprint(self):
            return {"model": "mock-conflict-checker", "synthetic": False}

        def assess(self, question, topics, evidence, *, deadline=None):
            items = [item for item in evidence if not item.is_heading]
            ids = [item.evidence_id for item in items[:2]]
            return Assessment(
                coverage=[{"topic_id": "T01", "state": "conflicting", "evidence_ids": ids}],
                conflicts=[
                    EvidenceConflict(
                        conflict_id="X1",
                        topic_ids=["T01"],
                        evidence_ids=ids,
                        description="Mock conflict for state-machine testing only",
                    )
                ],
            )

        def compose(
            self,
            question,
            topics,
            evidence,
            assessment,
            output_format,
            repair=False,
            *,
            deadline=None,
        ):
            return AnswerDraft(
                requested_format=output_format,
                conflicts=assessment.conflicts,
                used_evidence_ids=assessment.conflicts[0].evidence_ids,
            )

        def verify(self, question, draft, evidence, *, deadline=None):
            return SupportReport(
                claim_checks=[],
                topic_checks=[
                    SupportDecision(
                        item_id="T01", state="uncertain", evidence_ids=draft.used_evidence_ids
                    )
                ],
                conflict_checks=[
                    SupportDecision(
                        item_id="X1", state="supported", evidence_ids=draft.used_evidence_ids
                    )
                ],
            )

    result = answer_question(
        index,
        "SD5993",
        output=tmp_path,
        options=AgentOptions(retrieval=retrieval),
        chat_clients=(ConflictClient(), ConflictClient(role="verifier")),
        retrieval_providers=providers,
    )
    assert result.status == "conflicting_evidence" and len(result.citations) == 2
    assert "Mock conflict" in result.answer_markdown and not result.production_ready
