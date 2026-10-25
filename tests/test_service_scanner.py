"""Source authorization, incomplete snapshots, and bounded discovery."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from docqa.domain.errors import DocQAError
from docqa.service import scanner
from docqa.service.scanner import canonical_path, scan_directory, scan_file, validate_watch_root


def _write(root: Path, name: str, data: bytes = b"document") -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_recursive_case_insensitive_extensions_and_hashes(tmp_path):
    first = _write(tmp_path, "Top.PDF", b"one")
    second = _write(tmp_path, "nested/file.DoCx", b"two")
    _write(tmp_path, "nested/legacy.doc")
    _write(tmp_path, "ignored.txt")
    snapshot = scan_directory(tmp_path)
    assert snapshot.complete
    assert set(snapshot.entries) == {os.path.normcase(str(first)), os.path.normcase(str(second))}
    assert (
        snapshot.entries[os.path.normcase(str(first))].content_sha256
        == hashlib.sha256(b"one").hexdigest()
    )
    assert snapshot.entries[os.path.normcase(str(second))].size == 3
    assert snapshot.ignored == 2


def test_nonrecursive_and_explicit_legacy_types(tmp_path):
    legacy = _write(tmp_path, "top.DOC")
    _write(tmp_path, "nested/slide.PPT")
    snapshot = scan_directory(tmp_path, recursive=False, types=[".DOC", "ppt"])
    assert snapshot.complete
    assert list(snapshot.entries) == [os.path.normcase(str(legacy))]


def test_ignores_office_temporary_files_and_hidden_caches(tmp_path):
    _write(tmp_path, "~$open.docx")
    _write(tmp_path, "partial.pdf.part")
    _write(tmp_path, "temp.tmp")
    _write(tmp_path, "swap.swp")
    _write(tmp_path, ".cache/cached.pdf")
    _write(tmp_path, "__pycache__/cached.pdf")
    wanted = _write(tmp_path, "published.pdf")
    snapshot = scan_directory(tmp_path)
    assert snapshot.complete
    assert list(snapshot.entries) == [os.path.normcase(str(wanted))]
    assert snapshot.ignored == 6


def test_watch_root_requires_explicit_trusted_roots(tmp_path):
    with pytest.raises(DocQAError) as error:
        validate_watch_root(tmp_path, [], [])
    assert error.value.code == "SOURCE_ROOT_NOT_CONFIGURED"
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(DocQAError) as error:
        validate_watch_root(outside, [allowed], [])
    assert error.value.code == "SOURCE_ROOT_NOT_ALLOWED"


def test_watch_root_containment_is_not_a_string_prefix(tmp_path):
    allowed = tmp_path / "source"
    allowed.mkdir()
    sibling = tmp_path / "source-other"
    sibling.mkdir()
    with pytest.raises(DocQAError, match="outside"):
        validate_watch_root(sibling, [allowed], [])
    child = allowed / "docs"
    child.mkdir()
    assert validate_watch_root(child, [allowed], []) == child


@pytest.mark.parametrize("relationship", ["equal", "inside", "contains"])
def test_watch_and_managed_outputs_must_be_disjoint(tmp_path, relationship):
    root = tmp_path / "sources"
    root.mkdir()
    excluded = {"equal": root, "inside": root / "state", "contains": tmp_path}[relationship]
    with pytest.raises(DocQAError) as error:
        validate_watch_root(root, [tmp_path], [excluded])
    assert error.value.code == "SOURCE_ROOT_OVERLAP"


def test_canonical_missing_outputs_still_inspect_existing_ancestors(tmp_path):
    missing = tmp_path / "new" / "state"
    assert canonical_path(missing, must_exist=False) == missing
    with pytest.raises(DocQAError) as error:
        canonical_path(missing)
    assert error.value.code == "SOURCE_NOT_FOUND"
    regular = _write(tmp_path, "document.pdf")
    with pytest.raises(DocQAError) as error:
        canonical_path(regular, directory=True)
    assert error.value.code == "SOURCE_NOT_DIRECTORY"
    with pytest.raises(DocQAError):
        canonical_path(tmp_path, directory=False)


def test_missing_root_is_an_incomplete_snapshot(tmp_path):
    snapshot = scan_directory(tmp_path / "removed")
    assert not snapshot.complete
    assert not snapshot.entries
    assert snapshot.errors[0]["code"] == "SOURCE_NOT_FOUND"


def test_link_ancestors_are_rejected_without_resolving(monkeypatch, tmp_path):
    child = tmp_path / "linked" / "child"
    child.mkdir(parents=True)
    original = Path.lstat

    def pretend_link(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == tmp_path / "linked":
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", pretend_link)
    with pytest.raises(DocQAError) as error:
        canonical_path(child)
    assert error.value.code == "SOURCE_PATH_UNSAFE"


def test_scanner_skips_reparse_entries_and_marks_incomplete(monkeypatch, tmp_path):
    _write(tmp_path, "wanted.pdf")
    original = os.scandir

    class Entry:
        name = "outside"

        @staticmethod
        def stat(*, follow_symlinks):
            assert not follow_symlinks
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)

    class Entries:
        def __enter__(self):
            return iter([Entry()])

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        os, "scandir", lambda path: Entries() if Path(path) == tmp_path else original(path)
    )
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert snapshot.ignored == 1
    assert snapshot.errors[0]["code"] == "SOURCE_PATH_UNSAFE"


def test_read_permission_failure_cannot_be_a_complete_empty_scan(monkeypatch, tmp_path):
    target = _write(tmp_path, "blocked.pdf")
    original = os.open

    def denied(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError("test denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert snapshot.errors[0]["code"] == "SOURCE_READ_FAILED"
    assert not snapshot.entries


def test_traversal_permission_failure_is_recorded(monkeypatch, tmp_path):
    def denied(_):
        raise PermissionError("test denied")

    monkeypatch.setattr(os, "scandir", denied)
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert snapshot.errors[0]["code"] == "DIRECTORY_SCAN_FAILED"


def test_changed_open_file_is_not_returned(monkeypatch, tmp_path):
    _write(tmp_path, "changing.pdf")
    original = os.fstat
    calls = 0

    def changed(descriptor):
        nonlocal calls
        info = original(descriptor)
        calls += 1
        if calls == 2:
            values = {
                key: getattr(info, key)
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            }
            values["st_mtime_ns"] += 1
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(os, "fstat", changed)
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert snapshot.errors[0]["code"] == "SOURCE_CHANGED_DURING_READ"
    assert not snapshot.entries


def test_file_count_limit_is_incomplete_and_bounded(tmp_path):
    for number in range(4):
        _write(tmp_path, f"{number}.pdf")
    snapshot = scan_directory(tmp_path, max_files=2)
    assert not snapshot.complete
    assert len(snapshot.entries) == 2
    assert snapshot.errors[0]["code"] == "RESOURCE_LIMIT_EXCEEDED"


def test_oversized_file_keeps_previous_membership_undecidable(tmp_path):
    _write(tmp_path, "too-big.pdf", b"four")
    snapshot = scan_directory(tmp_path, max_file_bytes=3)
    assert not snapshot.complete
    assert snapshot.errors[0]["code"] == "SOURCE_TOO_LARGE"


def test_directory_removed_mid_scan_is_incomplete(monkeypatch, tmp_path):
    _write(tmp_path, "stable.pdf")
    original = scanner._hash_file

    def remove_after_read(path, *args):
        result = original(path, *args)
        path.unlink()
        return result

    monkeypatch.setattr(scanner, "_hash_file", remove_after_read)
    snapshot = scan_directory(tmp_path)
    assert not snapshot.complete
    assert any(item["code"] == "SOURCE_CHANGED_DURING_READ" for item in snapshot.errors)


def test_metadata_only_scan_does_not_read_body(monkeypatch, tmp_path):
    target = _write(tmp_path, "wanted.pdf", b"document contents")
    original = os.fdopen

    class NoRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, *_):
            pytest.fail("metadata-only scans must not read source bytes")

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: NoRead(original(*args, **kwargs)))
    snapshot = scan_directory(tmp_path, hash_contents=False)
    assert snapshot.complete
    scanned = snapshot.entries[os.path.normcase(str(target))]
    assert scanned.content_sha256 == ""
    assert scanned.size == 17


def test_single_file_scan_is_independent_of_sibling_count_and_permissions(monkeypatch, tmp_path):
    selected = _write(tmp_path, "wanted.pdf", b"one")
    _write(tmp_path, "other.pdf", b"two")
    assert not scan_directory(tmp_path, max_files=1).complete

    def traversal_forbidden(_):
        pytest.fail("single-file inspection must never enumerate its parent")

    monkeypatch.setattr(os, "scandir", traversal_forbidden)
    result = scan_file(selected)
    assert result.path == str(selected)
    assert result.content_sha256 == hashlib.sha256(b"one").hexdigest()
    assert scan_file(selected, hash_contents=False).content_sha256 == ""


@pytest.mark.parametrize(
    "name,code",
    [
        ("~$locked.docx", "SOURCE_NOT_READY"),
        ("part.pdf.part", "SOURCE_NOT_READY"),
        ("ignored.txt", "UNSUPPORTED_FORMAT"),
    ],
)
def test_single_file_respects_temporary_and_type_policies(tmp_path, name, code):
    selected = _write(tmp_path, name)
    with pytest.raises(DocQAError) as error:
        scan_file(selected)
    assert error.value.code == code


def test_single_file_enforces_size_and_enabled_types(tmp_path):
    selected = _write(tmp_path, "legacy.PPT", b"long")
    with pytest.raises(DocQAError) as error:
        scan_file(selected)
    assert error.value.code == "UNSUPPORTED_FORMAT"
    with pytest.raises(DocQAError) as error:
        scan_file(selected, types=["ppt"], max_file_bytes=3)
    assert error.value.code == "SOURCE_TOO_LARGE"
    assert scan_file(selected, types=["ppt"]).size == 4


def test_single_file_wraps_read_failures(monkeypatch, tmp_path):
    selected = _write(tmp_path, "blocked.pdf")

    def denied(*_):
        raise PermissionError("test denied")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(DocQAError) as error:
        scan_file(selected)
    assert error.value.code == "SOURCE_READ_FAILED"


@pytest.mark.parametrize(
    "options",
    [
        {"max_files": 0},
        {"max_files": True},
        {"max_file_bytes": -1},
        {"types": "pdf"},
        {"types": []},
        {"types": ["exe"]},
        {"recursive": 1},
        {"hash_contents": 1},
    ],
)
def test_invalid_scan_options_are_rejected(tmp_path, options):
    with pytest.raises(DocQAError) as error:
        scan_directory(tmp_path, **options)
    assert error.value.code == "INVALID_SCAN_OPTIONS"
