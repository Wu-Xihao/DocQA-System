"""Deadline coordination with synthetic sources, fake clocks and isolated subprocesses."""

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_agent_integration as shared
from pydantic import ValidationError
from test_dashscope_joint import NativeServer
from test_dashscope_joint import options as joint_options
from test_web_runner import child_script

from docqa.agent import pipeline
from docqa.agent.config import AgentOptions
from docqa.agent.providers import DemoChatClient
from docqa.docbase import providers as provider_module
from docqa.docbase.providers import HttpDenseClient, make_providers
from docqa.domain.answers import Assessment, CoverageItem, PlannedQuery
from docqa.domain.errors import DocQAError
from docqa.web.config import WebOptions
from docqa.web.runner import HeadlessRunner


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


class Clock:
    def __init__(self, now=None):
        self.now = time.monotonic() if now is None else now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("response", ["late", "retry"])
def test_retrieval_http_caps_timeout_discards_late_results_and_stops_retries(monkeypatch, response):
    clock, calls = Clock(100.0), []
    monkeypatch.setattr(provider_module, "time", clock)
    config = joint_options().dense.model_copy(
        update={"protocol": "openai.embeddings", "retries": 3}
    )

    def respond(request):
        calls.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 0.05 + 1e-9
        if response == "retry":
            return httpx.Response(503)
        clock.now += 0.1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 64}]})

    client = HttpDenseClient(config, transport=httpx.MockTransport(respond), deadline=100.05)
    with pytest.raises(DocQAError) as caught:
        client.encode(["synthetic"], "query")
    assert caught.value.code == "MODEL_DEADLINE_EXCEEDED"
    assert len(calls) == 1


def test_joint_encoder_and_reranker_share_deadline_without_changing_index_identity():
    config, server = joint_options(), NativeServer()
    plain = make_providers(config, transport=httpx.MockTransport(server))
    bounded = make_providers(config, transport=httpx.MockTransport(server), deadline=123.0)
    assert [x.fingerprint() for x in plain] == [x.fingerprint() for x in bounded]
    assert bounded[0].encoder is bounded[1].encoder
    assert bounded[0].encoder.deadline == bounded[2].deadline == 123.0


def use_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(
        pipeline,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic,
            perf_counter=time.perf_counter,
        ),
    )
    return clock


def test_session_preparation_counts_towards_answer_budget(corpus, tmp_path, monkeypatch):
    clock = use_clock(monkeypatch)
    original = pipeline.create_evidence_session

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now += 2
        return result

    monkeypatch.setattr(pipeline, "create_evidence_session", prepare)
    options = corpus[1].model_copy(update={"deadline_seconds": 1})
    answer = pipeline.answer_question(corpus[0], "SD5993", output=tmp_path, options=options)
    assert answer.status == "failed" and answer.stop_reason == "AGENT_DEADLINE_EXCEEDED"
    assert answer.queries_used == 0 and not answer.claims


def test_followup_reserve_preserves_verification_and_reports_missing_topics(
    corpus, tmp_path, monkeypatch
):
    clock = use_clock(monkeypatch)

    class SlowAssessment(DemoChatClient):
        def assess(self, question, topics, evidence, *, deadline=None):
            evidence = list(evidence)
            clock.now += 75
            return Assessment(
                coverage=[
                    CoverageItem(
                        topic_id="T01", state="supported", evidence_ids=[evidence[0].evidence_id]
                    ),
                    CoverageItem(topic_id="T02", state="missing"),
                ],
                followup_queries=[PlannedQuery(query="unknown synthetic topic", topic_ids=["T02"])],
            )

    class Checked(DemoChatClient):
        calls = 0

        def verify(self, *args, **kwargs):
            self.calls += 1
            return super().verify(*args, **kwargs)

    options = corpus[1].model_copy(
        update={"deadline_seconds": 100, "finalization_reserve_seconds": 30}
    )
    verifier = Checked()
    answer = pipeline.answer_question(
        corpus[0],
        "SD5993 与未知主题",
        output=tmp_path,
        options=options,
        required_topics=["SD5993", "unknown synthetic topic"],
        chat_clients=(SlowAssessment(), verifier),
    )
    assert answer.queries_used == 1 and verifier.calls == 1
    assert answer.status == "partial" and "T02" in answer.unanswered_topics
    assert any(e["stage"] == "followup_skipped" for e in answer.trace)


def test_short_repair_budget_keeps_no_unsupported_claim(corpus, tmp_path, monkeypatch):
    clock = use_clock(monkeypatch)

    class Generator(DemoChatClient):
        calls = 0

        def compose(self, *args, **kwargs):
            self.calls += 1
            return super().compose(*args, **kwargs)

    class Unsupported(DemoChatClient):
        calls = 0

        def verify(self, *args, **kwargs):
            self.calls += 1
            report = super().verify(*args, **kwargs)
            clock.now += 85
            return report.model_copy(
                update={
                    "claim_checks": [
                        check.model_copy(update={"state": "unsupported"})
                        for check in report.claim_checks
                    ]
                }
            )

    options = corpus[1].model_copy(
        update={"deadline_seconds": 100, "finalization_reserve_seconds": 20}
    )
    generator, verifier = Generator(), Unsupported()
    answer = pipeline.answer_question(
        corpus[0], "SD5993", output=tmp_path, options=options, chat_clients=(generator, verifier)
    )
    assert generator.calls == verifier.calls == 1
    assert not answer.claims and answer.status == "insufficient_evidence"
    assert "UNSUPPORTED_ITEMS_REMOVED" in answer.warnings
    assert any(e["stage"] == "repair_skipped" for e in answer.trace)


def test_runner_timeout_retains_only_sanitized_phase_timings(tmp_path, monkeypatch):
    runner = HeadlessRunner(
        tmp_path / "index",
        tmp_path / "agent",
        AgentOptions(mode="demo"),
        tmp_path / "runs",
        timeout_seconds=0.3,
    )
    child_script(
        monkeypatch,
        "import sys,time; print('private-evidence-do-not-log',file=sys.stderr,flush=True); print('DOCQA_STAGE:verifying',file=sys.stderr,flush=True); time.sleep(10)",
    )
    identity = uuid4().hex
    with pytest.raises(DocQAError) as caught:
        runner.run(
            identity,
            {"question": "private-question-do-not-log", "generation_id": "a" * 32},
            cancelled=lambda: False,
            progress=lambda _: None,
        )
    assert caught.value.code == "RUNNER_TIMEOUT"
    raw = (runner.runs / identity / "timing.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["error_code"] == "RUNNER_TIMEOUT"
    assert any(stage["stage"] == "verifying" for stage in data["stages"])
    assert "private-evidence" not in raw and "private-question" not in raw


def test_web_rejects_runner_deadline_that_cannot_cover_agent():
    with pytest.raises(ValidationError):
        WebOptions(runner_timeout_seconds=45, agent=AgentOptions(deadline_seconds=45))
    with pytest.raises(ValidationError):
        AgentOptions(deadline_seconds=10, finalization_reserve_seconds=10)


@pytest.mark.parametrize("seconds,status", [(45, "failed"), (90, "answered")])
def test_old_short_budget_vs_configured_budget_without_wall_clock_sleep(
    corpus, tmp_path, monkeypatch, seconds, status
):
    clock = use_clock(monkeypatch)

    class Delayed(DemoChatClient):
        def assess(self, *args, **kwargs):
            answer = super().assess(*args, **kwargs)
            clock.now += 50
            return answer

    config = corpus[1].model_copy(update={"deadline_seconds": seconds})
    answer = pipeline.answer_question(
        corpus[0],
        "SD5993",
        output=tmp_path,
        options=config,
        chat_clients=(Delayed(), DemoChatClient()),
    )
    assert answer.status == status
    if status == "failed":
        assert answer.stop_reason == "AGENT_DEADLINE_EXCEEDED" and not answer.claims
    else:
        assert answer.claims and answer.support_report and answer.citations
