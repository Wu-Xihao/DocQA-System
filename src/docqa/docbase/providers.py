"""Explicit HTTP contracts and clearly labelled deterministic demo providers.

The sparse contract carries BGE-M3 lexical token weights, not ordinary words or
BM25 scores. An actual server must adapt its protocol to this documented contract.
No model is downloaded and failed HTTP services never fall back to the demo.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from docqa.docbase.config import DocBaseOptions, ServiceOptions
from docqa.domain.errors import DocQAError

InputType = Literal["document", "query"]


class DenseClient(Protocol):
    def encode(self, texts: list[str], input_type: InputType) -> list[list[float]]: ...
    def fingerprint(self) -> dict: ...


class SparseClient(Protocol):
    def encode(self, texts: list[str], input_type: InputType) -> list[dict[int, float]]: ...
    def fingerprint(self) -> dict: ...


class Reranker(Protocol):
    def rerank(self, question: str, texts: list[str]) -> list[float]: ...
    def fingerprint(self) -> dict: ...


def _invalid_response():
    return DocQAError("MODEL_RESPONSE_INVALID", "Model response violates the configured contract")


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_response()
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _invalid_response() from exc
    if not math.isfinite(number):
        raise _invalid_response()
    return number


def _validate_inputs(texts, input_type, config):
    if not isinstance(input_type, str) or input_type not in {"document", "query"}:
        raise DocQAError("INVALID_INPUT", "input_type must be document or query")
    if not isinstance(texts, list):
        raise DocQAError("INVALID_INPUT", "Model input must be a list of text strings")
    prefix = config.document_prefix if input_type == "document" else config.query_prefix
    result = []
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise DocQAError("INVALID_INPUT", "Model input contains empty or non-text content")
        value = prefix + text
        try:
            byte_count = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise DocQAError("INVALID_INPUT", "Model input is not valid UTF-8 text") from exc
        if byte_count > config.max_input_utf8_bytes:
            raise DocQAError(
                "MODEL_INPUT_TOO_LARGE", "Model input exceeds the configured UTF-8 limit"
            )
        result.append(value)
    return result


def _ordered_rows(value, key, count):
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        raise _invalid_response()
    rows = value[key]
    if len(rows) != count:
        raise _invalid_response()
    result = [None] * count
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid_response()
        index = row.get("index")
        if type(index) is not int or not 0 <= index < count or result[index] is not None:
            raise _invalid_response()
        if row.get("truncated", False) is not False or _truncated_finish(row):
            raise _invalid_response()
        result[index] = row
    if any(row is None for row in result):
        raise _invalid_response()
    if value.get("truncated", False) is not False or _truncated_finish(value):
        raise _invalid_response()
    return result


def _truncated_finish(value):
    reason = value.get("finish_reason")
    if reason is not None and not isinstance(reason, str):
        raise _invalid_response()
    return reason in {
        "length",
        "max_tokens",
        "truncated",
        "token_limit",
        "max_length",
    }


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _validate_endpoint(value):
    try:
        url = urlsplit(value)
        value.encode("utf-8")
        httpx.URL(value)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and not any(character.isspace() for character in value)
        )
        _ = url.port
    except (ValueError, UnicodeError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise DocQAError(
            "INVALID_CONFIG", "A full HTTP endpoint without embedded credentials is required"
        )


class _HttpService:
    role: str

    def __init__(self, config: ServiceOptions, *, transport: httpx.BaseTransport | None = None):
        self.config = config.model_copy(deep=True)
        self.transport = transport
        _validate_endpoint(config.endpoint)
        if not all(
            value.strip() for value in (config.model, config.revision, config.tokenizer_revision)
        ):
            raise DocQAError(
                "MODEL_NOT_CONFIGURED", "Model, revision and tokenizer_revision must be configured"
            )

    def fingerprint(self):
        config = self.config
        value = {
            "protocol": "docbase.http.v1",
            "role": self.role,
            "synthetic": False,
            "endpoint_sha256": hashlib.sha256(config.endpoint.encode()).hexdigest(),
            "model": config.model,
            "revision": config.revision,
            "tokenizer_revision": config.tokenizer_revision,
            "dimension": config.dimension,
            "vocab_size": config.vocab_size,
            "document_prefix": config.document_prefix,
            "query_prefix": config.query_prefix,
            "max_input_utf8_bytes": config.max_input_utf8_bytes,
        }
        if self.role == "dense":
            value["normalization"] = "client_l2_then_float32"
        return value

    def _post(self, fields):
        config = self.config
        headers = {"Accept": "application/json"}
        if config.api_key_env:
            secret = os.environ.get(config.api_key_env)
            if not secret or not secret.strip():
                raise DocQAError(
                    "MODEL_CREDENTIAL_MISSING", "Configured model credential is unavailable"
                )
            headers["Authorization"] = "Bearer " + secret
        payload = {
            "model": config.model,
            "revision": config.revision,
            "tokenizer_revision": config.tokenizer_revision,
            **fields,
        }
        for attempt in range(config.retries + 1):
            try:
                with httpx.Client(
                    transport=self.transport,
                    timeout=config.timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        "POST", config.endpoint, json=payload, headers=headers
                    ) as response:
                        if response.status_code in {408, 429, 500, 502, 503, 504}:
                            if attempt < config.retries:
                                time.sleep(min(0.1 * 2**attempt, 1))
                                continue
                            raise DocQAError(
                                "MODEL_SERVICE_UNAVAILABLE",
                                "Model service is temporarily unavailable",
                                retryable=True,
                            )
                        if response.status_code != 200:
                            raise DocQAError(
                                "MODEL_SERVICE_HTTP_ERROR",
                                "Model service returned an unsuccessful status",
                            )
                        data = bytearray()
                        for part in response.iter_bytes(chunk_size=65536):
                            data.extend(part)
                            if len(data) > config.max_response_bytes:
                                raise _invalid_response()
                        try:
                            result = json.loads(bytes(data), object_pairs_hook=_unique_json_object)
                        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                            raise _invalid_response() from exc
                        return result
            except httpx.TimeoutException as exc:
                if attempt == config.retries:
                    raise DocQAError(
                        "MODEL_SERVICE_TIMEOUT", "Model service timed out", retryable=True
                    ) from exc
            except httpx.TransportError as exc:
                if attempt == config.retries:
                    raise DocQAError(
                        "MODEL_SERVICE_UNAVAILABLE", "Cannot reach model service", retryable=True
                    ) from exc
            if attempt < config.retries:
                time.sleep(min(0.1 * 2**attempt, 1))
        raise AssertionError("bounded retry loop must return or raise")

    def _batches(self, values):
        size = self.config.batch_size
        for offset in range(0, len(values), size):
            yield values[offset : offset + size]


class HttpDenseClient(_HttpService):
    role = "dense"

    def __init__(self, config, *, transport=None):
        super().__init__(config, transport=transport)
        if config.dimension is None:
            raise DocQAError("MODEL_NOT_CONFIGURED", "Dense dimension must be configured")

    def encode(self, texts: list[str], input_type: InputType = "document") -> list[list[float]]:
        values = _validate_inputs(texts, input_type, self.config)
        result = []
        for batch in self._batches(values):
            response = self._post({"input": batch, "input_type": input_type, "truncate": False})
            for row in _ordered_rows(response, "data", len(batch)):
                embedding = row.get("embedding")
                if not isinstance(embedding, list) or len(embedding) != self.config.dimension:
                    raise _invalid_response()
                vector = [_number(value) for value in embedding]
                norm = math.hypot(*vector)
                if norm == 0 or not math.isfinite(norm):
                    raise _invalid_response()
                result.append(vector)
        return result


class HttpSparseClient(_HttpService):
    role = "sparse"

    def __init__(self, config, *, transport=None):
        super().__init__(config, transport=transport)
        if config.vocab_size is None:
            raise DocQAError("MODEL_NOT_CONFIGURED", "Sparse vocab_size must be configured")

    def encode(
        self, texts: list[str], input_type: InputType = "document"
    ) -> list[dict[int, float]]:
        values = _validate_inputs(texts, input_type, self.config)
        result = []
        for batch in self._batches(values):
            response = self._post(
                {"input": batch, "input_type": input_type, "return_sparse": True, "truncate": False}
            )
            for row in _ordered_rows(response, "data", len(batch)):
                weights = row.get("lexical_weights")
                if not isinstance(weights, dict) or not weights:
                    raise _invalid_response()
                vector = {}
                for key, value in weights.items():
                    if not isinstance(key, str) or not re.fullmatch(r"0|[1-9][0-9]{0,8}", key):
                        raise _invalid_response()
                    index = int(key)
                    weight = _number(value)
                    if index >= self.config.vocab_size or weight < 0:
                        raise _invalid_response()
                    if weight:
                        vector[index] = weight
                if not vector:
                    raise _invalid_response()
                result.append(vector)
        return result


class HttpReranker(_HttpService):
    role = "reranker"

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        question_values = _validate_inputs([question], "query", self.config)
        values = _validate_inputs(texts, "document", self.config)
        result = []
        for batch in self._batches(values):
            if any(
                len((question_values[0] + text).encode("utf-8")) > self.config.max_input_utf8_bytes
                for text in batch
            ):
                raise DocQAError(
                    "MODEL_INPUT_TOO_LARGE", "Reranker query/document pair exceeds UTF-8 limit"
                )
            response = self._post(
                {"query": question_values[0], "documents": batch, "truncate": False}
            )
            result.extend(
                _number(row.get("relevance_score"))
                for row in _ordered_rows(response, "results", len(batch))
            )
        return result


_CONCEPTS = (
    ("heartbeat", "心跳", "保活"),
    ("architecture", "架构", "系统组织"),
    ("retrieval", "检索", "召回", "查资料"),
    ("parse", "解析", "转换"),
    ("version", "版本", "修订"),
    ("timeout", "超时", "等待"),
    ("error", "错误", "失败"),
)


def _demo_features(text):
    """Synthetic multilingual concept/lexical features, never model embeddings."""
    lowered = text.casefold()
    features = {}
    for index, terms in enumerate(_CONCEPTS):
        if any(term in lowered for term in terms):
            features[f"concept:{index}"] = 3.0
    for term in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", lowered):
        features[f"literal:{term}"] = features.get(f"literal:{term}", 0.0) + 1.0
    if not features:
        features["literal:" + lowered] = 1.0
    return features


class _DemoService:
    role: str

    def __init__(self, config: ServiceOptions):
        self.config = config.model_copy(deep=True)

    def fingerprint(self):
        value = {
            "protocol": "docbase.demo.v1",
            "role": self.role,
            "synthetic": True,
            "model": "synthetic-concept-hash",
            "revision": "1",
            "tokenizer_revision": "demo-unicode-v1",
            "dimension": self.config.dimension or 128,
            "vocab_size": self.config.vocab_size or 65536,
            "document_prefix": self.config.document_prefix,
            "query_prefix": self.config.query_prefix,
            "max_input_utf8_bytes": self.config.max_input_utf8_bytes,
        }
        if self.role == "dense":
            value["normalization"] = "client_l2_then_float32"
        return value


class DemoDenseClient(_DemoService):
    role = "dense"

    def encode(self, texts: list[str], input_type: InputType = "document") -> list[list[float]]:
        values = _validate_inputs(texts, input_type, self.config)
        result = []
        dimension = self.config.dimension or 128
        for text in values:
            vector = [0.0] * dimension
            for feature, weight in _demo_features(text).items():
                digest = hashlib.sha256(feature.encode()).digest()
                vector[int.from_bytes(digest[:8]) % dimension] += weight
            norm = math.hypot(*vector)
            result.append([value / norm for value in vector])
        return result


class DemoSparseClient(_DemoService):
    role = "sparse"

    def encode(
        self, texts: list[str], input_type: InputType = "document"
    ) -> list[dict[int, float]]:
        values = _validate_inputs(texts, input_type, self.config)
        result = []
        vocab_size = self.config.vocab_size or 65536
        for text in values:
            vector = {}
            for feature, weight in _demo_features(text).items():
                index = int.from_bytes(hashlib.sha256(feature.encode()).digest()[:8]) % vocab_size
                vector[index] = vector.get(index, 0.0) + weight
            result.append(vector)
        return result


class DemoReranker(_DemoService):
    role = "reranker"

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        query = _demo_features(_validate_inputs([question], "query", self.config)[0])
        values = _validate_inputs(texts, "document", self.config)
        scores = []
        for text in values:
            document = _demo_features(text)
            numerator = sum(
                min(weight, document.get(feature, 0)) for feature, weight in query.items()
            )
            denominator = sum(query.values())
            scores.append(numerator / denominator)
        return scores


def make_providers(
    options: DocBaseOptions, *, transport=None
) -> tuple[DenseClient, SparseClient, Reranker]:
    """Fail closed in HTTP mode; demo must be an explicit user configuration."""
    if options.mode == "demo":
        return (
            DemoDenseClient(options.dense),
            DemoSparseClient(options.sparse),
            DemoReranker(options.reranker),
        )
    if any(
        not config.endpoint.strip() for config in (options.dense, options.sparse, options.reranker)
    ):
        raise DocQAError(
            "MODEL_NOT_CONFIGURED", "Configure dense, sparse and reranker service endpoints"
        )
    return (
        HttpDenseClient(options.dense, transport=transport),
        HttpSparseClient(options.sparse, transport=transport),
        HttpReranker(options.reranker, transport=transport),
    )
