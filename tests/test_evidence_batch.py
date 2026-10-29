"""Batching removes duplicate package scans without relaxing individual checks."""

from hashlib import sha256

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

    def counted(*args, **kwargs):
        calls.append(args)
        return actual(*args, **kwargs)

    monkeypatch.setattr(evidence, "load_generation", counted)
    references = evidence.revalidate_evidence_batch(corpus[0], selected)
    assert len(calls) == 1
    assert [value["text"] for value in references] == [item.text for item in selected]
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
