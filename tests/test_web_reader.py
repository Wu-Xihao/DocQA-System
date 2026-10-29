"""Fast display metadata is not evidence; original-content reads remain checked."""

import shutil

import pytest
import test_agent_integration as shared
from fastapi.testclient import TestClient

from docqa.docbase.storage import read_current
from docqa.web.app import create_app
from docqa.web.config import WebOptions


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_initial_lists_and_submission_are_fast_and_worker_validates(corpus, tmp_path, monkeypatch):
    import docqa.web.tasks as tasks
    import docqa.web.workspace as workspace

    def forbidden(*args, **kwargs):
        raise AssertionError("Full evidence loader must not run for display")

    with TestClient(
        create_app(WebOptions(index=corpus[0], state=tmp_path, agent=corpus[1])),
        base_url="http://127.0.0.1:8765",
    ) as client:
        monkeypatch.setattr(tasks, "load_generation", forbidden)
        monkeypatch.setattr(workspace, "load_generation", forbidden)
        boot = client.get("/api/v1/bootstrap")
        assert boot.status_code == 200
        assert boot.json()["knowledge_base"]["evidence_verified"] is False
        client.headers["X-DocQA-Token"] = boot.json()["csrf_token"]
        docs = client.get("/api/v1/documents")
        assert docs.status_code == 200 and docs.json()["documents"]
        # Enqueueing is now fast; the isolated answer worker still validates
        # the pinned package before model calls or publishing.
        response = client.post("/api/v1/questions", json={"question": "SD5993"})
        assert response.status_code == 202


def test_metadata_tamper_is_not_cached_as_valid(corpus, tmp_path):
    from docqa.domain.errors import DocQAError
    from docqa.web.catalog import display_catalog

    index = tmp_path / "copy"
    shutil.copytree(corpus[0], index)
    generation, _ = read_current(index)
    path = index / "generations" / generation / "members.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(DocQAError) as error:
        display_catalog(index, corpus[1].retrieval)
    assert error.value.code == "GENERATION_CORRUPT"


def test_original_reader_and_original_file_are_version_bound(corpus, tmp_path):
    with TestClient(
        create_app(WebOptions(index=corpus[0], state=tmp_path, agent=corpus[1])),
        base_url="http://127.0.0.1:8765",
    ) as client:
        boot = client.get("/api/v1/bootstrap").json()
        client.headers["X-DocQA-Token"] = boot["csrf_token"]
        doc = client.get("/api/v1/documents").json()["documents"][0]
        base = "/api/v1/documents/" + doc["doc_id"]
        preview = client.get(base + "/preview?limit=1")
        assert preview.status_code == 200
        data = preview.json()
        assert len(data["blocks"]) == 1 and data["source_name"] == doc["source_name"]
        assert data["next_offset"] == 1
        original = client.get(base + "/original?inline=true")
        assert original.status_code == 200 and original.content[:2] == b"PK"
        assert "attachment" in original.headers["content-disposition"]
        token = client.headers.pop("X-DocQA-Token")
        assert client.get(base + "/original/content").status_code == 200
        assert client.get(base + "/preview").status_code == 403
        client.headers["X-DocQA-Token"] = token
        markdown = client.get(base + "/content")
        assert markdown.status_code == 200 and "text/plain" in markdown.headers["content-type"]
        assert client.get("/api/v1/documents/not-in-version/original").status_code == 404


def test_reader_checks_selected_document_without_loading_broken_vectors(corpus, tmp_path):
    import json

    from docqa.docbase.storage import verify_generation
    from docqa.domain.errors import DocQAError
    from docqa.web.documents import document_package

    index = tmp_path / "copy"
    shutil.copytree(corpus[0], index)
    generation, _ = read_current(index)
    directory = index / "generations" / generation
    member = json.loads((directory / "members.json").read_bytes())[0]
    (directory / "dense/vectors.npy").write_bytes(b"broken unrelated search index")
    artifact, snapshot, _ = document_package(index, member["doc_id"], generation)
    assert snapshot.is_file() and (artifact / "document.md").is_file()
    with pytest.raises(DocQAError):
        verify_generation(directory)  # The same package still cannot be used to answer.


def test_reader_warm_cache_cannot_hide_edited_markdown(corpus, tmp_path):
    import json
    import os

    from docqa.domain.errors import DocQAError
    from docqa.web.documents import document_package

    index = tmp_path / "copy"
    shutil.copytree(corpus[0], index)
    generation, _ = read_current(index)
    directory = index / "generations" / generation
    member = json.loads((directory / "members.json").read_bytes())[0]
    artifact, _, _ = document_package(index, member["doc_id"], generation)
    path = artifact / "document.md"
    old = path.stat()
    raw = bytearray(path.read_bytes())
    raw[-2] ^= 1
    path.write_bytes(raw)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(DocQAError):
        document_package(index, member["doc_id"], generation)
