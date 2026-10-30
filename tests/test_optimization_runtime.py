"""Synthetic real child processes and delayed local SSE; no external model calls."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient
from test_web_workspace import connect, send, wait

from docqa.agent.config import AgentOptions, ChatServiceOptions
from docqa.agent.providers import HttpChatClient
from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.config import WebOptions
from docqa.web.runner import WorkerRequest
from docqa.web.streaming import converse_stream
from docqa.web.warm_runner import WarmRunner


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def event(content=None, reason=None, **delta):
    if content is not None:
        delta["content"] = content
    return (
        "data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}, ensure_ascii=False
        )
        + "\n\n"
    ).encode()


def test_stream_yields_before_provider_finishes_and_rejects_truncation():
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield event("第一段")
            yield event("第二段")
            yield event(reason="stop") + b"data: [DONE]\n\n"

    client = HttpChatClient(
        ChatServiceOptions(
            endpoint="https://test.invalid/chat", model="synthetic", revision="1", stream=True
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks())),
    )
    stream = converse_stream(client, [{"role": "user", "content": "test"}], time.monotonic() + 5)
    assert next(stream) == "第一段"
    assert "".join(stream) == "第二段"
    client.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=event("unfinished"))
    )
    with pytest.raises(DocQAError, match="Incomplete"):
        list(converse_stream(client, [], time.monotonic() + 5))


@pytest.mark.parametrize(
    "body",
    [
        event("text", reason="length") + b"data: [DONE]\n\n",
        event(tool_calls=[{"name": "do-not-execute"}]) + b"data: [DONE]\n\n",
        b"data: [DONE]\n\n",
        b"data: []\n\n",
        b"data: " + b"a" * 70000,
    ],
    ids=["truncated", "tool-call", "early-done", "bad-json", "excessive"],
)
def test_bad_stream_never_completes(body):
    client = HttpChatClient(
        ChatServiceOptions(endpoint="https://test.invalid/chat", model="s", revision="1"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    with pytest.raises(DocQAError):
        list(converse_stream(client, [], time.monotonic() + 5))


def test_reused_child_keeps_jobs_separate_and_recovers_after_cancel(corpus, tmp_path):
    index, options, built = corpus
    runner = WarmRunner(index, tmp_path / "agent", options, tmp_path / "runs", timeout_seconds=20)
    timings = []
    try:
        for question in ("FE_HEART_Start 心跳", "TLS 证书链"):
            jid = uuid4().hex
            runner.agent_state = tmp_path / "agent" / jid
            result = runner.run(
                jid,
                {"question": question, "generation_id": built.generation_id},
                cancelled=lambda: False,
                progress=lambda _: None,
            )
            assert result["status"] == "answered"
            answer = json.loads(
                next((runner.agent_state / "answers").glob("*/answer.json")).read_text("utf-8")
            )
            assert answer["question"] == question
            timings.append(json.loads((tmp_path / "runs" / jid / "timing.json").read_text("utf-8")))
        assert not timings[0]["reused"] and timings[1]["reused"]
        assert timings[0]["process_id"] == timings[1]["process_id"]
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls > 1

        jid = uuid4().hex
        runner.agent_state = tmp_path / "agent" / jid
        with pytest.raises(DocQAError, match="cancelled"):
            runner.run(
                jid,
                WorkerRequest(mode="chat", question="canceled").model_dump(),
                cancelled=cancel,
                progress=lambda _: None,
            )
        assert runner.process is None
        runner.agent_state = tmp_path / "agent" / uuid4().hex
        runner.run(
            uuid4().hex,
            WorkerRequest(mode="chat", question="fresh context").model_dump(),
            cancelled=lambda: False,
            progress=lambda _: None,
        )
    finally:
        runner.close()


def test_two_dispatchers_and_artifact_edit_preserve_checked_facts(corpus, tmp_path):
    index, agent, _ = corpus
    options = WebOptions(
        index=index, state=tmp_path, agent=agent, query_workers=2, reuse_workers=True
    )
    with TestClient(create_app(options), base_url="http://127.0.0.1:8765") as client:
        connect(client)
        conversations = [
            client.post("/api/v1/conversations", json={}).json()["id"] for _ in range(2)
        ]
        jobs = [send(client, c, key=uuid4().hex) for c in conversations]
        done = [wait(client, "/api/v1/jobs/" + j["job_id"], ready="succeeded") for j in jobs]
        assert len(client.app.state.manager.warm_runners) == 2
        request = client.post(
            "/api/v1/studio/jobs",
            json={
                "conversation_id": conversations[0],
                "answer_id": done[0]["answer"]["answer_id"],
                "kind": "mindmap",
            },
        )
        assert request.status_code == 202, request.text
        task = wait(client, "/api/v1/studio/jobs/" + request.json()["id"])
        artifact = client.get("/api/v1/artifacts/" + task["artifact_id"]).json()
        ids = [c["id"] for c in artifact["content"]["facts"]]
        edit = {
            "title": "Edited research",
            "plan": {"sections": [{"title": "Edited group", "claim_ids": ids}]},
        }
        response = client.post("/api/v1/artifacts/" + artifact["id"] + "/edits", json=edit)
        assert response.status_code == 201, response.text
        revised = response.json()
        assert revised["previous_id"] == artifact["id"] and revised["version"] == 2
        for key in ("facts", "citations"):
            assert revised["content"][key] == artifact["content"][key]
        assert revised["content"]["organization"] == "user_editorial"
        edit["plan"]["sections"][0]["claim_ids"] = ["invented"]
        assert client.post(
            "/api/v1/artifacts/" + artifact["id"] + "/edits", json=edit
        ).status_code in {400, 409, 422}
        assert client.get("/api/v1/artifacts/" + artifact["id"]).json() == artifact


def test_delayed_provider_preview_visible_before_final_reply(tmp_path):
    first = threading.Event()
    finish = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload["stream"] is True and "response_format" not in payload
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(event("live prefix"))
            self.wfile.flush()
            first.set()
            finish.wait(8)
            self.wfile.write(event(" complete") + event(reason="stop") + b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = ChatServiceOptions(
        endpoint=f"http://127.0.0.1:{server.server_port}/chat",
        model="synthetic",
        revision="1",
        stream=True,
        timeout=12,
    )
    options = WebOptions(
        index=tmp_path / "no-index",
        state=tmp_path / "web",
        agent=AgentOptions(mode="http", generator=cfg, verifier=cfg),
        reuse_workers=True,
    )
    try:
        with TestClient(create_app(options), base_url="http://127.0.0.1:8765") as client:
            connect(client)
            cid = client.post("/api/v1/conversations", json={}).json()["id"]
            job = client.post(
                f"/api/v1/conversations/{cid}/messages",
                json={
                    "question": "synthetic live test",
                    "mode": "chat",
                    "client_request_id": uuid4().hex,
                },
            ).json()
            assert first.wait(8)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                progress = client.get("/api/v1/jobs/" + job["job_id"]).json()
                if progress.get("chat_preview"):
                    break
                time.sleep(0.05)
            assert (
                progress["state"] == "running"
                and progress["chat_preview"]["content"] == "live prefix"
            )
            assert not progress.get("chat") and not progress.get("answer")
            finish.set()
            done = wait(client, "/api/v1/jobs/" + job["job_id"], ready="succeeded")
            assert done["chat"]["content"] == "live prefix complete"
            assert "verified" not in done["chat"]
    finally:
        finish.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_visual_child_uses_actual_pixels_and_cancels_without_blocking(tmp_path):
    from PIL import Image

    from docqa.doc2md.config import VisionConfig
    from docqa.web.visual import isolated_check

    image = tmp_path / "pixels.png"
    Image.new("RGB", (12, 8), "blue").save(image)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            value = {
                "checks": [
                    {
                        "claim_id": "A",
                        "state": "uncertain",
                        "reason": "synthetic checker only",
                        "observations": ["blue pixels"],
                    }
                ]
            }
            raw = json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = VisionConfig(
        mode="http",
        endpoint=f"http://127.0.0.1:{server.server_port}/chat",
        model="synthetic-vl",
        revision="1",
    )
    try:
        report = isolated_check(
            image, "question", [{"claim_id": "A", "text": "not visible in synthetic image"}], cfg
        )
        assert (
            report["checks"][0]["state"] == "uncertain"
            and not report["answer_verification_changed"]
        )
        with pytest.raises(DocQAError, match="cancelled"):
            isolated_check(
                image, "question", [{"claim_id": "A", "text": "test"}], cfg, cancelled=lambda: True
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
