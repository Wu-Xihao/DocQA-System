"""Warm verification cannot hide edits, same-size writes, replacement or links."""

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_agent_integration as shared

from docqa.docbase import storage
from docqa.domain.errors import DocQAError
from docqa.integrity import VerifiedCache, file_stamp, tree_stamp


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_restored_mtime_does_not_hide_same_size_write(tmp_path):
    path = tmp_path / "bytes"
    path.write_bytes(b"before")
    original = path.stat()
    before = file_stamp(path)
    path.write_bytes(b"edited")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert file_stamp(path) != before


def test_racy_stamps_are_never_cached(tmp_path):
    from docqa.integrity import VerifiedCache, is_racy

    path = tmp_path / "fresh"
    path.write_bytes(b"value")
    stamp = file_stamp(path)
    assert is_racy(stamp)
    cache = VerifiedCache()
    cache.put("key", stamp, "value", 5)
    assert cache.get("key", stamp) is None


def test_tree_identity_detects_restored_mtime_and_empty_directory(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"before")
    before = tree_stamp(tmp_path)
    assert before is not None
    old = path.stat()
    path.write_bytes(b"edited")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert tree_stamp(tmp_path) != before
    before = tree_stamp(tmp_path)
    (tmp_path / "new-empty").mkdir()
    if os.name == "nt":
        assert tree_stamp(tmp_path) != before


@pytest.mark.skipif(os.name != "nt", reason="Windows change enumeration")
def test_windows_tree_opens_directories_instead_of_each_asset(tmp_path, monkeypatch):
    from docqa import integrity

    for i in range(100):
        (tmp_path / str(i)).write_bytes(b"fixture")
    original, calls = integrity._open, []

    def counted(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(integrity, "_open", counted)
    assert tree_stamp(tmp_path) is not None
    assert calls == [str(tmp_path)]


@pytest.mark.skipif(os.name != "nt", reason="Windows unsupported filesystem")
def test_windows_enumeration_failure_disables_cache(tmp_path, monkeypatch):
    from docqa import integrity

    monkeypatch.setattr(integrity, "_info", lambda *args: False)
    assert tree_stamp(tmp_path) is None


def test_cache_is_bounded_and_returns_independent_values():
    cache = VerifiedCache(max_entries=2, max_bytes=20)
    cache.put("a", (1,), {"nested": [1]}, 10)
    item = cache.get("a", (1,))
    item["nested"][0] = 99
    assert cache.get("a", (1,)) == {"nested": [1]}
    cache.put("b", (1,), {}, 10)
    cache.put("c", (1,), {}, 10)
    assert cache.get("a", (1,)) is None
    assert cache.get("b", (2,)) is None


def test_serialized_cache_measures_retained_bytes_and_copies_nested_values():
    cache = VerifiedCache(max_bytes=1024, serialized=True)
    # A large Python/JSON estimate must not reject a small retained buffer.
    cache.put("index", (1,), {"postings": {"1": [[0, 0.5]]}}, 170 * 1024 * 1024)
    value = cache.get("index", (1,))
    value["postings"]["1"][0][1] = 99
    assert cache.get("index", (1,))["postings"]["1"][0][1] == 0.5
    assert isinstance(cache.entries["index"][1], bytes)
    assert cache.entries["index"][2] == len(cache.entries["index"][1]) <= 1024
    cache.put("too_large", (1,), b"x" * 2048, 0)
    assert cache.get("too_large", (1,)) is None


def test_repeated_and_concurrent_loads_do_one_full_validation(corpus, monkeypatch):
    from docqa import integrity

    # Fixture generations were just written; published ones are long settled.
    monkeypatch.setattr(integrity, "_racy", lambda change: False)
    directory = Path(corpus[2].generation_dir)
    storage._VERIFIED_GENERATIONS.clear()
    original = storage._load_generation
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "_load_generation", counted)

    def load(_):
        with storage.load_generation(directory) as value:
            assert len(value.chunks)
            value.members[0]["source_name"] = "caller mutation"
            return value.vectors.shape

    with ThreadPoolExecutor(max_workers=4) as executor:
        shapes = list(executor.map(load, range(4)))
    assert len(set(shapes)) == 1 and len(calls) == 1
    with storage.load_generation(directory) as value:
        assert value.members[0]["source_name"] != "caller mutation"
        assert value.vectors[0].shape[0] > 0
    assert len(calls) == 1
    storage.verify_generation(directory)
    assert len(calls) == 2  # Explicit verify is always a full independent scan.


def test_warm_cache_rejects_restored_timestamp_tamper(corpus, tmp_path):
    directory = tmp_path / "generations" / Path(corpus[2].generation_dir).name
    shutil.copytree(corpus[2].generation_dir, directory)
    with storage.load_generation(directory):
        pass
    path = directory / "dense/vectors.npy"
    old = path.stat()
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(DocQAError) as error:
        storage.load_generation(directory)
    assert error.value.code == "GENERATION_CORRUPT"


@pytest.mark.parametrize("action", ["delete", "add", "replace"])
def test_package_membership_and_file_identity_invalidate(corpus, tmp_path, action):
    directory = tmp_path / "generations" / Path(corpus[2].generation_dir).name
    shutil.copytree(corpus[2].generation_dir, directory)
    with storage.load_generation(directory):
        pass
    path = directory / "bm25/index.json"
    if action == "delete":
        path.unlink()
    elif action == "add":
        (directory / "unlisted.bin").write_bytes(b"extra")
    else:
        replacement = directory / "replacement"
        replacement.write_bytes(b"{}")
        replacement.replace(path)
    with pytest.raises(DocQAError):
        storage.load_generation(directory)


def test_unavailable_change_identity_falls_back_to_full_validation(corpus, monkeypatch):
    original = storage._load_generation
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "tree_stamp", lambda _: None)
    monkeypatch.setattr(storage, "_load_generation", counted)
    for _ in range(2):
        with storage.load_generation(Path(corpus[2].generation_dir)):
            pass
    assert len(calls) == 2


def test_cache_hits_share_readonly_payload_but_copy_metadata(corpus, monkeypatch):
    from docqa import integrity

    monkeypatch.setattr(integrity, "_racy", lambda change: False)
    directory = Path(corpus[2].generation_dir)
    storage._VERIFIED_GENERATIONS.clear()
    with storage.load_generation(directory) as first:
        first.chunks.append("caller list mutation")
        first.members[0]["source_name"] = "caller mutation"
    with storage.load_generation(directory) as second, storage.load_generation(directory) as third:
        assert "caller list mutation" not in second.chunks
        assert second.members[0]["source_name"] != "caller mutation"
        assert second.chunks is not third.chunks and second.chunks[0] is third.chunks[0]
        assert second.bm25 is third.bm25 and second.members is not third.members
