"""Portable bootstrap and worker routing must preserve private configuration."""

import sys

import pytest

from docqa import desktop
from docqa.processes import module_command


def test_bootstrap_copies_only_public_templates_and_preserves_user_edits(tmp_path):
    desktop.initialize(tmp_path)
    configs = tmp_path / "configs"
    assert (configs / "local.web.yaml").read_bytes() == (
        desktop.bundle_root() / "configs/web.example.yaml"
    ).read_bytes()
    (configs / ".env").write_text("PRIVATE_TEST=keep", encoding="utf-8")
    (configs / "local.web.yaml").write_text("port: 8777", encoding="utf-8")
    desktop.initialize(tmp_path)
    assert (configs / ".env").read_text() == "PRIVATE_TEST=keep"
    assert (configs / "local.web.yaml").read_text() == "port: 8777"
    assert not (tmp_path / "runtime/service-user").exists()
    assert (tmp_path / "data/private/web-uploads").is_dir()


def test_worker_command_is_whitelisted_and_switches_for_frozen(monkeypatch):
    assert module_command("docqa.web.worker", "--persistent") == [
        sys.executable, "-m", "docqa.web.worker", "--persistent",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert module_command("docqa.web.ingestion_worker", "config.json", "state") == [
        sys.executable, "--internal-worker", "docqa.web.ingestion_worker", "config.json", "state",
    ]
    with pytest.raises(ValueError):
        module_command("os", "untrusted")


def test_internal_worker_never_bootstraps_or_loads_local_env(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "initialize", lambda *_: pytest.fail("No bootstrap for worker"))
    monkeypatch.setattr(desktop.runpy, "run_module", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["DocQA.exe"])
    assert desktop.main(["--internal-worker", "docqa.web.worker", "--persistent"]) == 0
    assert sys.argv == ["docqa.web.worker", "--persistent"]
    assert calls == [(("docqa.web.worker",), {"run_name": "__main__", "alter_sys": True})]
    assert desktop.main(["--internal-worker", "os"]) == 2
    assert desktop.main(["--internal-worker"]) == 2


def test_help_and_check_do_not_initialize(monkeypatch, capsys):
    monkeypatch.setattr(desktop, "initialize", lambda *_: pytest.fail("No state for checks"))
    monkeypatch.setattr(desktop, "self_check", lambda: {"status": "ok", "network": False})
    assert desktop.main(["--check"]) == 0
    assert '"network": false' in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        desktop.main(["--help"])
    assert caught.value.code == 0
