"""Offline, checksummed backups and conservative unreferenced Web-output cleanup."""

import json
import re
import shutil
import sqlite3
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from uuid import uuid4

import portalocker

from docqa.docbase.storage import _checksum, _io_path
from docqa.domain.errors import DocQAError


def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise DocQAError("SOURCE_ACCESS_DENIED", "Maintenance refuses linked paths")
    return path.resolve()


def files(root, *, journals=False):
    result = []
    io_root = _io_path(root)
    for path in io_root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise DocQAError("SOURCE_ACCESS_DENIED", "Backup/cleanup does not traverse links")
        if (
            path.is_file()
            and not path.name.endswith(".lock")
            and (journals or not path.name.endswith(("-wal", "-shm", "-journal")))
        ):
            result.append(root / path.relative_to(io_root))
    return sorted(result)


@contextmanager
def offline(service=None, web=None):
    with ExitStack() as stack:
        try:
            targets = []
            if service:
                targets.extend(
                    [
                        service / ".worker.lock",
                        service / "index/.docbase.lock",
                        service / ".mutation.lock",
                    ]
                )
            if web:
                targets.append(web / ".web-worker.lock")
            for path in targets:
                if path.parent.exists():
                    stack.enter_context(portalocker.Lock(_io_path(path), timeout=0))
            yield
        except portalocker.exceptions.LockException as error:
            raise DocQAError(
                "MAINTENANCE_BUSY", "Stop Web and Service before offline maintenance"
            ) from error


def signature(root):
    return [
        (p.relative_to(root).as_posix(), _io_path(p).stat().st_size, _io_path(p).stat().st_mtime_ns)
        for p in files(root, journals=True)
        if not p.name.endswith("-shm")
    ]


def backup(service, web, output):
    roots = {"service": safe_path(service), "web": safe_path(web)}
    output = safe_path(output)
    if output.exists() or any(
        not root.is_dir() or output.is_relative_to(root) or root.is_relative_to(output)
        for root in roots.values()
    ):
        raise DocQAError(
            "INVALID_OUTPUT",
            "Use existing separate state roots and a new external backup directory",
        )
    if roots["service"].is_relative_to(roots["web"]) or roots["web"].is_relative_to(
        roots["service"]
    ):
        raise DocQAError("INVALID_OUTPUT", "State roots must be separate")
    stage = output.with_name(output.name + ".staging-" + uuid4().hex)
    stage.mkdir(parents=True)
    try:
        with offline(**roots):
            before = {k: signature(root) for k, root in roots.items()}
            manifest = {
                "schema_version": "docqa.backup.v1",
                "created_at": time.time(),
                "roots": {k: str(v) for k, v in roots.items()},
                "files": {},
            }
            for kind, root in roots.items():
                for source in files(root):
                    if len(manifest["files"]) >= 100000:
                        raise DocQAError(
                            "BACKUP_LIMIT",
                            "Backup exceeds 100000 files; use separate operational snapshots",
                        )
                    relative = kind + "/" + source.relative_to(root).as_posix()
                    target = _io_path(stage / relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.suffix in {".sqlite", ".db"}:
                        with (
                            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
                            closing(sqlite3.connect(target)) as dst,
                        ):
                            src.backup(dst)
                    else:
                        shutil.copy2(_io_path(source), target)
                    manifest["files"][relative] = {
                        "size": target.stat().st_size,
                        "sha256": _checksum(target),
                    }
            if before != {k: signature(root) for k, root in roots.items()}:
                raise DocQAError(
                    "MAINTENANCE_BUSY", "Source changed during snapshot; backup discarded"
                )
            raw_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(raw_manifest) > 8 * 1024 * 1024:
                raise DocQAError("BACKUP_LIMIT", "Backup manifest exceeds 8 MiB")
            (stage / "manifest.json").write_bytes(raw_manifest)
            _io_path(stage).rename(_io_path(output))
        return {
            "backup": str(output),
            "files": len(manifest["files"]),
            "bytes": sum(v["size"] for v in manifest["files"].values()),
        }
    finally:
        if (
            stage.exists()
            and stage.parent == output.parent
            and stage.name.startswith(output.name + ".staging-")
        ):
            shutil.rmtree(_io_path(stage))


def verify_backup(bundle):
    bundle = safe_path(bundle)
    path = bundle / "manifest.json"
    if path.stat().st_size > 8 * 1024 * 1024:
        raise DocQAError("BACKUP_INVALID", "Backup manifest exceeds limit")
    manifest = json.loads(path.read_text("utf-8"))
    if (
        manifest.get("schema_version") != "docqa.backup.v1"
        or set(manifest.get("roots", {})) != {"service", "web"}
        or not isinstance(manifest.get("files"), dict)
        or len(manifest["files"]) > 100000
    ):
        raise DocQAError("BACKUP_INVALID", "Invalid backup manifest")
    for name, metadata in manifest["files"].items():
        parts = name.split("/")
        if (
            parts[0] not in {"service", "web"}
            or any(p in {"", ".", ".."} for p in parts)
            or "\\" in name
            or ":" in name
        ):
            raise DocQAError("BACKUP_INVALID", "Invalid backup member path")
        item = bundle.joinpath(*parts)
        if not item.resolve().is_relative_to(bundle) or any(
            p.is_symlink() or p.is_junction()
            for p in [item, *item.parents]
            if p.is_relative_to(bundle)
        ):
            raise DocQAError("BACKUP_INVALID", "Backup member traverses a link")
        if (
            _io_path(item).stat().st_size != metadata["size"]
            or _checksum(_io_path(item)) != metadata["sha256"]
        ):
            raise DocQAError("BACKUP_INVALID", "Backup checksum mismatch")
    actual = {
        p.relative_to(bundle).as_posix() for p in files(bundle) if p != bundle / "manifest.json"
    }
    if actual != set(manifest["files"]):
        raise DocQAError("BACKUP_INVALID", "Backup member list differs")
    return manifest


def restore(bundle, service, web):
    bundle = safe_path(bundle)
    manifest = verify_backup(bundle)
    roots = {"service": safe_path(service), "web": safe_path(web)}
    if roots["service"].is_relative_to(roots["web"]) or roots["web"].is_relative_to(
        roots["service"]
    ):
        raise DocQAError("INVALID_OUTPUT", "Restore roots must be separate")
    if any(
        root.exists() or root.resolve() != Path(manifest["roots"][kind]).resolve()
        for kind, root in roots.items()
    ):
        raise DocQAError(
            "INVALID_OUTPUT",
            "Restore only to original, currently absent state roots; never overwrite live data",
        )
    if any(bundle.is_relative_to(root) or root.is_relative_to(bundle) for root in roots.values()):
        raise DocQAError("INVALID_OUTPUT", "Restore targets overlap backup")
    stages = {
        kind: root.with_name(root.name + ".restore-" + uuid4().hex) for kind, root in roots.items()
    }
    published = []
    try:
        for kind, stage in stages.items():
            stage.mkdir(parents=True)
            for name in manifest["files"]:
                if name.startswith(kind + "/"):
                    relative = name[len(kind) + 1 :]
                    target = _io_path(stage / relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_io_path(bundle / name), target)
                    if _checksum(target) != manifest["files"][name]["sha256"]:
                        raise DocQAError("BACKUP_INVALID", "Source changed during restore")
        for kind, root in roots.items():
            _io_path(stages[kind]).rename(_io_path(root))
            published.append(kind)
        return {"restored": True, "files": len(manifest["files"])}
    except Exception:
        for kind in reversed(published):
            _io_path(roots[kind]).rename(_io_path(stages[kind]))
        raise
    finally:
        for kind, stage in stages.items():
            if (
                stage.exists()
                and stage.parent == roots[kind].parent
                and stage.name.startswith(roots[kind].name + ".restore-")
            ):
                shutil.rmtree(_io_path(stage))


def cleanup_web(state, *, apply=False, min_age_seconds=86400):
    state = safe_path(state)
    if min_age_seconds < 3600 or not (state / "questions.sqlite").is_file():
        raise DocQAError("INVALID_REQUEST", "Use a Web state and a minimum one-hour retention")
    with offline(web=state):
        with closing(
            sqlite3.connect((state / "questions.sqlite").as_uri() + "?mode=ro", uri=True)
        ) as db:
            jobs = {r[0] for r in db.execute("SELECT job_id FROM jobs")}
            objects = [
                (r[0], r[1], json.loads(r[2]))
                for r in db.execute("SELECT kind,id,payload FROM ws_objects")
            ]
        protected = {
            "agent": set(jobs),
            "runs": set(jobs),
            "studio": set(),
            "studio/exports": set(),
        }
        for kind, identity, value in objects:
            if kind == "artifact":
                protected["studio"].add(identity)
            if kind == "artifact_job":
                protected["studio"].update(
                    str(value.get(k, "")) for k in ("output_id", "artifact_id")
                )
            if kind == "export":
                protected["studio/exports"].add(identity)
        candidates = []
        for relative, keep in protected.items():
            folder = state / relative
            if not folder.exists():
                continue
            safe_path(folder)
            for path in folder.iterdir():
                identity = path.stem if relative == "studio/exports" else path.name
                if (
                    not re.fullmatch(r"[a-f0-9]{32}", identity)
                    or identity in keep
                    or time.time() - path.stat().st_mtime < min_age_seconds
                ):
                    continue
                if (
                    path.is_symlink()
                    or path.is_junction()
                    or not path.resolve().is_relative_to(state)
                ):
                    raise DocQAError("SOURCE_ACCESS_DENIED", "Cleanup refuses unsafe paths")
                members = files(path) if path.is_dir() else [path]
                if any(
                    time.time() - _io_path(member).stat().st_mtime < min_age_seconds
                    for member in members
                ):
                    continue
                candidates.append(
                    {
                        "path": path.relative_to(state).as_posix(),
                        "bytes": sum(_io_path(p).stat().st_size for p in members),
                    }
                )
                if apply:
                    lock = state / "agent" / (identity + ".writer.lock")
                    lock.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with portalocker.Lock(_io_path(lock), timeout=0):
                            if path.is_dir():
                                shutil.rmtree(_io_path(path))
                            else:
                                path.unlink()
                    except portalocker.exceptions.LockException:
                        candidates[-1]["skipped"] = "writer_active"
        return {
            "dry_run": not apply,
            "candidates": candidates,
            "index_generations_preserved": True,
            "source_documents_preserved": True,
        }
