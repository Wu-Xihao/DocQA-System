"""Bounded, explicit configuration for evidence-grounded answer orchestration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from docqa.docbase.config import DocBaseOptions
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError


class ChatServiceOptions(Contract):
    """A complete compatible chat-completions URL, never an inferred model URL."""

    endpoint: str = ""
    model: str = ""
    revision: str = ""
    api_key_env: str | None = None
    timeout: float = Field(default=30, gt=0, le=600, allow_inf_nan=False)
    retries: int = Field(default=2, ge=0, le=3)
    max_input_utf8_bytes: int = Field(default=131072, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=131072, ge=128, le=16_777_216)
    max_output_tokens: int = Field(default=4096, ge=128, le=32768)

    @field_validator("api_key_env")
    @classmethod
    def validate_environment_name(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must name an environment variable")
        return value


class AgentOptions(Contract):
    mode: Literal["http", "demo"] = "http"
    generator: ChatServiceOptions = Field(default_factory=ChatServiceOptions)
    verifier: ChatServiceOptions = Field(default_factory=ChatServiceOptions)
    retrieval: DocBaseOptions = Field(default_factory=DocBaseOptions)
    max_rounds: int = Field(default=3, ge=1, le=10)
    max_subqueries: int = Field(default=2, ge=1, le=2)
    max_queries: int = Field(default=6, ge=1, le=100)
    max_candidates: int = Field(default=100, ge=1, le=10000)
    rerank_top_n: int = Field(default=50, ge=1, le=10000)
    max_evidence_bytes: int = Field(default=24000, ge=1000, le=4_194_304)
    max_output_bytes: int = Field(default=12000, ge=256, le=1_048_576)
    deadline_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)
    session_ttl_seconds: int = Field(default=3600, ge=30, le=604800)
    max_session_queries: int = Field(default=6, ge=1, le=100)
    max_model_calls: int = Field(default=12, ge=1, le=100)
    max_finalize_attempts: int = Field(default=2, ge=2, le=2)
    allow_degraded: bool = False

    @model_validator(mode="after")
    def validate_budgets(self):
        if self.rerank_top_n > self.max_candidates:
            raise ValueError("rerank_top_n exceeds max_candidates")
        return self


def load_options(path: str | Path | None) -> AgentOptions:
    """Load at most 1 MiB of YAML; no credentials are read during configuration."""
    if path is None:
        return AgentOptions()
    try:
        config_path = Path(path)
        if config_path.stat().st_size > 1_048_576:
            raise ValueError("Configuration exceeds 1 MiB")
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError("Configuration root must be a mapping")
        return AgentOptions.model_validate(value)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise DocQAError("INVALID_CONFIG", "Cannot load Agent configuration; check fields") from exc
