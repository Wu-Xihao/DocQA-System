"""DocBase configuration. Templates may be inspected before services are configured."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError
from docqa.domain.rag import ChunkOptions


class ServiceOptions(Contract):
    protocol: Literal[
        "docbase.http.v1", "openai.embeddings", "gitee.rerank", "dashscope.text-embedding"
    ] = "docbase.http.v1"
    endpoint: str = ""
    endpoint_env: str | None = None
    model: str = ""
    revision: str = ""
    tokenizer_revision: str = ""
    dimension: int | None = Field(default=None, ge=1, le=65536)
    vocab_size: int | None = Field(default=None, ge=1, le=10_000_000)
    document_prefix: str = ""
    query_prefix: str = ""
    max_input_utf8_bytes: int = Field(default=32768, ge=16, le=16_777_216)
    batch_size: int = Field(default=16, ge=1, le=512)
    api_key_env: str | None = None
    timeout: float = Field(default=30, gt=0, le=600, allow_inf_nan=False)
    retries: int = Field(default=2, ge=0, le=3)
    max_response_bytes: int = Field(default=8_388_608, ge=128, le=67_108_864)

    @field_validator("api_key_env", "endpoint_env")
    @classmethod
    def validate_environment_name(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("Credential/endpoint fields must name an environment variable")
        return value


class DocBaseOptions(Contract):
    vector_backend: Literal["numpy", "sqlite_vec"] = "numpy"
    chunk: ChunkOptions = Field(default_factory=ChunkOptions)
    mode: Literal["http", "demo"] = "http"
    dense: ServiceOptions = Field(default_factory=ServiceOptions)
    sparse: ServiceOptions = Field(default_factory=ServiceOptions)
    reranker: ServiceOptions = Field(default_factory=ServiceOptions)
    recall_routes: list[Literal["dense", "bm25", "sparse"]] = Field(
        default_factory=lambda: ["dense", "bm25", "sparse"]
    )
    analyzer_terms: list[str] = Field(default_factory=list)
    route_top_n: int = Field(default=50, ge=1, le=10000)
    fusion_top_n: int = Field(default=50, ge=1, le=10000)
    top_k: int = Field(default=8, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1, le=10000)
    route_weights: dict[str, float] = Field(
        default_factory=lambda: {"dense": 1.0, "bm25": 1.0, "sparse": 1.0}
    )
    bm25_k1: float = Field(default=1.2, gt=0, le=10, allow_inf_nan=False)
    bm25_b: float = Field(default=0.75, ge=0, le=1, allow_inf_nan=False)
    allow_degraded: bool = False
    incomplete_policy: Literal["reject", "native_only", "validated_parts"] = "reject"

    @field_validator("recall_routes", mode="before")
    @classmethod
    def validate_routes(cls, values):
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("recall_routes must be an explicit list")
        if len(values) != len(set(values)) or set(values) not in (
            {"dense", "bm25"},
            {"dense", "bm25", "sparse"},
        ):
            raise ValueError("Choose dense+bm25 or dense+bm25+sparse recall")
        return [route for route in ("dense", "bm25", "sparse") if route in values]

    @field_validator("analyzer_terms")
    @classmethod
    def validate_terms(cls, values):
        if len(values) > 10000 or any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("Analyzer terms must be nonempty, bounded strings")
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_retrieval(self):
        protocols = {
            "dense": {"docbase.http.v1", "openai.embeddings", "dashscope.text-embedding"},
            "sparse": {"docbase.http.v1", "dashscope.text-embedding"},
            "reranker": {"docbase.http.v1", "gitee.rerank"},
        }
        if any(getattr(self, role).protocol not in allowed for role, allowed in protocols.items()):
            raise ValueError("Model protocol does not support the configured retrieval role")
        joint = [
            role
            for role in ("dense", "sparse")
            if getattr(self, role).protocol == "dashscope.text-embedding"
        ]
        if joint:
            if set(joint) != {"dense", "sparse"} or "sparse" not in self.recall_routes:
                raise ValueError("DashScope joint encoding requires dense+bm25+sparse recall")
            if self.dense != self.sparse:
                raise ValueError(
                    "DashScope dense/sparse must share the same complete configuration"
                )
            if self.dense.dimension is None or self.dense.vocab_size is None:
                raise ValueError("DashScope requires dimension and a sparse index upper bound")
        weight_routes = set(self.route_weights)
        if weight_routes != set(self.recall_routes) and weight_routes != {
            "dense",
            "bm25",
            "sparse",
        }:
            raise ValueError("route_weights must cover exactly the enabled recall routes")
        if any(not math.isfinite(value) or value <= 0 for value in self.route_weights.values()):
            raise ValueError("Route weights must be finite and positive")
        # A caller selecting only two routes can keep the original default
        # weights, but inactive routes never participate in identity or fusion.
        self.route_weights = {route: self.route_weights[route] for route in self.recall_routes}
        if self.top_k > self.fusion_top_n:
            raise ValueError("top_k must not exceed fusion_top_n")
        return self


def load_options(path: str | Path | None) -> DocBaseOptions:
    """Load a bounded YAML mapping; service connectivity is checked separately."""
    if path is None:
        return DocBaseOptions()
    try:
        config_path = Path(path)
        if config_path.stat().st_size > 1_048_576:
            raise ValueError("Configuration exceeds 1 MiB")
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("Configuration root must be a mapping")
        return DocBaseOptions.model_validate(value)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise DocQAError(
            "INVALID_CONFIG", "Cannot load DocBase configuration; check fields"
        ) from exc
