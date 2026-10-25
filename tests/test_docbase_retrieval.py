"""End-to-end DocBase mechanics on real text-only DOCX inputs.

HTTP tests use MockTransport; they do not establish actual model quality or prove
that an organization service implements the project's explicit HTTP contract.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from docx import Document

from docqa.doc2md.config import ParseOptions
from docqa.doc2md.pipeline import parse_document
from docqa.docbase import evaluation, retrieval
from docqa.docbase.config import DocBaseOptions, ServiceOptions
from docqa.docbase.evaluation import EvidenceTarget, eval_retrieval, metrics_for_hits
from docqa.docbase.pipeline import build_index
from docqa.docbase.providers import HttpReranker, make_providers
from docqa.docbase.retrieval import fuse_routes, query, resolve_chunk
from docqa.docbase.storage import load_generation, read_current
from docqa.domain.errors import DocQAError


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    directory = tmp_path_factory.mktemp("retrieval-corpus")
    input_directory = directory / "input"
    input_directory.mkdir()
    artifacts, document_ids = [], {}
    for marker, category, text in (
        ("ALPHA", "architecture", "Architecture 系统架构采用分层设计。"),
        ("BETA", "operations", "Heartbeat 心跳消息用于保活，超时后恢复。"),
        ("GAMMA", "operations", "Parse 文档解析转换为 Markdown 后再检索。"),
    ):
        document = Document()
        document.add_paragraph(f"{marker} common heartbeat. {text}")
        source = input_directory / f"{marker}.docx"
        document.save(source)
        result = parse_document(
            source,
            directory / "parsed",
            options=ParseOptions(
                allowed_roots=[input_directory],
                category=category,
                office_render="never",
                ocr_mode="disabled",
            ),
        )
        assert result.status == "complete"
        assert result.eligible_for_indexing
        artifacts.append(Path(result.artifact_dir))
        document_ids[marker] = result.doc_id
    options = DocBaseOptions(mode="demo", top_k=3)
    built = build_index(artifacts, directory / "knowledge", options=options)
    assert built.documents == 3
    assert built.chunks == 3
    with load_generation(Path(built.generation_dir)) as generation:
        chunks = {chunk.source_name.removesuffix(".docx"): chunk for chunk in generation.chunks}
    return SimpleNamespace(
        directory=directory,
        artifacts=artifacts,
        doc_ids=document_ids,
        options=options,
        built=built,
        index=directory / "knowledge",
        chunks=chunks,
    )


def target_for(chunk):
    span = chunk.source_spans[0]
    return EvidenceTarget(
        doc_id=chunk.doc_id,
        revision_id=chunk.revision_id,
        block_id=span.block_id,
        component=span.component,
        text_start_codepoint=span.text_start_codepoint,
        text_end_codepoint=span.text_end_codepoint,
    )


def write_evaluation(path, cases):
    path.write_text(
        json.dumps({"schema_version": "docqa.docbase-eval.v1", "cases": cases}), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("mode", ["dense", "bm25", "sparse", "rrf", "hybrid"])
def test_doc_id_hard_filter_precedes_route_top_n(dataset, mode):
    options = dataset.options.model_copy(update={"route_top_n": 1, "top_k": 1})
    initial = query(dataset.index, "common heartbeat", options=options, mode=mode)
    assert len(initial.hits) == 1
    selected = next(
        identity for identity in dataset.doc_ids.values() if identity != initial.hits[0].doc_id
    )
    result = query(
        dataset.index, "common heartbeat", options=options, mode=mode, doc_ids=[selected]
    )
    assert len(result.hits) == 1
    assert result.hits[0].doc_id == selected
    assert result.applied_filter == {"categories": None, "doc_ids": [selected]}
    assert result.synthetic is True
    assert result.production_ready is False


@pytest.mark.parametrize("mode", ["dense", "bm25", "sparse", "rrf", "hybrid"])
def test_category_hard_filter_precedes_route_top_n(dataset, mode):
    options = dataset.options.model_copy(update={"route_top_n": 1, "top_k": 1})
    initial = query(dataset.index, "common heartbeat", options=options, mode=mode)
    selected = "operations" if initial.hits[0].category == "architecture" else "architecture"
    result = query(
        dataset.index, "common heartbeat", options=options, mode=mode, categories=[selected]
    )
    assert len(result.hits) == 1
    assert result.hits[0].category == selected


def test_all_three_route_functions_receive_the_same_filtered_rows_before_ranking(
    dataset, monkeypatch
):
    expected_id = dataset.doc_ids["BETA"]
    with load_generation(Path(dataset.built.generation_dir)) as generation:
        expected_rows = {
            row for row, chunk in enumerate(generation.chunks) if chunk.doc_id == expected_id
        }
    calls = {}

    def spy(name, original):
        def wrapped(index, value, eligible, limit):
            calls[name] = set(eligible)
            assert limit == 1
            return original(index, value, eligible, limit)

        return wrapped

    for name in ("search_dense", "search_bm25", "search_sparse"):
        monkeypatch.setattr(retrieval, name, spy(name, getattr(retrieval, name)))
    result = query(
        dataset.index,
        "common heartbeat",
        options=dataset.options.model_copy(update={"route_top_n": 1}),
        categories=["operations"],
        doc_ids=[expected_id],
    )
    assert len(result.hits) == 1
    assert calls == {
        name: expected_rows for name in ("search_dense", "search_bm25", "search_sparse")
    }


class NeverCalled:
    def fingerprint(self):
        pytest.fail("A no-eligible-documents query consulted models")

    def encode(self, *args, **kwargs):
        pytest.fail("A no-eligible-documents query encoded text")

    def rerank(self, *args, **kwargs):
        pytest.fail("A no-eligible-documents query reranked text")


@pytest.mark.parametrize(
    "filters",
    [
        {"categories": []},
        {"categories": ["does-not-exist"]},
        {"doc_ids": []},
        {"doc_ids": ["not-a-member"]},
    ],
)
def test_no_eligible_documents_returns_empty_without_model_calls(dataset, filters):
    result = query(
        dataset.index,
        "question",
        options=dataset.options,
        providers=(NeverCalled(), NeverCalled(), NeverCalled()),
        **filters,
    )
    assert result.status == "empty"
    assert result.reason == "no_eligible_documents"
    assert result.hits == []
    assert result.candidate_count == 0


def test_category_and_document_filters_are_intersection(dataset):
    result = query(
        dataset.index,
        "common",
        options=dataset.options,
        categories=["operations"],
        doc_ids=[dataset.doc_ids["ALPHA"]],
        providers=(NeverCalled(), NeverCalled(), NeverCalled()),
    )
    assert result.status == "empty"


def test_rrf_uses_rank_instead_of_incompatible_scores_and_never_double_votes():
    chunks = [SimpleNamespace(chunk_id="a"), SimpleNamespace(chunk_id="b")]
    rows, scores, ranks, raw = fuse_routes(
        {"dense": [(0, 1000.0), (0, 999.0), (1, 0.2)], "bm25": [(1, 10.0), (0, 1.0)]},
        chunks,
        {"dense": 2.0, "bm25": 1.0},
        60,
    )
    assert rows == [0, 1]
    assert scores[0] == pytest.approx(2 / 61 + 1 / 62)
    assert scores[1] == pytest.approx(2 / 62 + 1 / 61)
    assert ranks == {0: {"dense": 1, "bm25": 2}, 1: {"dense": 2, "bm25": 1}}
    assert raw[0]["dense"] == 1000


class FailingEncoder:
    def __init__(self, original, *, mismatch=False):
        self.original = original
        self.mismatch = mismatch

    def fingerprint(self):
        value = self.original.fingerprint()
        if self.mismatch:
            value["revision"] = "different-query-weights"
        return value

    def encode(self, *args, **kwargs):
        raise DocQAError("MODEL_SERVICE_TIMEOUT", "simulated query service timeout", retryable=True)


def test_route_failure_is_strict_by_default_and_only_explicitly_degrades(dataset):
    dense, sparse, reranker = make_providers(dataset.options)
    services = (FailingEncoder(dense), sparse, reranker)
    with pytest.raises(DocQAError) as failure:
        query(dataset.index, "common heartbeat", options=dataset.options, providers=services)
    assert failure.value.code == "RETRIEVAL_FAILED"
    result = query(
        dataset.index,
        "common heartbeat",
        options=dataset.options,
        providers=services,
        allow_degraded=True,
    )
    assert result.status == "degraded"
    assert result.routes["dense"].state == "failed"
    assert result.routes["dense"].error_code == "MODEL_SERVICE_TIMEOUT"
    assert result.hits
    assert all("dense" not in hit.route_ranks for hit in result.hits)
    assert result.warnings


@pytest.mark.parametrize("route", ["dense", "sparse"])
def test_encoder_profile_mismatch_is_not_silently_treated_as_degraded(dataset, route):
    dense, sparse, reranker = make_providers(dataset.options)
    services = (
        FailingEncoder(dense, mismatch=True) if route == "dense" else dense,
        FailingEncoder(sparse, mismatch=True) if route == "sparse" else sparse,
        reranker,
    )
    with pytest.raises(DocQAError) as failure:
        query(
            dataset.index,
            "common",
            options=dataset.options,
            providers=services,
            allow_degraded=True,
        )
    assert failure.value.code == "PROFILE_MISMATCH"


def reranker_options():
    return ServiceOptions(
        endpoint="https://models.invalid/rerank",
        model="configured-cross-encoder",
        revision="reranker-v1",
        tokenizer_revision="tokenizer-v1",
        retries=0,
    )


def test_http_reranker_restores_shuffled_server_indices_before_final_ranking(dataset):
    def respond(request):
        texts = json.loads(request.content)["documents"]
        scores = [
            {
                "index": index,
                "relevance_score": 10 if "GAMMA" in text else (5 if "BETA" in text else 1),
            }
            for index, text in enumerate(texts)
        ]
        return httpx.Response(200, json={"results": list(reversed(scores))})

    dense, sparse, _ = make_providers(dataset.options)
    reranker = HttpReranker(reranker_options(), transport=httpx.MockTransport(respond))
    result = query(
        dataset.index,
        "common heartbeat",
        options=dataset.options,
        providers=(dense, sparse, reranker),
    )
    assert [hit.doc_id for hit in result.hits] == [
        dataset.doc_ids[name] for name in ("GAMMA", "BETA", "ALPHA")
    ]
    assert [hit.rerank_score for hit in result.hits] == [10, 5, 1]
    assert result.routes["reranker"].count == 3


def test_missing_http_rerank_scores_fail_strictly_and_report_explicit_rrf_fallback(dataset):
    reranker = HttpReranker(
        reranker_options(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{"index": 0, "relevance_score": 1.0}]}
            )
        ),
    )
    dense, sparse, _ = make_providers(dataset.options)
    services = (dense, sparse, reranker)
    with pytest.raises(DocQAError) as failure:
        query(dataset.index, "common heartbeat", options=dataset.options, providers=services)
    assert failure.value.code == "RERANK_FAILED"
    result = query(
        dataset.index,
        "common heartbeat",
        options=dataset.options,
        providers=services,
        allow_degraded=True,
    )
    expected = query(dataset.index, "common heartbeat", options=dataset.options, mode="rrf")
    assert result.status == "degraded"
    assert result.routes["reranker"].state == "failed"
    assert result.routes["reranker"].error_code == "MODEL_RESPONSE_INVALID"
    assert [hit.chunk_id for hit in result.hits] == [hit.chunk_id for hit in expected.hits]
    assert all(hit.rerank_score is None for hit in result.hits)


def test_http_raw_dense_vectors_are_normalized_before_persist_and_search(dataset, tmp_path):
    calls = []
    service = dict(
        model="configured-real-model-id",
        revision="weights-v1",
        tokenizer_revision="tokenizer-v1",
        retries=0,
    )
    options = DocBaseOptions(
        mode="http",
        top_k=3,
        dense=ServiceOptions(endpoint="https://models.invalid/dense", dimension=2, **service),
        sparse=ServiceOptions(endpoint="https://models.invalid/sparse", vocab_size=1000, **service),
        reranker=ServiceOptions(endpoint="https://models.invalid/rerank", **service),
    )

    def respond(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == "/dense":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [3.0, 4.0]}
                        for index in range(len(payload["input"]))
                    ]
                },
            )
        if request.url.path == "/sparse":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "lexical_weights": {"2": 1.0}}
                        for index in range(len(payload["input"]))
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": float(index)}
                    for index in range(len(payload["documents"]))
                ]
            },
        )

    services = make_providers(options, transport=httpx.MockTransport(respond))
    built = build_index(
        dataset.artifacts, tmp_path / "http-knowledge", options=options, providers=services
    )
    with load_generation(Path(built.generation_dir)) as generation:
        assert np.allclose(generation.vectors, [[0.6, 0.8]] * 3, atol=1e-6)
        assert np.allclose(np.linalg.norm(generation.vectors, axis=1), 1)
    result = query(
        Path(built.generation_dir), "common heartbeat", options=options, providers=services
    )
    assert result.status == "ok"
    assert len(result.hits) == 3
    assert {path for path, _ in calls} == {"/dense", "/sparse", "/rerank"}
    assert any(payload.get("input_type") == "query" for _, payload in calls)


def test_same_build_is_reused_and_resolve_returns_owned_source_package(dataset, tmp_path):
    root = tmp_path / "knowledge"
    first = build_index(dataset.artifacts, root, options=dataset.options)
    second = build_index(list(reversed(dataset.artifacts)), root, options=dataset.options)
    assert second.reused is True
    assert second.generation_id == first.generation_id
    assert second.publish_sequence == first.publish_sequence
    result = query(root, "ALPHA", options=dataset.options, doc_ids=[dataset.doc_ids["ALPHA"]])
    resolved = resolve_chunk(root, result.hits[0].chunk_id)
    assert resolved["generation_id"] == first.generation_id
    assert Path(resolved["snapshot_path"]).is_file()
    assert Path(resolved["markdown_path"]).is_file()
    assert any(
        parent.samefile(first.generation_dir) for parent in Path(resolved["artifact_dir"]).parents
    )
    with pytest.raises(DocQAError) as failure:
        resolve_chunk(root, "not-a-chunk")
    assert failure.value.code == "CHUNK_NOT_FOUND"


def test_incremental_merge_preserves_members_and_explicit_replace_removes_them(dataset, tmp_path):
    root = tmp_path / "knowledge"
    first = build_index(dataset.artifacts[:2], root, options=dataset.options)
    merged = build_index(dataset.artifacts[2:], root, options=dataset.options)
    assert first.documents == 2
    assert merged.documents == 3
    assert merged.publish_sequence == first.publish_sequence + 1
    replaced = build_index(dataset.artifacts[2:], root, options=dataset.options, replace=True)
    assert replaced.documents == 1
    assert replaced.publish_sequence == merged.publish_sequence + 1
    with load_generation(Path(merged.generation_dir)) as old_generation:
        assert len(old_generation.members) == 3
    assert read_current(root) == (replaced.generation_id, replaced.publish_sequence)


def test_failed_build_does_not_change_current_generation(dataset, tmp_path):
    root = tmp_path / "knowledge"
    first = build_index(dataset.artifacts[:1], root, options=dataset.options)
    dense, sparse, reranker = make_providers(dataset.options)
    with pytest.raises(DocQAError):
        build_index(
            dataset.artifacts,
            root,
            options=dataset.options,
            providers=(FailingEncoder(dense), sparse, reranker),
        )
    assert read_current(root) == (first.generation_id, first.publish_sequence)


def test_evidence_group_metrics_count_alternatives_once_and_use_first_relevant_rank(dataset):
    chunk_a, chunk_b, chunk_c = [dataset.chunks[name] for name in ("ALPHA", "BETA", "GAMMA")]
    hit = SimpleNamespace(doc_id=chunk_a.doc_id, source_spans=chunk_a.source_spans, final_rank=2)
    groups = [
        [target_for(chunk_a), target_for(chunk_a), target_for(chunk_b)],
        [target_for(chunk_c)],
    ]
    metrics = metrics_for_hits([hit, hit], groups)
    assert metrics == {
        "recall_at_k": 0.5,
        "hit_rate_at_k": 1.0,
        "mrr_at_k": 0.5,
        "complete_coverage_at_k": 0.0,
    }
    assert metrics_for_hits([], groups) == {
        "recall_at_k": 0.0,
        "hit_rate_at_k": 0.0,
        "mrr_at_k": 0.0,
        "complete_coverage_at_k": 0.0,
    }


def test_eval_all_baselines_preserve_failure_denominator_and_separate_unanswerable(
    dataset, tmp_path, monkeypatch
):
    target = target_for(dataset.chunks["ALPHA"]).model_dump()
    path = write_evaluation(
        tmp_path / "evaluation.json",
        [
            {
                "question_id": "positive-ok",
                "question": "common",
                "answerable": True,
                "categories": ["architecture"],
                "evidence_groups": [[target]],
            },
            {
                "question_id": "positive-failed",
                "question": "SIMULATED_FAILURE",
                "answerable": True,
                "evidence_groups": [[target]],
            },
            {
                "question_id": "negative-empty",
                "question": "unknown question",
                "answerable": False,
                "categories": ["no-such-category"],
            },
            {"question_id": "negative-with-hits", "question": "common", "answerable": False},
            {
                "question_id": "negative-failed",
                "question": "SIMULATED_FAILURE",
                "answerable": False,
            },
        ],
    )
    real_query = evaluation.query

    def flaky_query(index, question, **kwargs):
        if question == "SIMULATED_FAILURE":
            raise DocQAError("MODEL_SERVICE_UNAVAILABLE", "injected failure")
        return real_query(index, question, **kwargs)

    monkeypatch.setattr(evaluation, "query", flaky_query)
    report = eval_retrieval(dataset.index, path, options=dataset.options)
    assert report["generation_id"] == dataset.built.generation_id
    assert set(report["summaries"]) == {"dense", "bm25", "sparse", "rrf", "hybrid"}
    assert report["failed_calls"] == 10
    assert report["synthetic"] is True
    for summary in report["summaries"].values():
        assert summary["answerable_cases"] == 2
        assert summary["recall_at_k"] == 0.5
        assert summary["hit_rate_at_k"] == 0.5
        assert summary["mrr_at_k"] == 0.5
        assert summary["complete_coverage_at_k"] == 0.5
        assert summary["unanswerable_completed"] == 2
        assert summary["unanswerable_cases"] == 3
        assert summary["unanswerable_failures"] == 1
        assert summary["unanswerable_with_hits_rate"] == 0.5
        assert summary["failures"] == [
            {"question_id": "positive-failed", "code": "MODEL_SERVICE_UNAVAILABLE"},
            {"question_id": "negative-failed", "code": "MODEL_SERVICE_UNAVAILABLE"},
        ]


def test_eval_rejects_stale_source_ground_truth(dataset, tmp_path):
    target = target_for(dataset.chunks["ALPHA"]).model_dump()
    target["revision_id"] = "wrong-revision"
    path = write_evaluation(
        tmp_path / "stale.json",
        [
            {
                "question_id": "stale",
                "question": "common",
                "answerable": True,
                "evidence_groups": [[target]],
            }
        ],
    )
    with pytest.raises(DocQAError) as failure:
        eval_retrieval(dataset.index, path, options=dataset.options)
    assert failure.value.code == "EVALUATION_SOURCE_MISMATCH"


def test_evidence_overlap_alone_does_not_claim_full_fact_recall(dataset):
    chunk = dataset.chunks["ALPHA"]
    target = target_for(chunk)
    shortened_span = chunk.source_spans[0].model_copy(
        update={"text_end_codepoint": target.text_end_codepoint - 1}
    )
    hit = SimpleNamespace(doc_id=chunk.doc_id, source_spans=[shortened_span], final_rank=1)
    metrics = metrics_for_hits([hit], [[target]])
    assert math.isclose(metrics["recall_at_k"], 0.0)


def test_changed_reranker_version_is_query_bound_without_rebuilding_generation(dataset):
    dense, sparse, _ = make_providers(dataset.options)

    def response(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0}
                    for index in range(len(json.loads(request.content)["documents"]))
                ]
            },
        )

    first_reranker = HttpReranker(reranker_options(), transport=httpx.MockTransport(response))
    second_reranker = HttpReranker(
        reranker_options().model_copy(update={"revision": "reranker-v2"}),
        transport=httpx.MockTransport(response),
    )
    first = query(
        dataset.index, "common", options=dataset.options, providers=(dense, sparse, first_reranker)
    )
    second = query(
        dataset.index, "common", options=dataset.options, providers=(dense, sparse, second_reranker)
    )
    assert first.generation_id == second.generation_id == dataset.built.generation_id
    assert first.retrieval_profile_id != second.retrieval_profile_id


@pytest.mark.parametrize(
    "invalid",
    [
        {"categories": "operations"},
        {"doc_ids": "some-id"},
        {"categories": ("operations",)},
        {"categories": [""]},
        {"doc_ids": [1]},
        {"allow_degraded": "false"},
    ],
)
def test_invalid_filter_containers_and_degradation_flag_are_rejected(dataset, invalid):
    with pytest.raises(DocQAError) as failure:
        query(dataset.index, "common", options=dataset.options, **invalid)
    assert failure.value.code == "INVALID_REQUEST"


def test_eval_rejects_evidence_excluded_by_its_own_hard_filter(dataset, tmp_path):
    target = target_for(dataset.chunks["ALPHA"]).model_dump()
    path = write_evaluation(
        tmp_path / "filtered-out.json",
        [
            {
                "question_id": "impossible-filter",
                "question": "common",
                "answerable": True,
                "categories": ["operations"],
                "evidence_groups": [[target]],
            }
        ],
    )
    with pytest.raises(DocQAError) as failure:
        eval_retrieval(dataset.index, path, options=dataset.options)
    assert failure.value.code == "INVALID_EVALUATION_SET"


@pytest.mark.parametrize("component", ["table_row", "figure_ocr", "figure_vl"])
def test_evidence_requires_exact_original_table_row_or_figure(dataset, component):
    chunk = dataset.chunks["ALPHA"]
    target = target_for(chunk).model_dump()
    target["component"] = component
    if component == "table_row":
        span = chunk.source_spans[0].model_copy(
            update={
                "component": component,
                "table_row_start": 0,
                "table_row_end": 1,
            }
        )
        target.update(table_row_start=1, table_row_end=2)
    else:
        span = chunk.source_spans[0].model_copy(
            update={"component": component, "figure_id": "figure-A"}
        )
        target["figure_id"] = "figure-B"
    hit = SimpleNamespace(doc_id=chunk.doc_id, source_spans=[span], final_rank=1)
    assert metrics_for_hits([hit], [[EvidenceTarget.model_validate(target)]])["recall_at_k"] == 0
