"""CLI contract tests use stubs; the service integration suite tests real I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from docqa.cli import service as cli
from docqa.domain.errors import DocQAError


@pytest.fixture
def harness(monkeypatch):
    calls = []
    construction = []

    class StubService:
        result = {"state": "succeeded"}
        error = None

        @property
        def queue(self):
            return self

        def __getattr__(self, operation):
            def call(*args, **kwargs):
                calls.append((operation, args, kwargs))
                if self.error:
                    raise self.error
                return self.result

            return call

    service = StubService()

    def factory(state, config):
        construction.append((state, config))
        return service

    monkeypatch.setattr(cli, "_service", factory)
    app = typer.Typer()
    app.add_typer(cli.app, name="service")
    return CliRunner(), app, service, calls, construction


@pytest.mark.parametrize(
    ("arguments", "operation", "args", "kwargs"),
    [
        (
            [
                "source",
                "add",
                "input",
                "--no-recursive",
                "--types",
                ".PDF, docx",
                "--category",
                "docs",
                "--interval-seconds",
                "12",
            ],
            "add_source",
            (Path("input"),),
            {
                "recursive": False,
                "types": ("pdf", "docx"),
                "category": "docs",
                "interval_seconds": 12.0,
            },
        ),
        (["source", "list"], "list_sources", (), {}),
        (
            [
                "source",
                "update",
                "S01",
                "--disabled",
                "--category",
                "new",
                "--interval-seconds",
                "8",
            ],
            "update_source",
            ("S01",),
            {"enabled": False, "category": "new", "interval_seconds": 8.0},
        ),
        (["source", "remove", "S01"], "remove_source", ("S01",), {}),
        (["source", "search", "S01"], "discover", ("S01",), {}),
        (["scan"], "enqueue_scan", (None,), {}),
        (["scan", "S01"], "enqueue_scan", ("S01",), {}),
        (
            ["parse", "input/a.pdf", "--source", "S01", "--force"],
            "enqueue_parse",
            (Path("input/a.pdf"),),
            {"source_id": "S01", "force": True},
        ),
        (
            ["documents", "--source", "S01", "--include-removed"],
            "list_documents",
            (),
            {"source_id": "S01", "include_removed": True},
        ),
        (["document", "show", "D01"], "document", ("D01",), {}),
        (["document", "remove", "D01"], "remove_document", ("D01",), {}),
        (["document", "restore", "D01"], "restore_document", ("D01",), {}),
        (["images", "list", "D01"], "images", ("D01",), {}),
        (["images", "reparse", "D01"], "reparse_images", ("D01",), {}),
        (["index", "build"], "enqueue_build", (), {}),
        (["index", "status"], "index_status", (), {}),
        (["index", "verify", "--generation", "G01"], "verify_index", (), {"generation_id": "G01"}),
        (
            ["job", "list", "--job-state", "failed", "--limit", "8"],
            "list",
            (),
            {"state": "failed", "limit": 8},
        ),
        (["job", "show", "J01"], "get", ("J01",), {}),
        (["job", "retry", "J01"], "retry", ("J01",), {}),
        (["job", "cancel", "J01"], "cancel", ("J01",), {}),
        (["work"], "process_next", (), {}),
        (["run", "--once"], "run_once", (), {}),
        (
            ["run", "--poll-seconds", "0.1", "--max-ticks", "2"],
            "run",
            (),
            {"poll_seconds": 0.1, "max_ticks": 2},
        ),
        (["status"], "status", (), {}),
        (["stop"], "request_stop", (), {}),
    ],
)
def test_command_delegates_without_extra_work(harness, arguments, operation, args, kwargs):
    runner, app, _, calls, construction = harness
    result = runner.invoke(
        app, ["service", *arguments, "--state", "custom-state", "-c", "custom.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"state": "succeeded"}
    assert calls == [(operation, args, kwargs)]
    assert construction == [(Path("custom-state"), Path("custom.yaml"))]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["source"],
        ["document"],
        ["images"],
        ["index"],
        ["job"],
        ["source", "add"],
        ["source", "list"],
        ["source", "update"],
        ["source", "remove"],
        ["source", "search"],
        ["scan"],
        ["parse"],
        ["documents"],
        ["document", "show"],
        ["document", "remove"],
        ["document", "restore"],
        ["images", "list"],
        ["images", "reparse"],
        ["index", "build"],
        ["index", "status"],
        ["index", "verify"],
        ["job", "list"],
        ["job", "show"],
        ["job", "retry"],
        ["job", "cancel"],
        ["work"],
        ["run"],
        ["status"],
        ["stop"],
    ],
)
def test_help_is_read_only_and_documents_every_command(harness, arguments):
    runner, app, _, calls, construction = harness
    result = runner.invoke(app, ["service", *arguments, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.stdout
    assert not calls and not construction


@pytest.mark.parametrize(
    ("error", "code", "expected_exit"),
    [
        (DocQAError("SOURCE_NOT_FOUND", "Missing registered source"), "SOURCE_NOT_FOUND", 4),
        (DocQAError("INVALID_CONFIG", "Invalid service configuration"), "INVALID_CONFIG", 2),
        (ValueError("private implementation detail"), "INVALID_REQUEST", 2),
        (OSError("private implementation detail"), "STORAGE_IO_FAILED", 4),
    ],
)
def test_errors_are_bounded_json(harness, error, code, expected_exit):
    runner, app, service, _, _ = harness
    service.error = error
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == expected_exit
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "docqa.error.v1"
    assert payload["error"]["code"] == code
    assert "private implementation" not in result.stdout


@pytest.mark.parametrize("state", ["failed", "succeeded", None])
def test_work_failure_and_empty_queue_exit_semantics(harness, state):
    runner, app, service, _, _ = harness
    service.result = None if state is None else {"state": state}
    result = runner.invoke(app, ["service", "work"])
    assert result.exit_code == (4 if state == "failed" else 0)
    assert json.loads(result.stdout) == service.result


@pytest.mark.parametrize(
    "report",
    [
        {"failed": 1},
        {"failed_jobs": 1},
        {"jobs": [{"state": "failed"}]},
        {"results": [{"status": "failed"}]},
        {"status": "failed"},
    ],
)
def test_run_once_failure_exit_is_distinct(harness, report):
    runner, app, service, _, _ = harness
    service.result = report
    result = runner.invoke(app, ["service", "run", "--once"])
    assert result.exit_code == 5
    assert json.loads(result.stdout) == report


def test_run_once_success_and_null_optional_detail(harness):
    runner, app, service, _, _ = harness
    service.result = {"failed": 0, "jobs": None}
    result = runner.invoke(app, ["service", "run", "--once"])
    assert result.exit_code == 0


def test_invalid_loop_flag_combination_does_not_construct_service(harness):
    runner, app, _, calls, construction = harness
    result = runner.invoke(app, ["service", "run", "--once", "--max-ticks", "2"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "INVALID_REQUEST"
    assert not calls and not construction


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--poll-seconds", "0"],
        ["run", "--max-ticks", "0"],
        ["source", "add", "input", "--interval-seconds", "-1"],
        ["job", "list", "--limit", "0"],
        ["job", "list", "--limit", "1001"],
    ],
)
def test_numeric_cli_boundaries_prevent_service_calls(harness, arguments):
    runner, app, _, calls, construction = harness
    result = runner.invoke(app, ["service", *arguments])
    assert result.exit_code == 2
    assert not calls and not construction


def test_ctrl_c_is_reported_without_claiming_job_cancellation(harness):
    runner, app, service, calls, _ = harness
    service.error = KeyboardInterrupt()
    result = runner.invoke(app, ["service", "run", "--max-ticks", "1"])
    assert result.exit_code == 130
    assert json.loads(result.stdout)["status"] == "interrupted"
    assert calls[0][0] == "run"


def test_default_state_and_unchanged_optional_update(harness):
    runner, app, _, calls, construction = harness
    result = runner.invoke(app, ["service", "source", "update", "S01"])
    assert result.exit_code == 0
    assert construction == [(Path("runtime/service"), None)]
    assert calls == [
        ("update_source", ("S01",), {"enabled": None, "category": None, "interval_seconds": None})
    ]
