"""Validated doc2md configuration; secrets stay in environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError


class VisionConfig(Contract):
    mode: Literal["disabled", "http"] = "disabled"
    endpoint: str = ""
    model: str = ""
    revision: str = ""
    api_key_env: str | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    enable_thinking: bool | None = None
    timeout: float = Field(default=60, gt=0, le=600)
    retries: int = Field(default=2, ge=0, le=3)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.mode == "http" and not all(
            (self.endpoint.strip(), self.model.strip(), self.revision.strip())
        ):
            raise ValueError("HTTP vision requires endpoint, model and revision")
        return self


class ParseOptions(Contract):
    allowed_roots: list[Path] = Field(default_factory=list)
    category: str = "default"
    max_file_mb: int = Field(default=100, ge=1, le=1024)
    max_pages: int = Field(default=1000, ge=1, le=10000)
    max_pixels: int = Field(default=20_000_000, ge=10000, le=100_000_000)
    max_uncompressed_mb: int = Field(default=1000, ge=1, le=5000)
    max_archive_members: int = Field(default=20000, ge=1, le=100000)
    render_dpi: int = Field(default=144, ge=72, le=300)
    render_pdf: bool = True
    office_render: Literal["auto", "never", "required"] = "auto"
    allow_legacy_office: bool = False
    office_executable: str = "soffice"
    ocr_mode: Literal["auto", "disabled", "required"] = "auto"
    tesseract_executable: str = "tesseract"
    tessdata_dir: str | None = None
    ocr_languages: str = "chi_sim+eng"
    external_timeout: float = Field(default=60, gt=0, le=600)
    analysis_revision: str = "1"
    vision: VisionConfig = Field(default_factory=VisionConfig)


def load_options(path: str | Path | None) -> ParseOptions:
    if path is None:
        return ParseOptions()
    config_path = Path(path).resolve()
    try:
        if config_path.stat().st_size > 1024 * 1024:
            raise DocQAError("INVALID_CONFIG", "Configuration exceeds 1 MiB")
        content = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if content is None:
            content = {}
        if not isinstance(content, dict):
            raise ValueError("Configuration root must be a mapping")
        options = ParseOptions.model_validate(content)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise DocQAError(
            "INVALID_CONFIG", "Cannot load doc2md configuration; check fields and paths"
        ) from exc
    options.allowed_roots = [
        (config_path.parent / root).resolve() if not root.is_absolute() else root.resolve()
        for root in options.allowed_roots
    ]
    return options
