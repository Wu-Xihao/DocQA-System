"""Warm verification cannot hide edits, same-size writes, replacement or links."""

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_agent_integration as shared

from docqa.docbase import storage
from docqa.domain.errors import DocQAError
from docqa.integrity import VerifiedCache, file_stamp


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


def test_repeated_and_concurrent_loads_do_one_full_validation(corpus, monkeypatch):
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
