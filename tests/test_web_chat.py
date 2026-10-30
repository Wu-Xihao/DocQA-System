"""Direct chat remains isolated from RAG, with real child processes and synthetic APIs."""

import json
import threading
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_web_workspace import connect, wait

from docqa.agent.config import AgentOptions, ChatServiceOptions
from docqa.agent.providers import HttpChatClient
from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.chat import direct_chat
from docqa.web.config import WebOptions
from docqa.web.models import MessageRequest
from docqa.web.runner import HeadlessRunner, WorkerRequest, verify_saved_result
from docqa.web.tasks import TaskStore
from docqa.web.workspace_store import WorkspaceStore, encoded


def send(client, cid, question="synthetic hello", **values):
    response = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"question": question, "mode": "chat", "client_request_id": uuid4().hex, **values},
    )
    assert response.status_code == 202, response.text
    return wait(client, "/api/v1/jobs/" + response.json()["job_id"], ready="succeeded")


def test_request_defaults_to_retrieval_and_chat_never_requires_scope():
    values = {"question": "hello", "client_request_id": "mode-default-123456"}
    assert MessageRequest(**values).mode == "retrieval"
    with pytest.raises(ValidationError):
        MessageRequest(**values, scope_mode="selected", doc_ids=[])
    assert MessageRequest(**values, mode="chat", scope_mode="selected", doc_ids=[]).mode == "chat"
    with pytest.raises(ValidationError):
        MessageRequest(**values, mode="other")


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "chat", "generation_id": "a" * 32},
        {"mode": "chat", "doc_ids": ["private"]},
        {"mode": "chat", "chat_history": [{"role": "user", "content": "orphan"}]},
        {"mode": "chat", "chat_history": [{"role": "system", "content": "bad"}]},
        {"mode": "retrieval"},
    ],
)
def test_worker_enforces_distinct_modes_and_bounded_context(values):
    with pytest.raises(ValidationError):
        WorkerRequest(question="hello", **values)


def test_direct_api_uses_plain_chat_protocol_and_keeps_context():
    messages = [
        {"role": "system", "content": "ordinary chat"},
        {"role": "user", "content": "synthetic"},
    ]

    def handler(request):
        payload = json.loads(request.content)
        assert payload["messages"] == messages
        assert "response_format" not in payload
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "A normal text response, not JSON.",
                        },
                    }
                ]
            },
        )

    client = HttpChatClient(
        ChatServiceOptions(
            endpoint="https://synthetic.invalid/chat",
            model="test",
            revision="1",
            thinking="disabled",
        ),
        transport=httpx.MockTransport(handler),
    )
    assert client.converse(messages, deadline=time.monotonic() + 3).startswith("A normal text")


def test_chat_receipt_bound_to_job_question_and_history_without_index(tmp_path):
    request = WorkerRequest(mode="chat", question="hello")
    jid = uuid4().hex
    receipt = direct_chat(jid, request, AgentOptions(mode="demo"), tmp_path / "output")
    assert (
        verify_saved_result(tmp_path / "no-index", tmp_path / "output", jid, request, receipt)
        == receipt
    )
    with pytest.raises(DocQAError, match="Saved chat"):
        verify_saved_result(
            tmp_path / "no-index",
            tmp_path / "output",
            jid,
            WorkerRequest(mode="chat", question="changed"),
            receipt,
        )


def test_history_budget_keeps_recent_complete_turns_and_mode_is_idempotent(tmp_path):
    tasks = TaskStore(tmp_path)
    store = WorkspaceStore(tasks)
    cid = store.create("owner")["id"]
    for number in range(10):
        value = MessageRequest(
            question=f"turn-{number}", mode="chat", client_request_id=uuid4().hex
        )
        jid = store.submit(cid, "owner", value, None, None)
        assert tasks.claim()["job_id"] == jid
        tasks.finish(
            jid,
            receipt={"status": "answered", "chat_id": uuid4().hex, "chat": {"content": "A" * 5000}},
        )
    value = MessageRequest(question="next", mode="chat", client_request_id=uuid4().hex)
    jid = store.submit(cid, "owner", value, None, None)
    history = tasks.get(jid)["payload"]["chat_history"]
    assert [m["content"] for m in history if m["role"] == "user"] == [
        "turn-6",
        "turn-7",
        "turn-8",
        "turn-9",
    ]
    assert sum(len(m["content"].encode()) for m in history) <= 24000
    assert [m["role"] for m in history] == ["user", "assistant"] * 4
    assert store.previous_submission(cid, "owner", value) == jid
    with pytest.raises(DocQAError, match="Request key"):
        store.previous_submission(cid, "owner", value.model_copy(update={"mode": "retrieval"}))


def test_legacy_retrieval_fingerprint_and_chat_only_credentials(tmp_path, monkeypatch):
    tasks = TaskStore(tmp_path)
    store = WorkspaceStore(tasks)
    cid = store.create("owner")["id"]
    value = MessageRequest(question="query", client_request_id=uuid4().hex)
    jid = store.submit(cid, "owner", value, "a" * 32, ["doc"])
    legacy = value.model_dump(mode="json", exclude={"mode"})
    with tasks.transaction() as db:
        stored = db.execute(
            "SELECT request_hash FROM ws_messages WHERE job_id=? AND role='user'", (jid,)
        ).fetchone()[0]
    assert stored == sha256(encoded(legacy).encode()).hexdigest()
    assert store.previous_submission(cid, "owner", value) == jid
    options = AgentOptions()
    options.generator.api_key_env = "SYNTHETIC_CHAT_KEY"
    options.retrieval.dense.api_key_env = "SYNTHETIC_DENSE_KEY"
    monkeypatch.setenv("SYNTHETIC_CHAT_KEY", "synthetic-one")
    monkeypatch.setenv("SYNTHETIC_DENSE_KEY", "synthetic-two")
    runner = HeadlessRunner(tmp_path / "none", tmp_path / "agent", options, tmp_path / "runs")
    environment = runner._environment(chat_only=True)
    assert environment["SYNTHETIC_CHAT_KEY"] == "synthetic-one"
    assert "SYNTHETIC_DENSE_KEY" not in environment


def test_chat_without_index_persists_streams_and_deletes_every_owned_output(tmp_path):
    options = WebOptions(
        index=tmp_path / "absent", state=tmp_path / "web", agent=AgentOptions(mode="demo")
    )
    app = create_app(options)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        denied = client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"question": "hello", "client_request_id": uuid4().hex},
        )
        assert denied.status_code != 202
        index_before = {p.relative_to(options.index) for p in options.index.rglob("*")}
        done = send(client, cid)
        assert done["mode"] == "chat" and done["generation_id"] is None
        assert done["answer"] is None and done["answer_id"] is None
        assert done["chat"]["content"].endswith("synthetic hello")
        assert "verified" not in done["chat"] and "citations" not in done["chat"]
        assert client.get("/api/v1/conversations/" + cid).json()["generation_id"] is None
        assert {p.relative_to(options.index) for p in options.index.rglob("*")} == index_before
        events = client.get("/api/v1/events?conversation_id=" + cid)
        values = [
            json.loads(line[6:]) for line in events.text.splitlines() if line.startswith("data: ")
        ]
        assert [value["kind"] for value in values] == ["chat_start", "chat_delta", "chat_done"]
        assert (
            "".join(v["text"] for v in values if v["kind"] == "chat_delta")
            == done["chat"]["content"]
        )
        assert client.get("/api/v1/queries/" + done["chat_id"]).status_code == 404
        directory = options.state / "agent" / done["job_id"]
        assert (directory / "chat.json").exists()
        other = TestClient(app, base_url="http://127.0.0.1:8765")
        try:
            connect(other)
            assert other.get("/api/v1/jobs/" + done["job_id"]).status_code == 404
        finally:
            other.close()
        response = client.delete("/api/v1/conversations/" + cid).json()
        for _ in range(30):
            if response["state"] == "complete":
                break
            time.sleep(0.05)
            response = client.get("/api/v1/deletions/" + response["id"]).json()
        assert response["state"] == "complete"
        assert not directory.exists()
        assert client.get("/api/v1/jobs/" + done["job_id"]).status_code == 404
        assert not client.get("/api/v1/conversations").json()["items"]


def test_http_chat_needs_only_generator_and_transmits_complete_turns(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            reply = "Synthetic reply: " + payload["messages"][-1]["content"]
            raw = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": reply},
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent = AgentOptions(
        mode="http",
        generator=ChatServiceOptions(
            endpoint=f"http://127.0.0.1:{server.server_port}/chat",
            model="synthetic",
            revision="1",
            retries=0,
            timeout=3,
        ),
    )
    app = create_app(WebOptions(index=tmp_path / "missing", state=tmp_path / "web", agent=agent))
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            connect(client)
            cid = client.post("/api/v1/conversations", json={}).json()["id"]
            first = send(client, cid, "my synthetic color is blue")
            second = send(client, cid, "what color?")
            assert not first["chat"]["synthetic"] and second["chat"]["model"] == "synthetic"
            assert len(requests) == 2
            assert [m["role"] for m in requests[1]["messages"]] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert requests[1]["messages"][2]["content"] == first["chat"]["content"]
            assert "response_format" not in requests[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_mixed_modes_keep_pin_separate_contexts_and_retry_original_mode(corpus, tmp_path):
    index, agent, _ = corpus
    app = create_app(WebOptions(index=index, state=tmp_path, agent=agent))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        chat = send(client, cid, "NORMAL_CHAT_SENTINEL")
        rag = send(client, cid, "SD5993", mode="retrieval")
        assert rag["answer"]["citations"] and rag["generation_id"]
        payload = app.state.manager.store.get(rag["job_id"])["payload"]
        assert payload["context_questions"] == []
        second = send(client, cid, "continue normal")
        payload = app.state.manager.store.get(second["job_id"])["payload"]
        assert payload["chat_history"] == [
            {"role": "user", "content": "NORMAL_CHAT_SENTINEL"},
            {"role": "assistant", "content": chat["chat"]["content"]},
        ]
        assert (
            client.get("/api/v1/conversations/" + cid).json()["generation_id"]
            == rag["generation_id"]
        )
        messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["items"]
        retry = client.post(f"/api/v1/messages/{messages[-1]['id']}/retry", json={})
        assert retry.status_code == 202, retry.text
        repeated = wait(client, "/api/v1/jobs/" + retry.json()["job_id"], ready="succeeded")
        assert repeated["mode"] == "chat"
        assert (
            app.state.manager.store.get(repeated["job_id"])["payload"]["chat_history"]
            == payload["chat_history"]
        )
