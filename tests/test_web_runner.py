"""Exercise the fixed child protocol and real-process termination boundaries."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

import docqa.web.runner as module
from docqa.agent.config import AgentOptions, load_options
from docqa.agent.sessions import SessionStore
from docqa.docbase.pipeline import build_index
from docqa.docbase.storage import load_generation, read_current
from docqa.domain.errors import DocQAError
from docqa.web.runner import HeadlessRunner, WorkerRequest, _result, verify_saved_result
from docqa.web.worker import main as worker_main


@pytest.fixture
def runner(tmp_path):
    return HeadlessRunner(
        tmp_path / "index",
        tmp_path / "agent",
        AgentOptions(mode="demo"),
        tmp_path / "runs",
        timeout_seconds=3,
    )


@pytest.fixture
def request_scope():
    return {"question": "SD5993 的 FE_HEART_Start 如何启动心跳？", "generation_id": "a" * 32}


def child_script(monkeypatch, script):
    """Replace only the executable payload; all runner pipes/lifecycle are real."""
    original = subprocess.Popen
    created, arguments = [], []

    def launch(args, **kwargs):
        arguments.append((args, kwargs))
        process = original([sys.executable, "-c", script], **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    return created, arguments


def run_error(runner, request_scope, code, *, cancelled=lambda: False, progress=lambda _: None):
    with pytest.raises(DocQAError) as caught:
        runner.run(uuid4().hex, request_scope, cancelled=cancelled, progress=progress)
    assert caught.value.code == code
    return caught.value


@pytest.mark.parametrize("job", ["../escape", "A" * 32, "a" * 31, "a" * 33, None])
def test_invalid_job_never_launches(runner, request_scope, monkeypatch, job):
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("launched"))
    with pytest.raises(DocQAError, match="job identifier"):
        runner.run(job, request_scope, cancelled=lambda: False, progress=lambda _: None)
    assert not runner.runs.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"generation_id": None},
        {"generation_id": "../latest"},
        {"question": " "},
        {"question": "x" * 8193},
        {"question": "a\x00b"},
        {"categories": False},
        {"categories": [""]},
        {"topics": []},
        {"topics": ["repeated", "repeated"]},
        {"topics": ["x"] * 13},
        {"topics": ["x"] * 21},
        {"output_format": "html"},
        {"use_planner": "true"},
        {"index": "C:/private"},
    ],
)
def test_request_scope_is_strict(runner, request_scope, changes):
    run_error(runner, {**request_scope, **changes}, "INVALID_REQUEST")
    assert not runner.runs.exists()


def test_precancel_does_not_create_process(runner, request_scope, monkeypatch):
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("launched"))
    run_error(runner, request_scope, "WEB_JOB_CANCELLED", cancelled=lambda: True)
    assert not runner.runs.exists()


@pytest.mark.parametrize(
    "timeout,limit", [(0, 1024), (float("nan"), 1024), (True, 1024), (1, 0), (1, True)]
)
def test_resource_bounds_are_finite_and_positive(tmp_path, timeout, limit):
    with pytest.raises(DocQAError) as caught:
        HeadlessRunner(
            tmp_path,
            tmp_path,
            AgentOptions(),
            tmp_path,
            timeout_seconds=timeout,
            max_output_bytes=limit,
        )
    assert caught.value.code == "INVALID_CONFIG"


def test_environment_only_declared_credentials_and_runtime(runner, monkeypatch):
    monkeypatch.setenv("UNRELATED_PRODUCTION_SECRET", "do-not-copy")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.invalid")
    monkeypatch.setenv("PYTHONSTARTUP", "malicious.py")
    monkeypatch.setenv("PYTHONPATH", "malicious/imports")
    monkeypatch.setenv("DOCQA_GENERATOR_TEST_KEY", "generator-secret")
    monkeypatch.setenv("DOCQA_VERIFIER_TEST_KEY", "verifier-secret")
    monkeypatch.setenv("DOCQA_DENSE_TEST_KEY", "dense-secret")
    runner.options.generator.api_key_env = "DOCQA_GENERATOR_TEST_KEY"
    runner.options.verifier.api_key_env = "DOCQA_VERIFIER_TEST_KEY"
    runner.options.retrieval.dense.api_key_env = "DOCQA_DENSE_TEST_KEY"
    selected = runner._environment()
    assert selected["DOCQA_GENERATOR_TEST_KEY"] == "generator-secret"
    assert selected["DOCQA_VERIFIER_TEST_KEY"] == "verifier-secret"
    assert selected["DOCQA_DENSE_TEST_KEY"] == "dense-secret"
    assert selected["PYTHONPATH"] == str(Path(module.__file__).resolve().parents[2])
    assert selected["PYTHONNOUSERSITE"] == "1"
    for forbidden in ("UNRELATED_PRODUCTION_SECRET", "HTTPS_PROXY", "PYTHONSTARTUP"):
        assert forbidden not in selected
    assert not any(value == "do-not-copy" for value in selected.values())


@pytest.mark.parametrize(
    "name", ["PATH", "PYTHONPATH", "PYTHONSTARTUP", "HTTPS_PROXY", "LD_PRELOAD"]
)
def test_secret_variable_cannot_override_runtime(runner, name):
    runner.options.generator.api_key_env = name
    with pytest.raises(DocQAError) as caught:
        runner._environment()
    assert caught.value.code == "INVALID_CONFIG"


def test_timeout_kills_and_waits_real_process(runner, request_scope, monkeypatch):
    runner.timeout_seconds = 0.2
    processes, args = child_script(monkeypatch, "import time; time.sleep(20)")
    started = time.monotonic()
    run_error(runner, request_scope, "RUNNER_TIMEOUT")
    assert time.monotonic() - started < 5
    assert processes[0].poll() is not None
    command, kwargs = args[0]
    assert command == [sys.executable, "-m", "docqa.web.worker"]
    assert kwargs["shell"] is False and kwargs["close_fds"] is True
    assert kwargs["cwd"].parent == runner.runs
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_cancel_kills_and_waits_real_process(runner, request_scope, monkeypatch):
    processes, _ = child_script(monkeypatch, "import time; time.sleep(20)")
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        run_error(runner, request_scope, "WEB_JOB_CANCELLED", cancelled=cancel.is_set)
    finally:
        timer.cancel()
    assert processes[0].poll() is not None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_flood_is_bounded_and_process_reaped(runner, request_scope, monkeypatch, stream):
    runner.max_output_bytes = 1024
    processes, _ = child_script(
        monkeypatch,
        f"import sys,time; sys.{stream}.write('secret-value'*100000); sys.{stream}.flush(); time.sleep(20)",
    )
    error = run_error(runner, request_scope, "RUNNER_OUTPUT_LIMIT")
    assert "secret-value" not in str(error)
    assert processes[0].poll() is not None


@pytest.mark.parametrize(
    "output", ["not-json secret-value", "{}", "[]", '{"error": {"code": "secret-url"}}']
)
def test_bad_child_protocol_not_echoed(runner, request_scope, monkeypatch, output):
    processes, _ = child_script(monkeypatch, "import sys; sys.stdout.write(" + repr(output) + ")")
    error = run_error(runner, request_scope, "RUNNER_INVALID_OUTPUT")
    assert "secret" not in str(error)
    assert processes[0].poll() is not None


def test_child_error_code_kept_but_message_discarded(runner, request_scope, monkeypatch):
    child_script(
        monkeypatch,
        'import sys; print(\'{"error":{"code":"MODEL_HTTP_ERROR","message":"secret-key"}}\'); sys.exit(4)',
    )
    error = run_error(runner, request_scope, "MODEL_HTTP_ERROR")
    assert "secret-key" not in str(error)


def test_stdout_receipt_cannot_bypass_persisted_answer(runner, request_scope, monkeypatch):
    receipt = {"answer_id": "a" * 32, "session_id": "b" * 32, "status": "answered"}
    child_script(monkeypatch, "print(" + repr(json.dumps(receipt)) + ")")
    run_error(runner, request_scope, "ANSWER_NOT_FOUND")


def test_cwd_is_unique_and_existing_directory_is_preserved(runner, request_scope):
    job = uuid4().hex
    directory = runner.runs / job
    directory.mkdir(parents=True)
    marker = directory / "must-remain.txt"
    marker.write_text("existing", encoding="utf-8")
    with pytest.raises(DocQAError) as caught:
        runner.run(job, request_scope, cancelled=lambda: False, progress=lambda _: None)
    assert caught.value.code == "RUNNER_WORKDIR_EXISTS"
    assert marker.read_text(encoding="utf-8") == "existing"


def test_oversized_input_rejected_before_launch(runner, request_scope, monkeypatch):
    runner.options.retrieval.analyzer_terms = [str(i) + "x" * 120 for i in range(10000)]
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("launched"))
    run_error(runner, request_scope, "RUNNER_INPUT_LIMIT")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"answer_id":"' + b"a" * 32 + b'","answer_id":"' + b"b" * 32 + b'"}',
        b'{"error":{"code":"MODEL_FAILED"}}\n{}',
        b"\xff",
        b'{"answer_id":NaN}',
    ],
)
def test_result_json_rejects_ambiguities(raw):
    with pytest.raises(DocQAError) as caught:
        _result(raw, 0)
    assert caught.value.code == "RUNNER_INVALID_OUTPUT"


def test_worker_invalid_input_redacts_errors(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"secret":"do-not-echo"}')))
    assert worker_main() == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": {"code": "INVALID_REQUEST"}}
    assert "do-not-echo" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_worker_input_byte_limit(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x" * (module.MAX_INPUT_BYTES + 1)))
    )
    assert worker_main() == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "RUNNER_INPUT_LIMIT"


@pytest.fixture(scope="module")
def runner_corpus(tmp_path_factory):
    # Independent genuine documents/parse/index; no runtime fixture is assumed.
    import test_agent_integration as shared

    return shared.corpus.__wrapped__(tmp_path_factory)


def test_real_worker_pins_generation_and_validates_saved_answer(runner_corpus, tmp_path):
    index, options, built = runner_corpus
    with load_generation(index / "generations" / built.generation_id) as generation:
        tls = next(item for item in generation.members if item["category"] == "security")
        later = build_index(
            [generation.directory / tls["artifact_ref"]],
            index,
            options=options.retrieval,
            replace=True,
        )
    assert later.generation_id != built.generation_id
    assert read_current(index)[0] == later.generation_id
    runner = HeadlessRunner(
        index, tmp_path / "state", options, tmp_path / "runs", timeout_seconds=30
    )
    request = {
        "question": "SD5993 的 FE_HEART_Start 如何启动心跳？",
        "generation_id": built.generation_id,
        "categories": ["protocol"],
        "output_format": "table",
    }
    job, stages = uuid4().hex, []
    receipt = runner.run(job, request, cancelled=lambda: False, progress=stages.append)
    assert set(receipt) == {"answer_id", "session_id", "status"}
    assert receipt["status"] == "answered"
    store = SessionStore(runner.agent_state)
    answer = store.load_answer(receipt["answer_id"])
    session = store.get(receipt["session_id"], caller="web:" + job, require_open=False)
    assert answer.generation_id == session.generation_id == built.generation_id
    assert answer.synthetic and not answer.verified
    assert answer.citations and "FE_HEART_Start" in answer.citations[0].quote
    assert session.hard_filters.categories == ["protocol"]
    assert "running" in stages
    assert stages[-1] == "complete"
    for citation in answer.citations:
        assert (
            store.resolve_citation(answer.answer_id, citation.citation_id)["generation_id"]
            == built.generation_id
        )
    # A different scope or job cannot adopt this valid saved answer receipt.
    changed = WorkerRequest.model_validate({**request, "categories": ["security"]})
    with pytest.raises(DocQAError) as caught:
        verify_saved_result(index, runner.agent_state, job, changed, receipt)
    assert caught.value.code == "RUNNER_RESULT_MISMATCH"
    with pytest.raises(DocQAError) as caught:
        verify_saved_result(
            index, runner.agent_state, uuid4().hex, WorkerRequest.model_validate(request), receipt
        )
    assert caught.value.code == "SESSION_OWNER_MISMATCH"


def test_real_worker_missing_models_returns_saved_failed_answer(runner_corpus, tmp_path):
    index, options, built = runner_corpus
    options = options.model_copy(update={"mode": "http"}, deep=True)
    runner = HeadlessRunner(
        index, tmp_path / "state", options, tmp_path / "runs", timeout_seconds=30
    )
    receipt = runner.run(
        uuid4().hex,
        {"question": "SD5993 心跳", "generation_id": built.generation_id},
        cancelled=lambda: False,
        progress=lambda _: None,
    )
    assert receipt["status"] == "failed"
    answer = SessionStore(runner.agent_state).load_answer(receipt["answer_id"])
    assert not answer.verified and not answer.production_ready
    assert answer.stop_reason == "MODEL_NOT_CONFIGURED"


def test_real_worker_ignores_inherited_bad_import_path(runner_corpus, tmp_path, monkeypatch):
    index, options, built = runner_corpus
    malicious = tmp_path / "malicious"
    malicious.mkdir()
    (malicious / "docqa.py").write_text(
        "raise RuntimeError('injected import path')", encoding="utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(malicious))
    runner = HeadlessRunner(
        index, tmp_path / "state", options, tmp_path / "runs", timeout_seconds=30
    )
    receipt = runner.run(
        uuid4().hex,
        {"question": "未记录的温度值是多少", "generation_id": built.generation_id},
        cancelled=lambda: False,
        progress=lambda _: None,
    )
    assert receipt["status"] == "insufficient_evidence"


def test_example_config_is_valid_and_does_not_embed_credentials():
    options = load_options(Path(__file__).resolve().parents[1] / "configs" / "agent.demo.yaml")
    assert options.mode == "demo"
    assert options.generator.api_key_env is None
