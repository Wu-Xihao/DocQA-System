"""Real HTTP transport over pinned built-in questions and system-owned citations."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from docqa.agent.pipeline import answer_question
from docqa.cli.main import app as cli
from docqa.domain.errors import DocQAError
from docqa.web.app import _secret, create_app
from docqa.web.config import WebOptions, load_options
from docqa.web.tasks import QuestionManager, TaskStore


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def configured(corpus, tmp_path):
    index, agent, _ = corpus
    return WebOptions(index=index, state=tmp_path, agent=agent)


def connected(app):
    return TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


def bootstrap(client):
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200, response.text
    data = response.json()
    client.headers["X-DocQA-Token"] = data["csrf_token"]
    return data


def wait_job(client, job_id):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("Question did not finish")


def test_browser_bootstrap_and_actual_headless_question_sources(corpus, tmp_path):
    with connected(create_app(configured(corpus, tmp_path))) as client:
        data = bootstrap(client)
        assert data["worker_ready"] and data["settings"]["synthetic"]
        assert data["knowledge_base"]["documents"]
        home = client.get("/")
        assert home.status_code == 200 and "文档" in home.text
        for asset in ("app.js", "styles.css"):
            assert client.get("/static/" + asset).status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200
        accepted = client.post("/api/v1/questions", json={"question": "SD5993"})
        assert accepted.status_code == 202, accepted.text
        job = wait_job(client, accepted.json()["job_id"])
        assert job["state"] == "succeeded", job
        answer = job["answer"]
        assert answer["status"] == "answered" and answer["synthetic"] and not answer["verified"]
        assert "trace" not in answer and "markdown_path" not in answer["citations"][0]
        assert str(tmp_path) not in str(answer)
        citation = answer["citations"][0]
        meta = client.get(citation["resources"]["metadata"]).json()
        assert "SD5993" in meta["quote"] and "snapshot_path" not in meta
        source = client.get(citation["resources"]["markdown"])
        assert source.status_code == 200 and "SD5993" in source.text
        raw = client.get(citation["resources"]["snapshot"])
        assert raw.status_code == 200 and raw.content.startswith(b"PK")
        assert "attachment" in raw.headers["content-disposition"]
        assert (
            client.get(f"/api/v1/queries/{answer['answer_id']}").json()["answer_id"]
            == answer["answer_id"]
        )
        assert client.get("/api/v1/jobs").json()["jobs"][0]["answer_id"] == answer["answer_id"]
        # Cookie-only resource links work in img/download; API JSON does not.
        del client.headers["X-DocQA-Token"]
        assert client.get(citation["resources"]["snapshot"]).status_code == 200
        assert client.get(citation["resources"]["metadata"]).status_code == 403


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"question": "外星飞船燃料配比？"}, "insufficient_evidence"),
        ({"question": "SD5993", "topics": ["SD5993", "外星飞船燃料配比？"]}, "partial"),
        ({"question": "SD5993", "categories": []}, "insufficient_evidence"),
        ({"question": "SD5993", "categories": ["nonexistent"]}, "insufficient_evidence"),
        ({"question": "SD5993", "output_format": "table"}, "answered"),
    ],
)
def test_question_states_keep_core_meaning(corpus, tmp_path, payload, expected):
    options = configured(corpus, tmp_path)

    class InProcessRunner:
        def __init__(self, job):
            self.job = job

        def run(self, job_id, request, *, cancelled, progress):
            result = answer_question(
                options.index,
                request["question"],
                output=options.state / "agent" / job_id,
                options=options.agent,
                categories=request["categories"],
                doc_ids=request["doc_ids"],
                required_topics=request["topics"],
                output_format=request["output_format"],
                generation_id=request["generation_id"],
            )
            return {
                "answer_id": result.answer_id,
                "session_id": result.session_id,
                "status": result.status,
            }

    with connected(create_app(options, runner_factory=InProcessRunner)) as client:
        bootstrap(client)
        accepted = client.post("/api/v1/questions", json=payload)
        job = wait_job(client, accepted.json()["job_id"])
        assert job["answer"]["status"] == expected, job


def test_ownership_cancellation_and_queue_bound(corpus, tmp_path):
    options = configured(corpus, tmp_path)
    options.max_pending_jobs = 1
    entered = threading.Event()

    class WaitingRunner:
        def run(self, job_id, request, *, cancelled, progress):
            entered.set()
            while not cancelled():
                time.sleep(0.01)
            raise DocQAError("WEB_JOB_CANCELLED", "Cancelled")

    with connected(create_app(options, runner_factory=lambda job: WaitingRunner())) as owner:
        bootstrap(owner)
        created = owner.post("/api/v1/questions", json={"question": "SD5993"}).json()
        job_id = created["job_id"]
        assert entered.wait(3)
        assert owner.post("/api/v1/questions", json={"question": "TLS"}).status_code == 429
        # A second browser cookie has neither history nor object access.
        other = TestClient(owner.app, base_url="http://127.0.0.1:8765")
        bootstrap(other)
        assert other.get("/api/v1/jobs").json()["jobs"] == []
        assert other.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert other.post(f"/api/v1/jobs/{job_id}/cancel", json={}).status_code == 404
        owner.post(f"/api/v1/jobs/{job_id}/cancel", json={})
        assert wait_job(owner, job_id)["state"] == "cancelled"


@pytest.mark.parametrize(
    "payload",
    [
        {"question": " "},
        {"question": "x", "state": "C:/Windows"},
        {"question": "x", "runner": "calc.exe"},
        {"question": "x", "topics": []},
        {"question": "x", "topics": ["a", "a"]},
        {"question": "x\x00"},
        {"question": "x", "categories": ["a" * 513]},
        {"question": "x", "output_format": "html"},
    ],
)
def test_http_input_boundary(corpus, tmp_path, payload):
    with connected(create_app(configured(corpus, tmp_path))) as client:
        bootstrap(client)
        reply = client.post("/api/v1/questions", json=payload)
        assert reply.status_code == 422
        assert reply.json()["error"]["code"] == "INVALID_REQUEST"


def test_missing_index_and_missing_worker_do_not_accept_async_work(tmp_path):
    options = WebOptions(index=tmp_path / "absent", state=tmp_path / "web")
    with connected(create_app(options)) as client:
        data = bootstrap(client)
        assert "INDEX_NOT_FOUND" in data["errors"] and not data["ready"]
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.post("/api/v1/questions", json={"question": "x"}).status_code == 503


def test_task_cancel_commit_races_and_restart_recovery(tmp_path):
    store = TaskStore(tmp_path, capacity=4)
    job = store.enqueue("owner", {"question": "x"})
    store.claim()
    store.cancel(job["job_id"], "owner")
    receipt = {"answer_id": "a", "session_id": "s", "status": "answered"}
    assert store.finish(job["job_id"], receipt=receipt)["state"] == "cancelled"
    assert store.get(job["job_id"])["result"] is None
    second = store.enqueue("owner", {"question": "y"})
    store.claim()
    store.finish(second["job_id"], receipt=receipt)
    assert store.cancel(second["job_id"], "owner")["state"] == "succeeded"
    third = store.enqueue("owner", {"question": "z"})
    store.claim()
    store.recover()
    assert store.get(third["job_id"])["error_code"] == "WEB_WORKER_RESTARTED"


def test_worker_lock_prevents_second_consumer(corpus, tmp_path):
    options = configured(corpus, tmp_path)
    one, two = QuestionManager(options), QuestionManager(options)
    one.start()
    try:
        with pytest.raises(DocQAError) as error:
            two.start()
        assert error.value.code == "WEB_ALREADY_RUNNING"
    finally:
        one.stop()


@pytest.mark.parametrize("value", ["false", "[]", "42", "{host: 0.0.0.0}", "{backend: claude}"])
def test_configuration_stays_loopback_builtin_only(tmp_path, value):
    path = tmp_path / "web.yaml"
    path.write_text(value, encoding="utf-8")
    with pytest.raises(DocQAError):
        load_options(path)


def test_cli_help_and_configuration_doctor():
    runner = CliRunner()
    assert runner.invoke(cli, ["web", "--help"]).exit_code == 0
    result = runner.invoke(cli, ["web", "doctor", "--config", "configs/web.demo.yaml"])
    assert result.exit_code == 0 and '"connection_tested": false' in result.stdout


def test_state_index_overlap_rejected(tmp_path):
    with pytest.raises(ValidationError):
        WebOptions(index=tmp_path, state=tmp_path / "web")


def test_parallel_secret_creation_and_failed_write_recovery(tmp_path, monkeypatch):
    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(lambda _: _secret(tmp_path), range(4)))
    assert len(set(values)) == 1 and len(values[0]) == 32
    other = tmp_path / "other"

    def failed_sync(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("docqa.web.app.os.fsync", failed_sync)
    with pytest.raises(OSError):
        _secret(other)
    assert not (other / ".web-secret").exists()


def test_startup_failure_releases_worker_lock(corpus, tmp_path, monkeypatch):
    options = configured(corpus, tmp_path)
    manager = QuestionManager(options)

    def fail_recovery():
        raise DocQAError("WEB_STORAGE_FAILED", "simulated failure")

    monkeypatch.setattr(manager.store, "recover", fail_recovery)
    with pytest.raises(DocQAError):
        manager.start()
    replacement = QuestionManager(options)
    replacement.start()
    try:
        with pytest.raises(DocQAError):
            replacement.start()
    finally:
        replacement.stop()


@pytest.mark.parametrize("field", ["categories", "doc_ids", "topics"])
def test_unencodable_scope_is_rejected_before_enqueue(corpus, tmp_path, field):
    with connected(create_app(configured(corpus, tmp_path))) as client:
        bootstrap(client)
        response = client.post(
            "/api/v1/questions",
            content=('{"question":"x","' + field + '":["\\ud800"]}').encode(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422


def test_worker_factory_error_is_recorded_without_killing_consumer(corpus, tmp_path):
    def bad_factory(job):
        raise OSError("should not leak this text")

    with connected(create_app(configured(corpus, tmp_path), runner_factory=bad_factory)) as client:
        bootstrap(client)
        job_id = client.post("/api/v1/questions", json={"question": "x"}).json()["job_id"]
        job = wait_job(client, job_id)
        assert job["error_code"] == "RUNNER_FAILED" and "leak" not in str(job)
        assert client.get("/api/v1/bootstrap").json()["worker_ready"]
