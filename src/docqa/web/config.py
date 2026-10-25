"""Server-owned paths and bounded, loopback-only web configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from docqa.agent.config import AgentOptions
from docqa.doc2md.security import is_within
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError
from docqa.service.scanner import canonical_path


class WebOptions(Contract):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    index: Path = Path("runtime/docbase")
    state: Path = Path("runtime/web")
    service_state: Path | None = None
    app_name: str = Field(default="DocQA 文档工作台", min_length=1, max_length=80)
    backend: Literal["builtin"] = "builtin"
    max_pending_jobs: int = Field(default=20, ge=1, le=1000)
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_asset_bytes: int = Field(default=33554432, ge=1024, le=268435456)
    runner_timeout_seconds: float = Field(default=150, gt=0, le=3600, allow_inf_nan=False)
    agent: AgentOptions = Field(default_factory=AgentOptions)

    @model_validator(mode="after")
    def separate_paths(self):
        state, index = self.state.absolute(), self.index.absolute()
        if is_within(state, index) or is_within(index, state):
            raise ValueError("Web state and knowledge index must be separate")
        return self


def load_options(path: Path | str | None) -> WebOptions:
    try:
        base = Path.cwd()
        value = {}
        if path is not None:
            selected = Path(path).resolve()
            base = selected.parent
            if selected.stat().st_size > 1048576:
                raise ValueError("Configuration exceeds 1 MiB")
            value = yaml.safe_load(selected.read_text(encoding="utf-8"))
            if value is None:
                value = {}
        if not isinstance(value, dict):
            raise ValueError("Expected a configuration object")
        for key in ("state", "index", "service_state"):
            if value.get(key) is not None:
                source = Path(value[key])
                value[key] = canonical_path(
                    base / source if not source.is_absolute() else source, must_exist=False
                )
        options = WebOptions.model_validate(value)
        options.state = canonical_path(options.state, must_exist=False)
        options.index = canonical_path(options.index, must_exist=False)
        options.separate_paths()
        return options
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        raise DocQAError("INVALID_CONFIG", "Cannot load Web configuration") from exc
