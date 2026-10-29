"""Two-route capability binding across persisted indexes, Agent and HTTP."""

from __future__ import annotations

import json
import shutil
import time
from hashlib import sha256
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from docqa.agent.config import AgentOptions
from docqa.agent.pipeline import answer_question
from docqa.agent.retrieval import CandidateLedger
from docqa.agent.sessions import SessionStore
from docqa.doc2md.config import ParseOptions
from docqa.doc2md.pipeline import parse_document
from docqa.docbase.config import DocBaseOptions
from docqa.docbase.pipeline import build_index
from docqa.docbase.providers import make_providers
from docqa.docbase.retrieval import query
from docqa.docbase.storage import load_generation
from docqa.domain.errors import DocQAError
from docqa.web.app import create_app
from docqa.web.config import WebOptions


@pytest.fixture(scope="module")
def dual(tmp_path_factory):
    root = tmp_path_factory.mktemp("dual")
    source = root / "heart.docx"
    document = Document()
    document.add_paragraph("SD5993 心跳启动：调用 FE_HEART_Start 后发送心跳报文，等待对端确认。")
    document.save(source)
    artifact = parse_document(
        source,
        root / "parsed",
        options=ParseOptions(office_render="never", ocr_mode="disabled"),
    )
    assert artifact.status == "complete"
    retrieval = DocBaseOptions(mode="demo", recall_routes=["dense", "bm25"])
    index = root / "index"
    built = build_index(artifact.artifact_dir, index, options=retrieval)
    return index, AgentOptions(mode="demo", retrieval=retrieval), built, root


def test_two_route_build_has_no_sparse_encoder_cache_or_learned_postings(dual):
    index, options, built, _ = dual
    assert make_providers(options.retrieval)[1] is None
    assert "sparse" not in built.cache_hits
    assert not (index / "cache/sparse").exists()
    with load_generation(Path(built.generation_dir)) as generation:
        assert generation.profiles["sparse"] == {"enabled": False}
        assert generation.manifest["recall_routes"] == ["dense", "bm25"]
        assert generation.sparse["postings"] == {}
    result = query(index, "SD5993", options=options.retrieval)
    assert result.status == "ok" and result.hits
    assert set(result.routes) == {"dense", "bm25", "reranker"}


@pytest.mark.parametrize("filters", [{}, {"categories": []}])
def test_disabled_sparse_query_is_rejected_even_with_empty_filter(dual, filters):
    with pytest.raises(DocQAError) as error:
        query(dual[0], "SD5993", options=dual[1].retrieval, mode="sparse", **filters)
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("filters", [{}, {"categories": []}])
def test_route_mismatch_cannot_bypass_frozen_generation(dual, filters):
    with pytest.raises(DocQAError) as error:
        query(dual[0], "SD5993", options=DocBaseOptions(mode="demo"), **filters)
    assert error.value.code == "PROFILE_MISMATCH"


def test_agent_checks_route_identity_before_empty_scope(dual):
    with load_generation(Path(dual[2].generation_dir)) as generation:
        with pytest.raises(DocQAError) as error:
            CandidateLedger(
                generation,
                "SD5993",
                options=DocBaseOptions(mode="demo"),
                providers=make_providers(DocBaseOptions(mode="demo")),
                topic_order=["T1"],
                categories=[],
            )
    assert error.value.code == "PROFILE_MISMATCH"


def test_two_route_agent_publishes_recoverable_citations(dual, tmp_path):
    result = answer_question(dual[0], "SD5993", options=dual[1], output=tmp_path)
    assert result.status == "answered" and result.citations
    assert result.synthetic and not result.verified and not result.degraded
    source = SessionStore(tmp_path).resolve_citation(
        result.answer_id, result.citations[0].citation_id
    )
    assert source["text"] == result.citations[0].quote
    assert Path(source["snapshot_path"]).is_file()


def test_disabled_sparse_cannot_hide_learned_postings_in_a_rehashed_package(dual, tmp_path):
    target = tmp_path / "tampered"
    shutil.copytree(dual[2].generation_dir, target)
    path = target / "sparse/index.json"
    payload = json.loads(path.read_bytes())
    payload["postings"] = {"1": [[0, 0.5]]}
    payload["nonzero_counts"][0] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["sparse/index.json"] = {
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DocQAError) as error:
        load_generation(target)
    assert error.value.code == "GENERATION_CORRUPT"


def test_web_runs_actual_two_route_headless_worker_and_downloads_source(dual, tmp_path):
    options = WebOptions(index=dual[0], state=tmp_path, agent=dual[1])
    with TestClient(create_app(options), base_url="http://127.0.0.1:8765") as client:
        bootstrap = client.get("/api/v1/bootstrap")
        assert bootstrap.status_code == 200
        body = bootstrap.json()
        assert body["knowledge_base"]["recall_routes"] == ["dense", "bm25"]
        assert body["ready"]
        client.headers["X-DocQA-Token"] = body["csrf_token"]
        accepted = client.post("/api/v1/questions", json={"question": "SD5993"})
        assert accepted.status_code == 202
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = client.get("/api/v1/jobs/" + accepted.json()["job_id"]).json()
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Headless question did not finish")
        assert job["state"] == "succeeded", job
        assert job["answer"]["status"] == "answered"
        citation = job["answer"]["citations"][0]
        response = client.get(citation["resources"]["snapshot"])
        assert response.status_code == 200 and response.content.startswith(b"PK")
