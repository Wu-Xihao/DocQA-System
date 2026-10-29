"""Preview is owned, bounded progress; it cannot masquerade as a final answer."""

import json
import threading
import time

import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient

from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.config import WebOptions


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_pending_source_preview_is_not_a_published_answer_and_is_owner_bound(corpus, tmp_path):
    started, release = threading.Event(), threading.Event()

    class PendingRunner:
        def run(self, job_id, request, *, cancelled, progress):
            directory = tmp_path / "agent" / job_id
            directory.mkdir(parents=True)
            (directory / "retrieval-preview.json").write_text(
                json.dumps(
                    [
                        {
                            "source_name": "Synthetic.docx",
                            "text": "Pending source evidence, not a final claim.",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            progress("composing")
            started.set()
            release.wait(10)
            raise DocQAError("MODEL_SERVICE_UNAVAILABLE", "Synthetic failure after preview")

    app = create_app(
        WebOptions(index=corpus[0], state=tmp_path, agent=corpus[1]),
        runner_factory=lambda _: PendingRunner(),
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            boot = client.get("/api/v1/bootstrap").json()
            client.headers["X-DocQA-Token"] = boot["csrf_token"]
            submitted = client.post("/api/v1/questions", json={"question": "SD5993"})
            assert submitted.status_code == 202
            job = submitted.json()["job_id"]
            assert started.wait(3)
            response = client.get("/api/v1/jobs/" + job).json()
            assert response["state"] == "running"
            assert response["answer"] is None
            assert response["retrieval_preview"][0]["source_name"] == "Synthetic.docx"
            other = TestClient(app, base_url="http://127.0.0.1:8765")
            token = other.get("/api/v1/bootstrap").json()["csrf_token"]
            assert (
                other.get("/api/v1/jobs/" + job, headers={"X-DocQA-Token": token}).status_code
                == 404
            )
            release.set()
            for _ in range(30):
                result = client.get("/api/v1/jobs/" + job).json()
                if result["state"] == "failed":
                    break
                time.sleep(0.02)
            assert result["state"] == "failed" and result["retrieval_preview"] == []
    finally:
        release.set()
