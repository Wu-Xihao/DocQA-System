"""DashScope native joint embedding; one request supplies both recall representations.

HTTP response rows use text_index (legacy gateway) or index (new gateway).
No token text is hashed into a fake sparse ID, and no OpenAI endpoint is used.
"""

from __future__ import annotations

import math

from docqa.docbase.providers import (
    _HttpService,
    _invalid_response,
    _number,
    _ordered_rows,
    _truncated_finish,
    _validate_inputs,
)
from docqa.domain.errors import DocQAError


class DashScopeEmbeddingClient(_HttpService):
    role = "dense_sparse"
    protocols = {"dashscope.text-embedding"}

    def __init__(self, config, *, transport=None, deadline=None):
        super().__init__(config, transport=transport, deadline=deadline)
        if config.dimension is None or config.vocab_size is None:
            raise DocQAError("MODEL_NOT_CONFIGURED", "Joint embedding bounds must be configured")

    def fingerprint(self):
        return {
            **super().fingerprint(),
            "output_type": "dense&sparse",
            "response_contract": "dashscope.joint.v1",
            "text_type_policy": "document/query",
        }

    def _payload(self, fields):
        return {
            "model": self.config.model,
            "input": {"texts": fields["input"]},
            "parameters": {
                "text_type": fields["input_type"],
                "dimension": self.config.dimension,
                "output_type": "dense&sparse",
            },
        }

    def encode(self, texts, input_type="document"):
        values = _validate_inputs(texts, input_type, self.config)
        result = []
        # text-embedding-v3/v4 accept at most 10 texts per native request.
        size = min(self.config.batch_size, 10)
        for start in range(0, len(values), size):
            batch = values[start : start + size]
            response = self._post({"input": batch, "input_type": input_type})
            if not isinstance(response, dict):
                raise _invalid_response()
            if response.get("code"):
                raise DocQAError("MODEL_SERVICE_HTTP_ERROR", "DashScope returned a model error")
            if response.get("truncated", False) is not False or _truncated_finish(response):
                raise _invalid_response()
            output = response.get("output")
            if not isinstance(output, dict) or not isinstance(output.get("embeddings"), list):
                raise _invalid_response()
            normalized = []
            for row in output["embeddings"]:
                if not isinstance(row, dict):
                    raise _invalid_response()
                index = row.get("text_index", row.get("index"))
                if (
                    "text_index" in row
                    and "index" in row
                    and (type(row["index"]) is not int or row["index"] != row["text_index"])
                ):
                    raise _invalid_response()
                normalized.append({**row, "index": index})
            ordered = _ordered_rows({**output, "data": normalized}, "data", len(batch))
            for row in ordered:
                dense = row.get("embedding")
                if not isinstance(dense, list) or len(dense) != self.config.dimension:
                    raise _invalid_response()
                dense = [_number(value) for value in dense]
                norm = math.hypot(*dense)
                if not norm or not math.isfinite(norm):
                    raise _invalid_response()
                entries = row.get("sparse_embedding")
                if not isinstance(entries, list) or not entries:
                    raise _invalid_response()
                sparse, seen = {}, set()
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise _invalid_response()
                    token = entry.get("index")
                    if (
                        type(token) is not int
                        or not 0 <= token < self.config.vocab_size
                        or token in seen
                    ):
                        raise _invalid_response()
                    weight = _number(entry.get("value"))
                    if weight < 0:
                        raise _invalid_response()
                    seen.add(token)
                    if weight:
                        sparse[token] = weight
                if not sparse:
                    raise _invalid_response()
                result.append({"dense": dense, "sparse": sparse})
        return result


class DashScopeView:
    """Role-specific profiles for the existing fixed-generation storage contracts."""

    def __init__(self, encoder, role):
        self.encoder, self.role = encoder, role

    def fingerprint(self):
        value = {**self.encoder.fingerprint(), "role": self.role}
        if self.role == "dense":
            value["normalization"] = "client_l2_then_float32"
        return value

    def encode(self, texts, input_type="document"):
        return [value[self.role] for value in self.encoder.encode(texts, input_type)]


def shared_encoder(dense, sparse):
    """Only a factory-created shared pair is eligible for request coalescing."""
    if isinstance(dense, DashScopeView) and isinstance(sparse, DashScopeView):
        if dense.role == "dense" and sparse.role == "sparse" and dense.encoder is sparse.encoder:
            return dense.encoder
    return None
