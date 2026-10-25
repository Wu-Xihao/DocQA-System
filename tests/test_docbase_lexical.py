from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

import jieba
import pytest

from docqa.docbase.analyzer import Analyzer
from docqa.docbase.lexical import build_bm25, build_sparse, search_bm25, search_sparse
from docqa.domain.errors import DocQAError


@pytest.fixture(scope="module")
def analyzer():
    return Analyzer(terms=["分组转发"])


def test_analyzer_protects_identifiers_chinese_and_negation(analyzer):
    tokens = analyzer.analyze(
        "分组转发不能使用 SD5993 FE_HEART_Start --dry-run v1.2.3 C:\\data\\test.pdf"
    )
    assert "分组转发" in tokens and any("不" in token for token in tokens)
    assert {"sd5993", "fe_heart_start", "--dry-run", "v1.2.3", "c:\\data\\test.pdf"} <= set(tokens)
    assert analyzer.analyze("ＳＤ５９９３") == ["sd5993"]
    assert "，" not in analyzer.analyze("分组，转发")


def test_analyzer_profiles_bind_terms_and_dictionary():
    one, two = Analyzer(["自定义术语"]), Analyzer(["其他术语"])
    assert one.profile() != two.profile()
    assert one.profile()["dictionary_sha256"] == two.profile()["dictionary_sha256"]
    assert one.profile()["jieba_version"] == "0.42.1"
    assert one.profile()["hmm"] is False
    assert one.analyze("自定义术语") == ["自定义术语"]


def test_terminology_substrings_cannot_split_protected_identifiers():
    analyzer = Analyzer(["sd", "heart", "test"])
    assert analyzer.analyze("SD5993 FE_HEART_Start https://test.example/api") == [
        "sd5993",
        "fe_heart_start",
        "https://test.example/api",
    ]


def test_analyzer_does_not_silently_truncate_identifiers():
    with pytest.raises(DocQAError, match="identifier") as error:
        Analyzer(max_token_utf8_bytes=16).analyze("FE_" + "A" * 30)
    assert error.value.code == "TOKEN_TOO_LONG"


def test_analyzer_cache_shares_initialization_without_sharing_user_mutations(monkeypatch):
    initialized = []
    original = jieba.Tokenizer.initialize

    def record(self, *args, **kwargs):
        initialized.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(jieba.Tokenizer, "initialize", record)
    terms = ["缓存隔离专用词甲乙"]
    one = Analyzer(terms)
    two = Analyzer(tuple(terms))
    assert len(initialized) == 1
    assert one.analyze(terms[0]) == two.analyze(terms[0]) == [terms[0]]
    terms.append("调用者后续追加术语")
    mutable_profile = one.profile()
    mutable_profile["terms"].append("只修改返回对象")
    assert two.terms == ("缓存隔离专用词甲乙",)
    assert one.profile()["terms"] == two.profile()["terms"] == ["缓存隔离专用词甲乙"]
    with pytest.raises(AttributeError):
        one.terms = ("不能修改冻结词典",)
    assert not hasattr(one, "tokenizer")
    # A different term set must obtain another initialized snapshot and cannot
    # affect the already-created analyzers' dictionary/profile.
    three = Analyzer(["不同词典隔离专用词丙丁"])
    assert len(initialized) == 2
    assert three.profile()["terms"] != two.profile()["terms"]
    assert two.analyze("缓存隔离专用词甲乙") == ["缓存隔离专用词甲乙"]


def test_analyzer_concurrent_creation_initializes_one_frozen_snapshot(monkeypatch):
    original = jieba.Tokenizer.initialize
    initialized = []
    barrier = threading.Barrier(8)

    def record(self, *args, **kwargs):
        initialized.append(self)
        return original(self, *args, **kwargs)

    def worker(_):
        barrier.wait(timeout=10)
        analyzer = Analyzer(["并发冻结专用词戊己"])
        return analyzer.analyze("并发冻结专用词戊己 SD5993 不得删除"), analyzer.profile()

    monkeypatch.setattr(jieba.Tokenizer, "initialize", record)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))
    assert len(initialized) == 1
    assert all(result == results[0] for result in results)
    assert "并发冻结专用词戊己" in results[0][0]
    assert "sd5993" in results[0][0]


def test_analyzer_cache_isolated_by_actual_dictionary_bytes(tmp_path, monkeypatch):
    first_package = tmp_path / "first"
    second_package = tmp_path / "second"
    first_package.mkdir()
    second_package.mkdir()
    (first_package / "dict.txt").write_text("甲乙 100 n\n", encoding="utf-8")
    (second_package / "dict.txt").write_text("甲 100 n\n乙 100 n\n", encoding="utf-8")
    monkeypatch.setattr(jieba, "__file__", str(first_package / "__init__.py"))
    first = Analyzer()
    monkeypatch.setattr(jieba, "__file__", str(second_package / "__init__.py"))
    second = Analyzer()
    assert first.dictionary_sha256 != second.dictionary_sha256
    assert first.analyze("甲乙") == ["甲乙"]
    assert second.analyze("甲乙") == ["甲", "乙"]
    # Loading another package dictionary does not modify the first snapshot.
    assert first.analyze("甲乙") == ["甲乙"]


def test_bm25_hand_computed_corpus_statistics_and_query_uniqueness(analyzer):
    index = build_bm25(["a a b", "a c", "d"], analyzer, k1=1.2, b=0.75)
    scores = dict(search_bm25(index, ["a", "a"], None, 10))
    idf = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))
    assert index["doc_lengths"] == [3, 2, 1]
    assert scores[0] == pytest.approx(idf * 2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 3 / 2)))
    assert scores[1] == pytest.approx(idf)
    assert len(scores) == 2


def test_bm25_filter_precedes_limit_and_does_not_recalculate_idf(analyzer):
    index = build_bm25(["a a", "a", "a", "b"], analyzer)
    full = dict(search_bm25(index, ["a"], None, 10))
    scoped = search_bm25(index, ["a"], {2}, 1)
    assert scoped == [(2, full[2])]
    assert search_bm25(index, ["a"], {1, 2}, 2)[0][0] == 1
    assert search_bm25(index, ["not-in-index"], None, 10) == []
    assert search_bm25(index, ["a"], set(), 10) == []


def test_bm25_empty_corpus_and_empty_documents(analyzer):
    assert search_bm25(build_bm25([], analyzer), ["a"], None, 5) == []
    index = build_bm25(["", "!!!"], analyzer)
    assert index["row_count"] == 2 and index["doc_lengths"] == [0, 0]
    assert search_bm25(index, ["a"], None, 5) == []


@pytest.mark.parametrize("k1,b", [(0, 0.5), (float("nan"), 0.5), (1.2, -0.1), (1.2, 1.1)])
def test_bm25_rejects_invalid_formula_parameters(analyzer, k1, b):
    with pytest.raises(DocQAError):
        build_bm25([], analyzer, k1=k1, b=b)


def test_sparse_is_exact_learned_weight_inner_product_not_bm25():
    index = build_sparse([{9: 0.5, 80: 2.0}, {9: 2.0}, {}, {9: 0.5}])
    assert search_sparse(index, {9: 3, 80: 0.25}, None, 10) == [(1, 6.0), (0, 2.0), (3, 1.5)]
    assert search_sparse(index, {9: 3}, {0, 3}, 1) == [(0, 1.5)]
    assert search_sparse(index, {7: 100}, None, 10) == []
    assert index["nonzero_counts"] == [2, 1, 0, 1]
    assert build_sparse([{2: 0}])["postings"] == {}


@pytest.mark.parametrize(
    "vector",
    [
        {-1: 1},
        {True: 1},
        {2**31: 1},
        {1: -1},
        {1: float("inf")},
        {1: float("nan")},
        {1: True},
        {"1": 1},
    ],
)
def test_sparse_rejects_invalid_token_ids_and_weights(vector):
    with pytest.raises(DocQAError) as error:
        build_sparse([vector])
    assert error.value.code == "INVALID_SPARSE_VECTOR"


def test_sparse_overflow_is_not_a_valid_ranked_score():
    index = build_sparse([{1: 1e308}])
    with pytest.raises(DocQAError, match="overflowed"):
        search_sparse(index, {1: 1e308}, None, 3)


@pytest.mark.parametrize("eligible", [[-1], [2], [True], ["0"], [0, False]])
def test_routes_reject_invalid_eligible_rows(analyzer, eligible):
    with pytest.raises(DocQAError):
        search_bm25(build_bm25(["a"], analyzer), ["a"], eligible, 1)
    with pytest.raises(DocQAError):
        search_sparse(build_sparse([{1: 1}]), {1: 1}, eligible, 1)


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_routes_reject_invalid_limits(analyzer, limit):
    with pytest.raises(DocQAError):
        search_bm25(build_bm25([], analyzer), [], None, limit)
    with pytest.raises(DocQAError):
        search_sparse(build_sparse([]), {}, None, limit)
