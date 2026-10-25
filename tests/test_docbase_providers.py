"""DocBase provider contract tests; all HTTP calls use an in-process transport."""

from __future__ import annotations

import json
import math

import httpx
import pytest
from pydantic import ValidationError

from docqa.docbase.config import DocBaseOptions, ServiceOptions, load_options
from docqa.docbase.providers import (
    DemoDenseClient,
    DemoReranker,
    DemoSparseClient,
    HttpDenseClient,
    HttpReranker,
    HttpSparseClient,
    make_providers,
)
from docqa.domain.errors import DocQAError


def config(**values):
    return ServiceOptions(
        **{
            "endpoint": "https://models.invalid/dense",
            "model": "configured-model",
            "revision": "weights-sha256-123",
            "tokenizer_revision": "tokenizer-sha256-456",
            "dimension": 2,
            "vocab_size": 1000,
            "retries": 0,
            **values,
        }
    )


def transport_for(value):
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps(value).encode())
    )


def test_http_dense_preserves_index_order_across_batches_and_exact_prefix():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "configured-model"
        assert payload["revision"] == "weights-sha256-123"
        assert payload["tokenizer_revision"] == "tokenizer-sha256-456"
        assert payload["input_type"] == "query"
        assert payload["truncate"] is False
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(value[-1]), 1.0]}
                    for index, value in reversed(list(enumerate(payload["input"])))
                ]
            },
        )

    client = HttpDenseClient(
        config(query_prefix="query: ", batch_size=2), transport=httpx.MockTransport(respond)
    )
    assert client.encode(["item1", "item2", "item3"], "query") == [[1, 1], [2, 1], [3, 1]]
    assert [payload["input"] for payload in calls] == [
        ["query: item1", "query: item2"],
        ["query: item3"],
    ]


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        {"data": []},
        {"data": [{}]},
        {"data": [{"index": True, "embedding": [1, 1]}]},
        {"data": [{"index": -1, "embedding": [1, 1]}]},
        {"data": [{"index": 1, "embedding": [1, 1]}]},
        {"data": [{"index": 0, "embedding": [1]}]},
        {"data": [{"index": 0, "embedding": [1, 1, 1]}]},
        {"data": [{"index": 0, "embedding": [0, 0]}]},
        {"data": [{"index": 0, "embedding": [True, 1]}]},
        {"data": [{"index": 0, "embedding": ["1", 1]}]},
        {"data": [{"index": 0, "embedding": [math.nan, 1]}]},
        {"data": [{"index": 0, "embedding": [math.inf, 1]}]},
        {"data": [{"index": 0, "embedding": [1, 1], "truncated": True}]},
        {"data": [{"index": 0, "embedding": [1, 1], "finish_reason": "length"}]},
        {"data": [{"index": 0, "embedding": [1, 1]}], "truncated": True},
        {"data": [{"index": 0, "embedding": [1, 1]}], "finish_reason": "max_tokens"},
    ],
)
def test_dense_rejects_malformed_or_partial_response(value):
    client = HttpDenseClient(config(), transport=transport_for(value))
    with pytest.raises(DocQAError, match="contract") as exc:
        client.encode(["text"], "document")
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


def test_dense_rejects_duplicate_index_without_silently_losing_an_input():
    client = HttpDenseClient(
        config(),
        transport=transport_for(
            {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [2, 3]}]}
        ),
    )
    with pytest.raises(DocQAError) as exc:
        client.encode(["first", "second"], "document")
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


def test_sparse_returns_learned_token_id_weights_and_restores_order():
    client = HttpSparseClient(
        config(),
        transport=transport_for(
            {
                "data": [
                    {"index": 1, "lexical_weights": {"9": 1.5, "10": 0}},
                    {"index": 0, "lexical_weights": {"2": 0.2, "999": 0.7}},
                ]
            }
        ),
    )
    assert client.encode(["first", "second"], "document") == [{2: 0.2, 999: 0.7}, {9: 1.5}]


@pytest.mark.parametrize(
    "weights",
    [
        [],
        {},
        {"term": 1},
        {"1.0": 1},
        {"-1": 1},
        {"01": 1},
        {"1000": 1},
        {"2": -1},
        {"2": math.inf},
        {"2": True},
        {"2": "0.3"},
        {"2": 0},
    ],
)
def test_sparse_rejects_invalid_vocabulary_or_weights(weights):
    client = HttpSparseClient(
        config(), transport=transport_for({"data": [{"index": 0, "lexical_weights": weights}]})
    )
    with pytest.raises(DocQAError) as exc:
        client.encode(["test"], "query")
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


def test_reranker_restores_order_instead_of_confusing_returned_rank_with_index():
    client = HttpReranker(
        config(),
        transport=transport_for(
            {
                "results": [
                    {"index": 1, "relevance_score": -1.5},
                    {"index": 0, "relevance_score": 4.2},
                ]
            }
        ),
    )
    assert client.rerank("query", ["first", "second"]) == [4.2, -1.5]


def test_reranker_requires_all_candidates_not_a_server_top_n():
    client = HttpReranker(
        config(), transport=transport_for({"results": [{"index": 1, "relevance_score": 0.9}]})
    )
    with pytest.raises(DocQAError) as exc:
        client.rerank("query", ["first", "second"])
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "file:///tmp/model",
        "https://user:secret@models.invalid/dense",
        "https://models.invalid/dense?token=secret",
        "https://models.invalid/dense#token",
        "http://models.invalid:invalid/",
        "https://bad host/dense",
    ],
)
def test_service_rejects_credentials_in_endpoint_and_non_http_urls(endpoint):
    with pytest.raises(DocQAError) as exc:
        HttpDenseClient(config(endpoint=endpoint))
    assert exc.value.code == "INVALID_CONFIG"
    assert "secret" not in str(exc.value)


def test_fingerprint_is_secret_free_and_binds_semantically_relevant_configuration(monkeypatch):
    monkeypatch.setenv("DOCBASE_KEY", "never-print-this-value")
    baseline = HttpDenseClient(config(api_key_env="DOCBASE_KEY")).fingerprint()
    assert "never-print-this-value" not in json.dumps(baseline)
    assert "models.invalid" not in json.dumps(baseline)
    assert baseline["synthetic"] is False
    assert baseline["normalization"] == "client_l2_then_float32"
    for field, value in {
        "endpoint": "https://models.invalid/dense-v2",
        "model": "other",
        "revision": "2",
        "tokenizer_revision": "other-tokenizer",
        "dimension": 3,
        "query_prefix": "query: ",
        "document_prefix": "passage: ",
        "max_input_utf8_bytes": 1000,
    }.items():
        assert HttpDenseClient(config(**{field: value})).fingerprint() != baseline
    assert HttpDenseClient(config(api_key_env="ANOTHER_KEY_NAME")).fingerprint() == baseline


def test_missing_credentials_fails_without_making_a_request(monkeypatch):
    monkeypatch.delenv("DOCBASE_ABSENT", raising=False)
    client = HttpDenseClient(
        config(api_key_env="DOCBASE_ABSENT"),
        transport=httpx.MockTransport(lambda request: pytest.fail("network called")),
    )
    with pytest.raises(DocQAError) as exc:
        client.encode(["test"], "query")
    assert exc.value.code == "MODEL_CREDENTIAL_MISSING"


def test_authorization_header_is_the_only_location_receiving_the_secret(monkeypatch):
    monkeypatch.setenv("DOCBASE_KEY", "header-only-secret")

    def respond(request):
        assert request.headers["authorization"] == "Bearer header-only-secret"
        assert b"header-only-secret" not in request.content
        assert "header-only-secret" not in str(request.url)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    assert HttpDenseClient(
        config(api_key_env="DOCBASE_KEY"), transport=httpx.MockTransport(respond)
    ).encode(["text"], "query")


def test_service_does_not_follow_redirects_or_leak_redirect_body():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": "https://other.invalid/secret"}, text="private-server-body"
        )

    client = HttpDenseClient(config(), transport=httpx.MockTransport(respond))
    with pytest.raises(DocQAError) as exc:
        client.encode(["test"], "query")
    assert exc.value.code == "MODEL_SERVICE_HTTP_ERROR"
    assert "private-server-body" not in str(exc.value)
    assert len(calls) == 1


def test_retries_are_finite_and_recover_from_retryable_status(monkeypatch):
    monkeypatch.setattr("docqa.docbase.providers.time.sleep", lambda value: None)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, text="private-body")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    client = HttpDenseClient(config(retries=2), transport=httpx.MockTransport(respond))
    assert client.encode(["test"], "query") == [[1.0, 2.0]]
    assert len(calls) == 3


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.ReadTimeout, "MODEL_SERVICE_TIMEOUT"),
        (httpx.ConnectError, "MODEL_SERVICE_UNAVAILABLE"),
    ],
)
def test_timeout_and_connection_errors_are_classified_without_exception_details(
    monkeypatch, failure, code
):
    monkeypatch.setattr("docqa.docbase.providers.time.sleep", lambda value: None)
    calls = []

    def respond(request):
        calls.append(request)
        raise failure("secret-token-in-provider-error", request=request)

    with pytest.raises(DocQAError) as exc:
        HttpDenseClient(config(retries=1), transport=httpx.MockTransport(respond)).encode(
            ["test"], "query"
        )
    assert len(calls) == 2
    assert exc.value.code == code
    assert exc.value.retryable is True
    assert "secret-token" not in str(exc.value)


@pytest.mark.parametrize("content", [b"not-json", b'{"data":[],"data":[]}', b"x" * 200])
def test_response_json_is_bounded_and_duplicate_keys_are_rejected(content):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    with pytest.raises(DocQAError) as exc:
        HttpDenseClient(config(max_response_bytes=128), transport=transport).encode(
            ["test"], "query"
        )
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


def test_no_service_calls_for_empty_batch():
    transport = httpx.MockTransport(lambda request: pytest.fail("network called"))
    assert HttpDenseClient(config(), transport=transport).encode([], "query") == []
    assert HttpSparseClient(config(), transport=transport).encode([], "document") == []
    assert HttpReranker(config(), transport=transport).rerank("question", []) == []


def test_utf8_input_limit_includes_prefix_and_rejects_instead_of_truncating():
    client = HttpDenseClient(config(max_input_utf8_bytes=16, query_prefix="prefix:"))
    with pytest.raises(DocQAError) as exc:
        client.encode(["中文中文"], "query")
    assert exc.value.code == "MODEL_INPUT_TOO_LARGE"
    with pytest.raises(DocQAError) as exc:
        HttpReranker(config(max_input_utf8_bytes=16)).rerank("abcdefghij", ["klmnopq"])
    assert exc.value.code == "MODEL_INPUT_TOO_LARGE"


@pytest.mark.parametrize(
    "texts,input_type",
    [
        ([""], "query"),
        ([" "], "document"),
        ([1], "query"),
        ("test", "query"),
        (["test"], "unknown"),
        (["\ud800"], "query"),
    ],
)
def test_inputs_are_text_and_must_be_valid_utf8(texts, input_type):
    with pytest.raises(DocQAError) as exc:
        HttpDenseClient(config()).encode(texts, input_type)
    assert exc.value.code == "INVALID_INPUT"


def test_config_accepts_empty_http_template_but_factory_never_falls_back():
    options = DocBaseOptions()
    assert options.mode == "http"
    with pytest.raises(DocQAError) as exc:
        make_providers(options)
    assert exc.value.code == "MODEL_NOT_CONFIGURED"
    dense, sparse, reranker = make_providers(DocBaseOptions(mode="demo"))
    assert isinstance(dense, DemoDenseClient)
    assert isinstance(sparse, DemoSparseClient)
    assert isinstance(reranker, DemoReranker)
    assert all(provider.fingerprint()["synthetic"] for provider in (dense, sparse, reranker))
    assert dense.fingerprint()["normalization"] == "client_l2_then_float32"


def test_demo_is_deterministic_synthetic_and_has_reproducible_concept_similarity():
    dense, sparse, reranker = make_providers(DocBaseOptions(mode="demo"))
    documents = ["心跳消息用于保活", "文档解析转换为Markdown"]
    query = "heartbeat"
    vectors = dense.encode(documents, "document")
    assert vectors == dense.encode(documents, "document")
    assert all(math.isclose(math.hypot(*vector), 1) for vector in vectors)
    query_vector = dense.encode([query], "query")[0]
    scores = [sum(a * b for a, b in zip(query_vector, vector, strict=True)) for vector in vectors]
    assert scores[0] > scores[1]
    assert sparse.encode(documents, "document") == sparse.encode(documents, "document")
    assert reranker.rerank(query, documents)[0] > reranker.rerank(query, documents)[1]
    assert "synthetic" in json.dumps(dense.fingerprint())


@pytest.mark.parametrize(
    "value", ["[]", "false", "1", "unknown: value", "dense:\n  api_key: actual-secret"]
)
def test_config_loader_rejects_wrong_shape_unknown_fields_and_plaintext_keys(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(value, encoding="utf-8")
    with pytest.raises(DocQAError) as exc:
        load_options(path)
    assert exc.value.code == "INVALID_CONFIG"
    assert "actual-secret" not in str(exc.value)


def test_config_validates_fusion_and_normalizes_analyzer_terms():
    assert DocBaseOptions(analyzer_terms=["FE_HEART_Start", "SD5993", "SD5993"]).analyzer_terms == [
        "FE_HEART_Start",
        "SD5993",
    ]
    for value in (
        {"top_k": 60},
        {"route_weights": {"dense": 1}},
        {"route_weights": {"dense": math.nan, "bm25": 1, "sparse": 1}},
        {"analyzer_terms": [""]},
    ):
        with pytest.raises(ValidationError):
            DocBaseOptions(**value)
