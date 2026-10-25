"""Explicit trust roots and bounded local maintenance configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, field_validator

from docqa.doc2md.config import ParseOptions
from docqa.docbase.config import DocBaseOptions
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError


def normalize_types(values):
    if isinstance(values, str):
        values = values.split(",")
    result = sorted({value.strip().lower().lstrip(".") for value in values})
    if not result or not set(result).issubset({"pdf", "docx", "pptx", "doc", "ppt"}):
        raise ValueError("Types must be a nonempty selection of pdf,docx,pptx,doc,ppt")
    return result


class ServiceOptions(Contract):
    allowed_source_roots: list[Path] = Field(default_factory=list)
    types: list[str] = Field(default_factory=lambda: ["pdf", "docx", "pptx"])
    settle_seconds: float = Field(default=3, ge=0, le=3600, allow_inf_nan=False)
    scan_interval_seconds: float = Field(default=1800, ge=1, le=604800, allow_inf_nan=False)
    missing_confirmations: int = Field(default=2, ge=2, le=100)
    max_files: int = Field(default=10000, ge=1, le=1000000)
    max_pending_jobs: int = Field(default=1000, ge=1, le=100000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=5, ge=0, le=3600, allow_inf_nan=False)
    max_jobs_per_tick: int = Field(default=100, ge=1, le=10000)
    parse: ParseOptions = Field(default_factory=ParseOptions)
    retrieval: DocBaseOptions = Field(default_factory=DocBaseOptions)

    @field_validator("types", mode="before")
    @classmethod
    def validate_types(cls, value):
        return normalize_types(value)


def load_options(path: str | Path | None) -> ServiceOptions:
    if path is None:
        return ServiceOptions()
    try:
        source = Path(path).resolve()
        if source.stat().st_size > 1048576:
            raise ValueError("Configuration exceeds 1 MiB")
        value = yaml.safe_load(source.read_text(encoding="utf-8"))
        if value is None:
            value = {}
        options = ServiceOptions.model_validate(value)
        options.allowed_source_roots = [
            (source.parent / root).absolute() if not root.is_absolute() else root.absolute()
            for root in options.allowed_source_roots
        ]
        return options
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise DocQAError("INVALID_CONFIG", "Cannot load Service configuration") from exc
