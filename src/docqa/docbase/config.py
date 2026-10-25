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
    endpoint: str = ""
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

    @field_validator("api_key_env")
    @classmethod
    def validate_environment_name(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must name an environment variable")
        return value


class DocBaseOptions(Contract):
    chunk: ChunkOptions = Field(default_factory=ChunkOptions)
    mode: Literal["http", "demo"] = "http"
    dense: ServiceOptions = Field(default_factory=ServiceOptions)
    sparse: ServiceOptions = Field(default_factory=ServiceOptions)
    reranker: ServiceOptions = Field(default_factory=ServiceOptions)
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

    @field_validator("analyzer_terms")
    @classmethod
    def validate_terms(cls, values):
        if len(values) > 10000 or any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("Analyzer terms must be nonempty, bounded strings")
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_retrieval(self):
        if set(self.route_weights) != {"dense", "bm25", "sparse"}:
            raise ValueError("route_weights requires dense, bm25 and sparse")
        if any(not math.isfinite(value) or value <= 0 for value in self.route_weights.values()):
            raise ValueError("Route weights must be finite and positive")
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
