"""Batching removes duplicate package scans without relaxing individual checks."""

import os
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_agent_integration as shared

from docqa.agent import evidence
from docqa.agent.pipeline import answer_question
from docqa.agent.sessions import SessionStore
from docqa.docbase.retrieval import query
from docqa.domain.errors import DocQAError


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def items(corpus):
    index, options, built = corpus
    found = query(index, "SD5993 心跳", options=options.retrieval)
    return evidence.build_evidence_items(index, built.generation_id, found.hits)


def test_batch_checks_multiple_items_with_one_fresh_scan_per_call(corpus, monkeypatch):
    selected = items(corpus)
    assert len(selected) > 1
    actual = evidence.load_generation
    calls = []
    package_reads = []
    package_loader = evidence._reference_package

    def package_once(artifact):
        package_reads.append(artifact)
        return package_loader(artifact)

    monkeypatch.setattr(evidence, "_reference_package", package_once)

    def counted(*args, **kwargs):
        calls.append(args)
        return actual(*args, **kwargs)

    monkeypatch.setattr(evidence, "load_generation", counted)
    references = evidence.revalidate_evidence_batch(corpus[0], selected)
    assert len(calls) == 1
    assert [value["text"] for value in references] == [item.text for item in selected]
    assert len(package_reads) == len({item.artifact_id for item in selected})
    for source in references:
        markdown = Path(source["markdown_path"]).read_bytes()
        assert (
            source["markdown_start_line"]
            == markdown[: source["markdown_start_byte"]].count(b"\n") + 1
        )
        assert (
            source["markdown_end_line"] == markdown[: source["markdown_end_byte"]].count(b"\n") + 1
        )
    evidence.revalidate_evidence_batch(corpus[0], selected)
    assert len(calls) == 2  # No cache crosses a phase/request boundary.


def test_batch_rejects_tampering_even_in_last_item(corpus):
    selected = items(corpus)
    selected[-1] = selected[-1].model_copy(
        update={"text": "forged", "text_sha256": sha256(b"forged").hexdigest()}
    )
    with pytest.raises(DocQAError) as error:
        evidence.revalidate_evidence_batch(corpus[0], selected)
    assert error.value.code == "EVIDENCE_SOURCE_INVALID"


def test_batch_rejects_mixed_generations_before_reading(corpus):
    selected = items(corpus)
    selected[-1] = selected[-1].model_copy(update={"generation_id": "f" * 32})
    with pytest.raises(DocQAError) as error:
        evidence.revalidate_evidence_batch(corpus[0], selected)
    assert error.value.code == "EVIDENCE_SOURCE_INVALID"


def test_saved_answer_citations_are_validated_together(corpus, tmp_path, monkeypatch):
    index, options, _ = corpus
    result = answer_question(
        index,
        "SD5993 心跳启动与超时恢复",
        output=tmp_path,
        options=options,
        required_topics=["SD5993 心跳如何启动？", "心跳超时如何恢复？"],
    )
    assert len(result.citations) > 1
    actual = evidence.load_generation
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return actual(*args, **kwargs)

    monkeypatch.setattr(evidence, "load_generation", counted)
    resolved = SessionStore(tmp_path).resolve_citations(result.answer_id)
    assert len(calls) == 1
    assert set(resolved) == {citation.citation_id for citation in result.citations}


def test_source_ir_cache_reuses_parse_but_detects_restored_timestamp_write(tmp_path, monkeypatch):
    from docx import Document

    from docqa.doc2md.config import ParseOptions
    from docqa.doc2md.pipeline import parse_document

    source = tmp_path / "synthetic.docx"
    document = Document()
    document.add_paragraph("Synthetic cache source; no external services.")
    document.save(source)
    parsed = parse_document(
        source, tmp_path / "parsed", options=ParseOptions(office_render="never")
    )
    artifact = Path(parsed.artifact_dir)
    generation = SimpleNamespace(
        directory=tmp_path,
        members=[
            {
                "artifact_id": parsed.artifact_id,
                "artifact_ref": artifact.relative_to(tmp_path).as_posix(),
            }
        ],
    )
    chunk = SimpleNamespace(artifact_id=parsed.artifact_id)
    from docqa import integrity

    # The freshly parsed fixture counts as settled, as published artifacts are.
    monkeypatch.setattr(integrity, "_racy", lambda change: False)
    evidence._SOURCE_IR.clear()
    original = evidence.DocumentIR.model_validate_json
    calls = []

    def counted(raw):
        calls.append(1)
        return original(raw)

    monkeypatch.setattr(evidence.DocumentIR, "model_validate_json", counted)
    _, first = evidence._source(generation, chunk)
    first.category = "caller mutation"
    _, second = evidence._source(generation, chunk)
    assert second.category == "default" and len(calls) == 1
    path = artifact / "document.ir.json"
    old = path.stat()
    raw = path.read_bytes()
    changed = raw.replace(b'"category": "default"', b'"category": "updated"')
    assert changed != raw and len(changed) == len(raw)
    monkeypatch.undo()
    monkeypatch.setattr(evidence.DocumentIR, "model_validate_json", counted)
    time.sleep(0.05)  # A settled file's next write always lands in a later clock tick.
    path.write_bytes(changed)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    _, third = evidence._source(generation, chunk)
    assert third.category == "updated" and len(calls) == 2
