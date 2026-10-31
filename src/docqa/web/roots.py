"""Browser-initiated authorization of Service source roots.

The Web binds to 127.0.0.1 and every mutation carries the CSRF token, so the
local user may extend ``allowed_source_roots`` without editing YAML by hand.
Guards: the directory must exist and be a real (non-link) folder, must not be a
drive root or system/profile folder, and must not overlap managed outputs.
The YAML is edited in place (comments kept), re-validated, and restored on
any failure. Only roots added here can be removed here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml
from fastapi import Request
from pydantic import Field

from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError
from docqa.service.config import load_options
from docqa.service.scanner import _within, canonical_path

KEY = "allowed_source_roots:"


class RootRequest(Contract):
    path: str = Field(min_length=1, max_length=4096)


def _protected() -> list[Path]:
    names = ["SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"]
    names += ["APPDATA", "LOCALAPPDATA"]
    paths = [os.environ.get(name) for name in names]
    return [Path(os.path.abspath(p)) for p in paths if p]


def check_root(path: str, managed: list[Path]) -> Path:
    root = canonical_path(path, directory=True)
    if root.parent == root:
        raise DocQAError("ROOT_TOO_BROAD", "Drive or filesystem roots cannot be authorized")
    home = Path(os.path.abspath(os.path.expanduser("~")))
    if root == home or _within(home, root):
        raise DocQAError("ROOT_TOO_BROAD", "The user profile folder itself cannot be authorized")
    for system in _protected():
        if _within(root, system) or _within(system, root):
            raise DocQAError("ROOT_PROTECTED", "System or application folders cannot be authorized")
    for output in managed:
        output = canonical_path(output, must_exist=False)
        if _within(root, output) or _within(output, root):
            raise DocQAError(
                "SOURCE_ROOT_OVERLAP", "Directory overlaps DocQA's managed state or index"
            )
    return root


def _rewrite(config: Path, transform) -> None:
    original = config.read_bytes()
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    lines = transform(lines)
    temporary = config.with_name(config.name + ".tmp")
    temporary.write_bytes(newline.join(lines).encode("utf-8"))
    os.replace(temporary, config)
    try:
        load_options(config)
    except DocQAError:
        config.write_bytes(original)
        raise


def _block(lines):
    """Return (key line index, end index) of the top-level root list."""
    start = next((i for i, line in enumerate(lines) if line.rstrip() == KEY), None)
    if start is None:
        return None, None
    end = start + 1
    while end < len(lines) and (lines[end].startswith("-") or lines[end].startswith("  -")):
        end += 1
    return start, end


def add_root(config: Path, root: Path) -> None:
    # A JSON string is a valid YAML double-quoted scalar and never line-folds.
    entry = "- " + json.dumps(str(root), ensure_ascii=False)

    def transform(lines):
        start, end = _block(lines)
        if start is None:
            return [KEY, entry, *lines]
        if lines[start + 1 : end] == [] and start + 1 < len(lines) and "[]" in lines[start]:
            raise DocQAError("INVALID_CONFIG", "Unsupported allowed_source_roots layout")
        return [*lines[:end], entry, *lines[end:]]

    if any(line.rstrip().startswith(KEY) and line.rstrip() != KEY for line in _read(config)):
        raise DocQAError("INVALID_CONFIG", "allowed_source_roots must be a YAML block list")
    _rewrite(config, transform)


def remove_root(config: Path, root: Path) -> None:
    def transform(lines):
        start, end = _block(lines)
        kept = []
        for index, line in enumerate(lines):
            if start is not None and start < index < end:
                value = yaml.safe_load(line.strip()[1:].strip() or "''")
                if isinstance(value, str) and _same(Path(value), root, config):
                    continue
            kept.append(line)
        return kept

    _rewrite(config, transform)


def _read(config: Path):
    return config.read_text(encoding="utf-8").splitlines()


def _same(value: Path, root: Path, config: Path) -> bool:
    value = value if value.is_absolute() else config.parent / value
    return os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(root))


def install(app, workspace):
    def config() -> Path:
        if not workspace.options.allow_root_authorization:
            raise DocQAError("ROOT_AUTHORIZATION_DISABLED", "Root authorization is disabled")
        if workspace.options.service_config is None:
            raise DocQAError(
                "ROOT_CONFIG_UNAVAILABLE", "Only a Service YAML configuration can be extended"
            )
        return Path(workspace.options.service_config)

    def managed():
        options = workspace.options
        return [p for p in (options.state, options.index, options.service_state) if p]

    @app.post("/api/v1/maintenance/roots", status_code=201)
    def authorize(value: RootRequest, request: Request):
        path = config()
        root = check_root(value.path, managed())
        current = load_options(path).allowed_source_roots
        if any(_within(root, canonical_path(p, must_exist=False)) for p in current):
            return {"path": str(root), "added": False}
        add_root(path, root)
        workspace.store.object_put(
            "authorized_root",
            request.state.client_id,
            {"id": os.path.normcase(str(root)), "path": str(root), "added_at": time.time()},
        )
        return {"path": str(root), "added": True}

    @app.delete("/api/v1/maintenance/roots")
    def revoke(value: RootRequest, request: Request):
        path = config()
        root = canonical_path(value.path, must_exist=False)
        record = workspace.store.object_get(
            "authorized_root", os.path.normcase(str(root)), request.state.client_id
        )
        service = workspace.service()
        if any(
            not s["removed"] and _within(Path(s["path"]), root) for s in service.list_sources()
        ):
            raise DocQAError(
                "ROOT_IN_USE", "Remove the monitored folders inside this directory first"
            )
        remove_root(path, Path(record["path"]))
        workspace.store.object_delete("authorized_root", record["id"], request.state.client_id)
        return {"path": record["path"], "removed": True}

    return lambda owner: {
        item["path"] for item in workspace.store.objects("authorized_root", owner, 1000)
    }
