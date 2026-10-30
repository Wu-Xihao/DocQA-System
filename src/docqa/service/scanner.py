"""Bounded, link-free source discovery with explicit incomplete-scan reporting.

An incomplete snapshot must never be used as proof that a source was deleted.
This module only reads source files; it neither follows links nor parses content.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from docqa.domain.errors import DocQAError
from docqa.domain.formats import ALL_FORMATS, NATIVE_FORMATS

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_TYPES = frozenset(ALL_FORMATS)
_CACHE_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "$recycle.bin",
        "system volume information",
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
    }
)


@dataclass(frozen=True)
class ScannedFile:
    path: str
    size: int
    mtime_ns: int
    content_sha256: str


@dataclass
class ScanSnapshot:
    root: Path
    entries: dict[str, ScannedFile] = field(default_factory=dict)
    complete: bool = True
    errors: list[dict[str, str]] = field(default_factory=list)
    ignored: int = 0


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _check_ancestors(path: Path, *, must_exist: bool) -> os.stat_result | None:
    """Inspect lexical ancestors with lstat, never resolve through a link."""
    target_info = None
    for candidate in [*reversed(path.parents), path]:
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            if must_exist:
                raise DocQAError("SOURCE_NOT_FOUND", f"Source does not exist: {candidate}") from exc
            return None
        except OSError as exc:
            raise DocQAError(
                "SOURCE_PATH_UNAVAILABLE", f"Cannot inspect source path: {candidate}: {exc}"
            ) from exc
        if _is_link(info):
            raise DocQAError(
                "SOURCE_PATH_UNSAFE", f"Source links and reparse points are forbidden: {candidate}"
            )
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise DocQAError(
                "SOURCE_PATH_UNSAFE", f"Source ancestor is not a directory: {candidate}"
            )
        target_info = info
    return target_info


def canonical_path(
    path: str | os.PathLike[str],
    must_exist: bool = True,
    directory: bool | None = None,
) -> Path:
    """Return a normalized absolute path after rejecting links and special files.

    Missing suffixes are permitted only for output/exclusion paths explicitly
    checked with ``must_exist=False``. Existing ancestors are still checked.
    """
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise ValueError("A nonempty text path is required")
        if os.name == "nt" and raw.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\")):
            raise ValueError("Windows device and extended namespace inputs are forbidden")
        result = Path(os.path.abspath(os.path.expanduser(raw)))
    except (TypeError, ValueError, OSError) as exc:
        raise DocQAError("SOURCE_PATH_UNSAFE", f"Invalid source path: {exc}") from exc
    info = _check_ancestors(result, must_exist=must_exist)
    if info is None:
        return result
    is_directory = stat.S_ISDIR(info.st_mode)
    if not is_directory and not stat.S_ISREG(info.st_mode):
        raise DocQAError("SOURCE_PATH_UNSAFE", f"Special source files are forbidden: {result}")
    if directory is True and not is_directory:
        raise DocQAError("SOURCE_NOT_DIRECTORY", f"Expected a directory: {result}")
    if directory is False and is_directory:
        raise DocQAError("SOURCE_NOT_FILE", f"Expected a regular file: {result}")
    return result


def _within(path: Path, parent: Path) -> bool:
    """Path containment respects the current platform's case rules and drive."""
    try:
        return os.path.commonpath(
            [os.path.normcase(str(path)), os.path.normcase(str(parent))]
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def validate_watch_root(
    path: str | os.PathLike[str],
    allowed_roots: Iterable[str | os.PathLike[str]],
    excluded_roots: Iterable[str | os.PathLike[str]],
) -> Path:
    """Authorize a real source directory, disjoint from all managed outputs."""
    if allowed_roots is None or isinstance(allowed_roots, (str, bytes, os.PathLike)):
        raise DocQAError(
            "SOURCE_ROOT_NOT_CONFIGURED", "allowed_source_roots must be a nonempty list"
        )
    trusted = [canonical_path(item, directory=True) for item in allowed_roots]
    if not trusted:
        raise DocQAError(
            "SOURCE_ROOT_NOT_CONFIGURED",
            "Configure allowed_source_roots before registering watches",
        )
    if excluded_roots is None or isinstance(excluded_roots, (str, bytes, os.PathLike)):
        raise DocQAError("SOURCE_PATH_UNSAFE", "excluded_roots must be a list")
    root = canonical_path(path, directory=True)
    if not any(_within(root, trusted_root) for trusted_root in trusted):
        raise DocQAError(
            "SOURCE_ROOT_NOT_ALLOWED", f"Source directory is outside allowed_source_roots: {root}"
        )
    for excluded in excluded_roots:
        managed = canonical_path(excluded, must_exist=False)
        if _within(root, managed) or _within(managed, root):
            raise DocQAError(
                "SOURCE_ROOT_OVERLAP", f"Watch directory overlaps managed output: {root}; {managed}"
            )
    return root


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        stat.S_IFMT(info.st_mode),
    )


def _opened_identity(info: os.stat_result) -> tuple[int, ...]:
    # Windows path-stat and handle-stat can expose different ctime values.
    # Compare ctime within the same stat API, never between those APIs.
    value = _fingerprint(info)
    return value[:4] + value[5:]


def _hash_file(path: Path, max_file_bytes: int, hash_contents: bool = True) -> ScannedFile:
    canonical_path(path, directory=False)
    before = path.lstat()
    if before.st_size > max_file_bytes:
        raise DocQAError("SOURCE_TOO_LARGE", f"Source exceeds max_file_bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _opened_identity(before) != _opened_identity(
                opened
            ):
                raise DocQAError(
                    "SOURCE_CHANGED_DURING_READ", f"Source changed before reading: {path}"
                )
            digest = hashlib.sha256()
            byte_count = 0
            if hash_contents:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    byte_count += len(block)
                    if byte_count > max_file_bytes:
                        raise DocQAError(
                            "SOURCE_TOO_LARGE", f"Source grew beyond max_file_bytes: {path}"
                        )
                    digest.update(block)
            else:
                byte_count = before.st_size
            after_opened = os.fstat(stream.fileno())
        _check_ancestors(path, must_exist=True)
        after = path.lstat()
        if (
            _fingerprint(opened) != _fingerprint(after_opened)
            or _fingerprint(before) != _fingerprint(after)
            or byte_count != before.st_size
        ):
            raise DocQAError("SOURCE_CHANGED_DURING_READ", f"Source changed during reading: {path}")
        return ScannedFile(
            str(path),
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest() if hash_contents else "",
        )
    finally:
        os.close(descriptor)


def _error(snapshot: ScanSnapshot, code: str, path: Path, message: str) -> None:
    snapshot.complete = False
    snapshot.errors.append({"code": code, "path": str(path), "message": message})


def scan_file(
    path: str | os.PathLike[str],
    *,
    max_file_bytes: int = 104857600,
    types: Iterable[str] = NATIVE_FORMATS,
    hash_contents: bool = True,
) -> ScannedFile:
    """Inspect one source without traversing or hashing its siblings.

    The caller supplies the containing source's authorization policy. Link,
    format, temporary-file, access, size, and stability checks remain identical
    to directory discovery, independent of its ``max_files`` resource bound.
    """
    if (
        type(hash_contents) is not bool
        or isinstance(max_file_bytes, bool)
        or not isinstance(max_file_bytes, int)
        or max_file_bytes < 1
    ):
        raise DocQAError("INVALID_SCAN_OPTIONS", "Invalid single-file scan options")
    if isinstance(types, (str, bytes)):
        raise DocQAError("INVALID_SCAN_OPTIONS", "types must be a nonempty list of extensions")
    try:
        extensions = {item.lower().removeprefix(".") for item in types}
    except (AttributeError, TypeError) as exc:
        raise DocQAError("INVALID_SCAN_OPTIONS", "types must contain extensions") from exc
    if not extensions or extensions - _TYPES:
        raise DocQAError("INVALID_SCAN_OPTIONS", "Supported extensions are " + "/".join(ALL_FORMATS))
    selected = canonical_path(path, directory=False)
    if selected.name.startswith("~$") or selected.name.lower().endswith((".tmp", ".part", ".swp")):
        raise DocQAError("SOURCE_NOT_READY", "Temporary/partial source files are excluded")
    if selected.suffix.lower().lstrip(".") not in extensions:
        raise DocQAError("UNSUPPORTED_FORMAT", "Document type is outside the source policy")
    try:
        return _hash_file(selected, max_file_bytes, hash_contents)
    except OSError as exc:
        raise DocQAError(
            "SOURCE_READ_FAILED", f"Cannot read source file: {selected}: {exc}"
        ) from exc


def scan_directory(
    root: str | os.PathLike[str],
    recursive: bool = True,
    types: Iterable[str] = NATIVE_FORMATS,
    max_files: int = 10000,
    max_file_bytes: int = 104857600,
    hash_contents: bool = True,
) -> ScanSnapshot:
    """Read a bounded content-hash snapshot, retaining all traversal failures.

    The caller first authorizes a watch with ``validate_watch_root``. Links are
    errors, never traversed. Unsupported files/cache directories are intentional
    exclusions. A resource bound or any unreadable candidate makes the result
    incomplete so callers can retain prior documents rather than infer deletion.
    ``hash_contents=False`` opens each candidate to check access and metadata
    stability, but does not read its body; callers must periodically run the
    default full-hash scan to detect changes preserving size and modification time.
    """
    if not isinstance(recursive, bool) or not isinstance(hash_contents, bool):
        raise DocQAError("INVALID_SCAN_OPTIONS", "recursive/hash_contents must be booleans")
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files < 1
        or isinstance(max_file_bytes, bool)
        or not isinstance(max_file_bytes, int)
        or max_file_bytes < 1
    ):
        raise DocQAError("INVALID_SCAN_OPTIONS", "Resource bounds must be positive integers")
    if isinstance(types, (str, bytes)):
        raise DocQAError("INVALID_SCAN_OPTIONS", "types must be a nonempty list of extensions")
    try:
        extensions = {item.lower().removeprefix(".") for item in types}
    except (AttributeError, TypeError) as exc:
        raise DocQAError("INVALID_SCAN_OPTIONS", "types must contain extensions") from exc
    if not extensions or extensions - _TYPES:
        raise DocQAError("INVALID_SCAN_OPTIONS", "Supported extensions are " + "/".join(ALL_FORMATS))
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    snapshot = ScanSnapshot(lexical_root)
    try:
        snapshot.root = canonical_path(root, directory=True)
        root_fingerprint = _fingerprint(snapshot.root.lstat())
    except DocQAError as exc:
        _error(snapshot, exc.code, lexical_root, exc.message)
        return snapshot
    pending = [snapshot.root]
    directory_fingerprints = {snapshot.root: root_fingerprint}
    directory_names = {}
    visited_entries = 0
    # Also bound trees containing no eligible files, without changing the public API.
    max_visited_entries = min(1000000, max(10000, max_files * 100))
    stopped = False
    while pending and not stopped:
        directory_path = pending.pop()
        try:
            canonical_path(directory_path, directory=True)
            directory_fingerprints[directory_path] = _fingerprint(directory_path.lstat())
            directory_names[directory_path] = set()
            with os.scandir(directory_path) as stream:
                for entry in stream:
                    directory_names[directory_path].add(entry.name)
                    visited_entries += 1
                    path = directory_path / entry.name
                    if visited_entries > max_visited_entries:
                        _error(
                            snapshot,
                            "RESOURCE_LIMIT_EXCEEDED",
                            path,
                            "Directory entry scan limit exceeded",
                        )
                        stopped = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _is_link(info):
                            _error(
                                snapshot,
                                "SOURCE_PATH_UNSAFE",
                                path,
                                "Source link/reparse point skipped",
                            )
                            snapshot.ignored += 1
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if (
                                not recursive
                                or entry.name.startswith(".")
                                or entry.name.lower() in _CACHE_DIRS
                                or getattr(info, "st_file_attributes", 0) & _HIDDEN
                            ):
                                snapshot.ignored += 1
                            else:
                                pending.append(path)
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            _error(
                                snapshot, "SOURCE_PATH_UNSAFE", path, "Special source file skipped"
                            )
                            snapshot.ignored += 1
                            continue
                        if (
                            entry.name.startswith("~$")
                            or entry.name.lower().endswith((".tmp", ".part", ".swp"))
                            or path.suffix.lower().lstrip(".") not in extensions
                        ):
                            snapshot.ignored += 1
                            continue
                        if len(snapshot.entries) >= max_files:
                            _error(snapshot, "RESOURCE_LIMIT_EXCEEDED", path, "max_files exceeded")
                            stopped = True
                            break
                        scanned = _hash_file(path, max_file_bytes, hash_contents)
                        snapshot.entries[os.path.normcase(str(path))] = scanned
                    except DocQAError as exc:
                        _error(snapshot, exc.code, path, exc.message)
                    except OSError as exc:
                        _error(snapshot, "SOURCE_READ_FAILED", path, str(exc))
        except DocQAError as exc:
            _error(snapshot, exc.code, directory_path, exc.message)
        except OSError as exc:
            _error(snapshot, "DIRECTORY_SCAN_FAILED", directory_path, str(exc))
    for directory_path, initial in directory_fingerprints.items():
        try:
            canonical_path(directory_path, directory=True)
            names_changed = False
            if snapshot.complete:
                with os.scandir(directory_path) as stream:
                    final_names = set()
                    for entry in stream:
                        final_names.add(entry.name)
                        if len(final_names) > max_visited_entries:
                            names_changed = True
                            break
                names_changed = names_changed or final_names != directory_names[directory_path]
            # Directory timestamps change when children change; do not publish
            # deletion inferences from a tree modified during this scan.
            if names_changed or _fingerprint(directory_path.lstat()) != initial:
                _error(
                    snapshot,
                    "SOURCE_CHANGED_DURING_READ",
                    directory_path,
                    "Source directory changed during scan",
                )
        except DocQAError as exc:
            _error(snapshot, exc.code, directory_path, exc.message)
        except OSError as exc:
            _error(snapshot, "SOURCE_READ_FAILED", directory_path, str(exc))
    return snapshot
