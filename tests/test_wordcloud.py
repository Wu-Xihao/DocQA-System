"""Source-bound keyword artifacts, packing, exports and owned lifecycle; no model API."""

import io
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
import test_agent_integration as shared
from PIL import Image
from test_web_deletion import erased
from test_web_workspace import client_for, connect, send, wait

from docqa.domain.errors import DocQAError
from docqa.web.config import WebOptions
from docqa.web.models import ArtifactCreate
from docqa.web.studio import build_content, export_bytes
from docqa.web.wordcloud import build_wordcloud, keywords, layout


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def facts():
    return [
        {
            "id": "K1",
            "text": "Graph memory retrieves graph memory. 图记忆支持图记忆检索。",
            "sources": ["C0001"],
        },
        {
            "id": "K2",
            "text": "GRAPH memory connects graph nodes with evidence. 证据支持检索。",
            "sources": ["C0002"],
        },
        {"id": "K3", "text": "PRIVATE_UNCITED should never become a keyword", "sources": []},
    ]


def artifact():
    answer = SimpleNamespace(
        status="answered",
        synthetic=True,
        verified=False,
        trace=[],
        warnings=[],
        unanswered_topics=[],
        conflicts=[],
        claims=[
            SimpleNamespace(claim_id=f["id"], text=f["text"], kind="fact", topic_ids=["T1"])
            for f in facts()[:2]
        ],
        citations=[
            SimpleNamespace(
                citation_id=f["sources"][0],
                claim_ids=[f["id"]],
                source_name="synthetic.docx",
                quote=f["text"],
                locators=[],
            )
            for f in facts()[:2]
        ],
    )
    return {
        "kind": "wordcloud",
        "title": "Synthetic keyword cloud",
        "generation_id": "isolated",
        "content": build_content(answer, "wordcloud"),
    }


def test_keywords_casefold_count_chinese_and_keep_precise_sources():
    words = keywords(facts())
    by_text = {w["text"].casefold(): w for w in words}
    assert by_text["graph"]["weight"] == 4
    assert by_text["memory"]["weight"] == 3
    assert by_text["graph"]["sources"] == ["C0001", "C0002"]
    assert by_text["graph"]["claim_ids"] == ["K1", "K2"]
    assert any("\u3400" <= w["text"][0] <= "\u9fff" for w in words)
    assert not any(w["text"].casefold() in {"the", "with", "private_uncited"} for w in words)
    assert words == keywords(facts())


def test_empty_or_uncited_keywords_fail_clearly_and_input_is_bounded():
    from docqa.web.app import _status

    assert _status("ARTIFACT_KEYWORDS_MISSING") == 409
    for text, sources in [("the and this", ["C0001"]), ("Uncited technical facts", [])]:
        with pytest.raises(DocQAError) as exc:
            keywords([{"id": "K1", "text": text, "sources": sources}])
        assert exc.value.code == "ARTIFACT_KEYWORDS_MISSING"
    with pytest.raises(DocQAError) as exc:
        keywords([{"id": "K1", "text": "x" * 200001, "sources": ["C0001"]}])
    assert exc.value.code == "ARTIFACT_TOO_LARGE"


def test_layout_is_deterministic_inside_bounds_and_without_intersections():
    cloud = build_wordcloud(facts())
    result = cloud["wordcloud_layout"]
    assert result == layout(cloud["words"])
    positions = result["positions"]
    assert len(positions) == len(cloud["words"])
    for i, item in enumerate(positions):
        box = item["box"]
        assert box[0] >= 24 and box[1] >= 24
        assert box[2] <= result["width"] - 24 and box[3] <= result["height"] - 24
        for other in positions[i + 1 :]:
            b = other["box"]
            assert not (box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1])


def test_wordcloud_has_real_vector_png_and_source_bound_text_exports():
    value = artifact()
    svg, mime = export_bytes(value, "svg")
    root = ET.fromstring(svg)
    assert mime == "image/svg+xml" and b"<script" not in svg
    texts = root.findall("{http://www.w3.org/2000/svg}text")
    for word in value["content"]["words"]:
        assert any(t.text == word["text"] for t in texts)
    png, mime = export_bytes(value, "png")
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == "PNG" and image.size == (1100, 710)
        assert image.getextrema() != ((255, 255),) * 3
    md, _ = export_bytes(value, "md")
    assert "关键词词频" in md.decode()
    assert "[C0001]" in md.decode() and "[C0002]" in md.decode()
    raw, _ = export_bytes(value, "json")
    assert b'"wordcloud"' in raw


def test_wordcloud_cannot_turn_an_unverified_answer_into_evidence():
    with pytest.raises(DocQAError) as exc:
        build_content(
            SimpleNamespace(status="answered", claims=[1], synthetic=False, verified=False),
            "wordcloud",
        )
    assert exc.value.code == "ARTIFACT_EVIDENCE_MISSING"
    assert (
        ArtifactCreate(conversation_id="a" * 32, answer_id="b" * 32, kind="wordcloud").kind
        == "wordcloud"
    )


def test_owned_wordcloud_creation_export_and_backend_deletion(corpus, tmp_path):
    index, agent, _ = corpus
    with client_for(WebOptions(index=index, state=tmp_path, agent=agent)) as client:
        connect(client)
        assert client.get("/api/v1/workspace").json()["artifact_types"][-1] == "wordcloud"
        cid = client.post("/api/v1/conversations", json={}).json()["id"]
        question = send(client, cid)
        answer = wait(client, "/api/v1/jobs/" + question["job_id"], ready="succeeded")["answer"]
        response = client.post(
            "/api/v1/studio/jobs",
            json={"conversation_id": cid, "answer_id": answer["answer_id"], "kind": "wordcloud"},
        )
        assert response.status_code == 202, response.text
        record = wait(client, "/api/v1/studio/jobs/" + response.json()["id"])
        identity = record["artifact_id"]
        value = client.get("/api/v1/artifacts/" + identity).json()
        allowed = {c["citation_id"] for c in answer["citations"]}
        assert value["content"]["words"]
        assert all(set(w["sources"]) <= allowed and w["sources"] for w in value["content"]["words"])
        exports = []
        for fmt in ("svg", "png", "md", "json"):
            exported = client.post(
                f"/api/v1/artifacts/{identity}/exports", json={"format": fmt}
            ).json()
            wait(client, "/api/v1/exports/" + exported["id"])
            assert client.get(f"/api/v1/exports/{exported['id']}/content").status_code == 200
            exports.append(exported["id"])
        original_cookies = list(client.cookies.jar)
        client.cookies.clear()
        connect(client)
        assert client.get("/api/v1/artifacts/" + identity).status_code == 404
        client.cookies.clear()
        for cookie in original_cookies:
            client.cookies.jar.set_cookie(cookie)
        connect(client)
        erased(client, client.delete("/api/v1/artifacts/" + identity).json())
        assert not (tmp_path / "studio" / identity).exists()
        for eid in exports:
            assert client.get(f"/api/v1/exports/{eid}/content").status_code == 404
            assert not list((tmp_path / "studio/exports").glob(eid + ".*"))
        assert client.get("/api/v1/conversations/" + cid).status_code == 200
