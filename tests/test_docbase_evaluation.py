"""Metrics count evidence groups once and preserve table/figure source identity."""

from hashlib import sha256
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from docqa.docbase.evaluation import EvidenceTarget, metrics_for_hits
from docqa.docbase.pipeline import _dense_value, _sparse_value, encode_cached
from docqa.domain.documents import SourceLocator
from docqa.domain.errors import DocQAError
from docqa.domain.rag import SourceSpan


def target(block="b", **kwargs):
    return EvidenceTarget(
        doc_id="d",
        revision_id="r",
        block_id=block,
        text_start_codepoint=1,
        text_end_codepoint=3,
        **kwargs,
    )


def hit(rank, block="b", **kwargs):
    span = SourceSpan(
        block_id=block,
        text_start_codepoint=0,
        text_end_codepoint=5,
        source_text_sha256=sha256(b"12345").hexdigest(),
        markdown_start_byte=10,
        markdown_end_byte=20,
        locators=[
            SourceLocator(
                locator_type="docx_object",
                precision="exact_object",
                display_label="test",
                doc_id="d",
                revision_id="r",
            )
        ],
        **kwargs,
    )
    return SimpleNamespace(final_rank=rank, doc_id="d", source_spans=[span])


def test_group_duplicates_do_not_inflate_recall():
    metrics = metrics_for_hits([hit(2), hit(3)], [[target()], [target("other")]])
    assert metrics == {
        "recall_at_k": 0.5,
        "hit_rate_at_k": 1,
        "mrr_at_k": 0.5,
        "complete_coverage_at_k": 0,
    }


def test_different_table_rows_with_same_offsets_are_not_equivalent_evidence():
    expected = target(component="table_row", table_row_start=1, table_row_end=2)
    wrong = hit(1, component="table_row", table_row_start=2, table_row_end=3)
    assert metrics_for_hits([wrong], [[expected]])["recall_at_k"] == 0
    right = hit(2, component="table_row", table_row_start=1, table_row_end=2)
    assert metrics_for_hits([wrong, right], [[expected]])["mrr_at_k"] == 0.5


def test_different_figure_occurrences_cannot_satisfy_same_target():
    expected = target(component="figure_vl", figure_id="first")
    wrong = hit(1, component="figure_vl", figure_id="second", origin="vl_generated")
    assert metrics_for_hits([wrong], [[expected]])["recall_at_k"] == 0


def test_target_requires_specific_component_coordinates():
    with pytest.raises(ValidationError):
        target(component="table_row")
    with pytest.raises(ValidationError):
        target(component="figure_vl")
    with pytest.raises(ValueError):
        metrics_for_hits([], [])


def test_dense_cache_boundary_normalizes_raw_http_representations(tmp_path):
    class RawDense:
        def fingerprint(self):
            return {"dimension": 2}

        def encode(self, texts, input_type="document"):
            return [[3, 4] for _ in texts]

    first, hits = encode_cached(RawDense(), ["one", "one"], "dense", tmp_path, "v1", 2)
    assert first == [[0.6, 0.8], [0.6, 0.8]] and hits == 1
    second, hits = encode_cached(RawDense(), ["one"], "dense", tmp_path, "v1", 2)
    assert second == [[0.6, 0.8]] and hits == 1


def test_sparse_cache_boundary_rejects_empty_or_all_zero_vectors():
    with pytest.raises(ValueError):
        _sparse_value({}, 100)
    with pytest.raises(ValueError):
        _sparse_value({1: 0.0}, 100)
    assert _dense_value([3, 4], 2) == [0.6, 0.8]


def test_oversized_integer_in_cache_is_classified(tmp_path):
    import json

    from docqa.doc2md.common import digest

    class Dense:
        def fingerprint(self):
            return {"dimension": 2}

        def encode(self, texts, input_type="document"):
            return [[1, 0] for _ in texts]

    encode_cached(Dense(), ["text"], "dense", tmp_path, "v1", 1)
    file = next(tmp_path.rglob("*.json"))
    cache = json.loads(file.read_bytes())
    cache["payload"] = [10**1000, 0]
    cache["payload_sha256"] = digest(cache["payload"])
    file.write_text(json.dumps(cache), encoding="utf-8")
    with pytest.raises(DocQAError) as error:
        encode_cached(Dense(), ["text"], "dense", tmp_path, "v1", 1)
    assert error.value.code == "MODEL_CACHE_CORRUPT"
