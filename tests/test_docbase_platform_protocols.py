"""Explicit platform wire contracts; all requests use MockTransport."""

from __future__ import annotations

import hashlib
import json
import math

import httpx
import pytest
from pydantic import ValidationError

from docqa.docbase.config import DocBaseOptions, ServiceOptions
from docqa.docbase.providers import HttpDenseClient, HttpReranker, HttpSparseClient, make_providers
from docqa.domain.errors import DocQAError


def options(**changes):
    return ServiceOptions(
        **{
            "endpoint": "https://platform.invalid/endpoint",
            "model": "actual-model-id",
            "revision": "provider-managed-unpinned",
            "tokenizer_revision": "provider-managed-unpinned",
            "dimension": 2,
            "vocab_size": 1000,
            "retries": 0,
            **changes,
        }
    )


def response_transport(payload):
    # Raw bytes deliberately allow malformed non-finite JSON test inputs to
    # reach the client validator rather than HTTPX's response constructor.
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps(payload).encode())
    )


def test_openai_embeddings_send_only_supported_fields_and_preserve_batch_order():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert set(payload) == {"model", "input", "encoding_format", "dimensions"}
        assert payload["model"] == "actual-model-id"
        assert payload["dimensions"] == 2
        assert payload["encoding_format"] == "float"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"index": index, "embedding": [float(text[-1]), 1.0]}
                    for index, text in reversed(list(enumerate(payload["input"])))
                ],
            },
        )

    client = HttpDenseClient(
        options(protocol="openai.embeddings", query_prefix="query: ", batch_size=2),
        transport=httpx.MockTransport(respond),
    )
    assert client.encode(["one1", "two2", "three3"], "query") == [[1, 1], [2, 1], [3, 1]]
    assert [call["input"] for call in calls] == [["query: one1", "query: two2"], ["query: three3"]]


def test_gitee_rerank_requests_all_candidates_per_batch_and_restores_input_order():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert set(payload) == {"model", "query", "documents", "top_n"}
        assert payload["top_n"] == len(payload["documents"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": float(text[-1]) / 10}
                    for index, text in reversed(list(enumerate(payload["documents"])))
                ]
            },
        )

    client = HttpReranker(
        options(protocol="gitee.rerank", batch_size=2), transport=httpx.MockTransport(respond)
    )
    assert client.rerank("query", ["one1", "two2", "three3"]) == [0.1, 0.2, 0.3]
    assert [call["top_n"] for call in calls] == [2, 1]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [1]}]},
        {"data": [{"index": 0, "embedding": [0, 0]}]},
        {"data": [{"index": True, "embedding": [1, 1]}]},
        {"data": [{"index": 0, "embedding": [math.nan, 1]}]},
        {"data": [{"index": 0, "embedding": [1, 1], "truncated": True}]},
        {"data": [{"index": 0, "embedding": [1, 1]}], "finish_reason": "length"},
    ],
)
def test_openai_embedding_keeps_existing_strict_response_validation(payload):
    client = HttpDenseClient(
        options(protocol="openai.embeddings"), transport=response_transport(payload)
    )
    with pytest.raises(DocQAError) as exc:
        client.encode(["text"], "document")
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {"results": []},
        {"results": [{"index": 0, "relevance_score": 0.5}]},
        {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.6},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 2, "relevance_score": 0.6},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": True},
                {"index": 1, "relevance_score": 0.6},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": math.inf},
                {"index": 1, "relevance_score": 0.6},
            ]
        },
        {
            "results": [
                {"index": 0, "relevance_score": 0.5, "truncated": True},
                {"index": 1, "relevance_score": 0.6},
            ]
        },
    ],
)
def test_gitee_rerank_rejects_partial_duplicate_or_invalid_scores(payload):
    client = HttpReranker(options(protocol="gitee.rerank"), transport=response_transport(payload))
    with pytest.raises(DocQAError) as exc:
        client.rerank("question", ["first", "second"])
    assert exc.value.code == "MODEL_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "client_type,role",
    [(HttpDenseClient, "dense"), (HttpSparseClient, "sparse"), (HttpReranker, "reranker")],
)
def test_original_fingerprint_remains_exactly_compatible(client_type, role):
    config = options()
    expected = {
        "protocol": "docbase.http.v1",
        "role": role,
        "synthetic": False,
        "endpoint_sha256": hashlib.sha256(config.endpoint.encode()).hexdigest(),
        "model": config.model,
        "revision": config.revision,
        "tokenizer_revision": config.tokenizer_revision,
        "dimension": 2,
        "vocab_size": 1000,
        "document_prefix": "",
        "query_prefix": "",
        "max_input_utf8_bytes": 32768,
    }
    if role == "dense":
        expected["normalization"] = "client_l2_then_float32"
    assert client_type(config).fingerprint() == expected


@pytest.mark.parametrize(
    "client_type,protocol", [(HttpDenseClient, "openai.embeddings"), (HttpReranker, "gitee.rerank")]
)
def test_protocol_is_part_of_profile_and_remote_managed_revision_is_not_sent(client_type, protocol):
    legacy = client_type(options()).fingerprint()
    platform = client_type(options(protocol=protocol)).fingerprint()
    assert platform == {**legacy, "protocol": protocol}
    assert platform["revision"] == "provider-managed-unpinned"
    assert platform["tokenizer_revision"] == "provider-managed-unpinned"


@pytest.mark.parametrize(
    "client_type,protocol",
    [
        (HttpDenseClient, "gitee.rerank"),
        (HttpSparseClient, "openai.embeddings"),
        (HttpSparseClient, "gitee.rerank"),
        (HttpReranker, "openai.embeddings"),
    ],
)
def test_direct_clients_reject_protocol_for_a_different_role(client_type, protocol):
    with pytest.raises(DocQAError) as exc:
        client_type(options(protocol=protocol))
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize(
    "role,protocol",
    [
        ("dense", "gitee.rerank"),
        ("sparse", "openai.embeddings"),
        ("sparse", "gitee.rerank"),
        ("reranker", "openai.embeddings"),
    ],
)
def test_configuration_rejects_role_protocol_mismatch(role, protocol):
    with pytest.raises(ValidationError):
        DocBaseOptions(**{role: options(protocol=protocol)})


def test_no_protocol_guessing_based_on_endpoint_hostname():
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    client = HttpDenseClient(
        options(endpoint="https://ai.gitee.com/v1/embeddings"),
        transport=httpx.MockTransport(respond),
    )
    client.encode(["text"], "query")
    assert seen == [
        {
            "model": "actual-model-id",
            "revision": "provider-managed-unpinned",
            "tokenizer_revision": "provider-managed-unpinned",
            "input": ["text"],
            "input_type": "query",
            "truncate": False,
        }
    ]


@pytest.mark.parametrize(
    "client_type,protocol,invoke",
    [
        (HttpDenseClient, "openai.embeddings", lambda client: client.encode(["text"], "query")),
        (HttpReranker, "gitee.rerank", lambda client: client.rerank("question", ["text"])),
    ],
)
def test_platform_protocol_keeps_auth_header_only_and_does_not_follow_redirects(
    monkeypatch, client_type, protocol, invoke
):
    monkeypatch.setenv("PLATFORM_TEST_KEY", "header-only-private-value")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer header-only-private-value"
        assert b"header-only-private-value" not in request.content
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid/"})

    client = client_type(
        options(protocol=protocol, api_key_env="PLATFORM_TEST_KEY"),
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(DocQAError) as exc:
        invoke(client)
    assert exc.value.code == "MODEL_SERVICE_HTTP_ERROR"
    assert len(requests) == 1
    assert "private-value" not in str(exc.value)


def test_platform_factory_does_not_hide_a_missing_sparse_service_even_with_degradation():
    config = DocBaseOptions(
        dense=options(protocol="openai.embeddings"),
        reranker=options(protocol="gitee.rerank"),
        allow_degraded=True,
    )
    with pytest.raises(DocQAError) as exc:
        make_providers(
            config, transport=httpx.MockTransport(lambda request: pytest.fail("network"))
        )
    assert exc.value.code == "MODEL_NOT_CONFIGURED"
