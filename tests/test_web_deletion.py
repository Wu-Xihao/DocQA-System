"""Permanent deletion clears owned outputs and cannot resurrect running tasks."""

import sqlite3
import threading
import time
from uuid import uuid4

import pytest
import test_agent_integration as shared
from test_web_workspace import client_for, connect, send, wait

from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.config import WebOptions


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def erased(client, result):
    until = time.monotonic() + 10
    while result["state"] != "complete" and time.monotonic() < until:
        time.sleep(0.05)
        response = client.get("/api/v1/deletions/" + result["id"])
        assert response.status_code == 200
        result = response.json()
    assert result["state"] == "complete", result
    return result


def artifact(client, cid, aid):
    job = client.post(
        "/api/v1/studio/jobs", json={"conversation_id": cid, "answer_id": aid, "kind": "report"}
    ).json()
    value = wait(client, "/api/v1/studio/jobs/" + job["id"])
    identity = value["artifact_id"]
    exp = client.post(f"/api/v1/artifacts/{identity}/exports", json={"format": "md"}).json()
    wait(client, "/api/v1/exports/" + exp["id"])
    return identity, exp["id"]


def test_conversation_deletion_removes_rows_answers_exports_and_files(corpus, tmp_path):
    index, agent, _ = corpus
    with client_for(WebOptions(index=index, state=tmp_path, agent=agent)) as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        other = client.post("/api/v1/conversations", json={"title": "keep"}).json()["id"]
        job = send(client, cid)
        answer = wait(client, "/api/v1/jobs/" + job["job_id"], ready="succeeded")["answer"]
        identity, export = artifact(client, cid, answer["answer_id"])
        # Legacy migration snapshots must not retain the erased question.
        backups = tmp_path / "backups"
        backups.mkdir(exist_ok=True)
        backup = backups / ("before-workspace-" + uuid4().hex + ".sqlite")
        owner = client.app.state.manager.store.get(job["job_id"])["owner_id"]
        with sqlite3.connect(backup) as db:
            db.execute("CREATE TABLE jobs(job_id TEXT,owner_id TEXT,payload TEXT)")
            db.executemany(
                "INSERT INTO jobs VALUES(?,?,?)",
                [(job["job_id"], owner, "private question"), ("keep", owner, "keep this question")],
            )
        assert (tmp_path / "agent" / job["job_id"]).is_dir()
        result = client.delete("/api/v1/conversations/" + cid)
        assert result.status_code in {200, 202}
        erased(client, result.json())
        with sqlite3.connect(backup) as db:
            assert db.execute("SELECT payload FROM jobs").fetchall() == [("keep this question",)]
        assert not (tmp_path / "agent" / job["job_id"]).exists()
        assert not (tmp_path / "runs" / job["job_id"]).exists()
        assert not (tmp_path / "studio" / identity).exists()
        assert not (tmp_path / "studio/exports" / (export + ".md")).exists()
        for path in [
            f"/conversations/{cid}",
            f"/jobs/{job['job_id']}",
            f"/queries/{answer['answer_id']}",
            f"/artifacts/{identity}",
            f"/exports/{export}/content",
        ]:
            assert client.get("/api/v1" + path).status_code == 404
        assert client.post(f"/api/v1/conversations/{cid}/restore", json={}).status_code == 404
        for _ in range(2):
            items = client.get("/api/v1/conversations").json()["items"]
            assert [v["id"] for v in items] == [other]
        assert client.delete("/api/v1/conversations/" + cid).json()["state"] == "complete"
        with client.app.state.manager.store.transaction() as db:
            assert not db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            assert not db.execute(
                "SELECT 1 FROM ws_messages WHERE conversation=?", (cid,)
            ).fetchone()
        assert (index / "registry.sqlite").exists()


def test_single_artifact_deletion_preserves_chat_and_other_versions(corpus, tmp_path):
    index, agent, _ = corpus
    with client_for(WebOptions(index=index, state=tmp_path, agent=agent)) as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        job = send(client, cid)
        answer = wait(client, "/api/v1/jobs/" + job["job_id"], ready="succeeded")["answer"]
        first, export = artifact(client, cid, answer["answer_id"])
        second, _ = artifact(client, cid, answer["answer_id"])
        erased(client, client.delete("/api/v1/artifacts/" + first).json())
        assert client.get("/api/v1/artifacts/" + first).status_code == 404
        assert client.get("/api/v1/artifacts/" + second).status_code == 200
        assert client.get("/api/v1/queries/" + answer["answer_id"]).status_code == 200
        assert not (tmp_path / "studio" / first).exists()
        assert not (tmp_path / "studio/exports" / (export + ".md")).exists()
        assert (tmp_path / "agent" / job["job_id"]).exists()


def test_deletion_is_owner_checked_and_file_failures_are_retryable(corpus, tmp_path, monkeypatch):
    index, agent, _ = corpus
    with client_for(WebOptions(index=index, state=tmp_path, agent=agent)) as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        cookie = list(client.cookies.jar)
        client.cookies.clear()
        connect(client)
        assert client.delete("/api/v1/conversations/" + cid).status_code == 404
        client.cookies.clear()
        for item in cookie:
            client.cookies.jar.set_cookie(item)
        connect(client)
        job = send(client, cid)
        wait(client, "/api/v1/jobs/" + job["job_id"], ready="succeeded")
        deletion = client.app.state.workspace.deletions
        real_remove = deletion._remove

        def blocked(_):
            raise PermissionError("Simulated open file")

        monkeypatch.setattr(deletion, "_remove", blocked)
        response = client.delete("/api/v1/conversations/" + cid)
        assert response.status_code == 202
        assert response.json()["error_code"] == "DELETION_CLEANUP_PENDING"
        assert client.get("/api/v1/conversations/" + cid).status_code == 404
        monkeypatch.setattr(deletion, "_remove", real_remove)
        erased(client, response.json())


def test_running_question_stops_before_its_directory_is_removed(corpus, tmp_path):
    from fastapi.testclient import TestClient

    index, agent, _ = corpus
    running = threading.Event()

    class SlowRunner:
        def run(self, jid, payload, *, cancelled, progress):
            (tmp_path / "agent" / jid).mkdir(parents=True, exist_ok=True)
            running.set()
            while not cancelled():
                time.sleep(0.01)
            raise DocQAError("WEB_JOB_CANCELLED", "Cancelled")

    with TestClient(
        create_app(
            WebOptions(index=index, state=tmp_path, agent=agent),
            runner_factory=lambda job: SlowRunner(),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        connect(client)
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        job = send(client, cid)
        assert running.wait(2)
        erased(client, client.delete("/api/v1/conversations/" + cid).json())
        assert not (tmp_path / "agent" / job["job_id"]).exists()
        assert client.app.state.manager.ready
