"""Only owned temporary states are backed up/restored/garbage-collected."""

import json
import os
import shutil
import sqlite3
import time
from uuid import uuid4

import portalocker
import pytest

from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError
from docqa.operations import backup, cleanup_web, restore, verify_backup
from docqa.web.tasks import TaskStore
from docqa.web.workspace_store import WorkspaceStore


def states(tmp_path):
    service, web = tmp_path / "service", tmp_path / "web"
    (service / "index/generations/g1").mkdir(parents=True)
    (service / "index/generations/g1/manifest.json").write_text('{"synthetic":true}')
    tasks = TaskStore(web)
    WorkspaceStore(tasks)
    return service, web


def test_backup_checksums_nested_manifests_and_exact_original_restore(tmp_path):
    service, web = states(tmp_path)
    output = tmp_path / "backup"
    long_relative = "/".join(["long-artifact"] * 22) + "/图片.png"
    long_file = _io_path(service / long_relative)
    long_file.parent.mkdir(parents=True)
    long_file.write_bytes(b"synthetic-long-path")
    result = backup(service, web, output)
    assert result["files"] >= 2
    manifest = verify_backup(output)
    assert "service/" + long_relative in manifest["files"]
    assert "service/index/generations/g1/manifest.json" in manifest["files"]
    with pytest.raises(DocQAError):
        restore(output, service, web)
    # Both resolved targets are explicit children of this pytest-owned directory.
    for target in (service, web):
        assert target.resolve().is_relative_to(tmp_path.resolve())
        shutil.rmtree(_io_path(target))
    assert restore(output, service, web)["restored"]
    assert (service / "index/generations/g1/manifest.json").read_text() == '{"synthetic":true}'
    with sqlite3.connect(web / "questions.sqlite") as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    item = output / next(iter(manifest["files"]))
    item.write_bytes(b"tampered")
    with pytest.raises(DocQAError, match="checksum"):
        verify_backup(output)


def test_backup_refuses_live_locks_and_manifest_traversal(tmp_path):
    service, web = states(tmp_path)
    with portalocker.Lock(str(web / ".web-worker.lock"), timeout=0):
        with pytest.raises(DocQAError, match="Stop Web"):
            backup(service, web, tmp_path / "busy")
    assert not (tmp_path / "busy").exists()
    bundle = tmp_path / "bundle"
    backup(service, web, bundle)
    path = bundle / "manifest.json"
    value = json.loads(path.read_text("utf-8"))
    value["files"]["service/../../escape"] = {"size": 0, "sha256": "a" * 64}
    path.write_text(json.dumps(value))
    with pytest.raises(DocQAError, match="member path"):
        verify_backup(bundle)


def test_gc_dry_run_and_apply_keep_referenced_and_recent_data(tmp_path):
    service, web = states(tmp_path)
    tasks = TaskStore(web)
    ws = WorkspaceStore(tasks)
    protected, orphan, recent = uuid4().hex, uuid4().hex, uuid4().hex
    ws.object_put("artifact", "owner", {"id": protected})
    old = time.time() - 90000
    for identity in (protected, orphan, recent):
        folder = web / "studio" / identity
        folder.mkdir(parents=True)
        file = folder / "artifact.json"
        file.write_text("{}")
        os.utime(folder, (old, old))
        if identity != recent:
            os.utime(file, (old, old))
    before = cleanup_web(web)
    assert before["dry_run"] and [c["path"] for c in before["candidates"]] == ["studio/" + orphan]
    assert (web / "studio" / orphan).exists()
    after = cleanup_web(web, apply=True)
    assert not after["dry_run"] and not (web / "studio" / orphan).exists()
    assert (web / "studio" / protected).exists() and (web / "studio" / recent).exists()
    assert (service / "index/generations/g1/manifest.json").exists()
