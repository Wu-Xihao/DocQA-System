"""A bounded, fixed-command subprocess boundary for the built-in Agent.

This is process isolation for cancellation and resource accounting, not an OS
sandbox for arbitrary third-party CodeAgents. The child never receives a shell
command, client-selected paths, or the parent's unrelated environment secrets.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from docqa.agent.config import AgentOptions
from docqa.agent.sessions import SessionStore
from docqa.domain.errors import DocQAError

MAX_INPUT_BYTES = 1_048_576
_ID = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,79}\Z")
STAGES = frozenset({"running", "complete"})
_STAGE_PREFIX = b"DOCQA_STAGE:"
_STATUS = frozenset(
    {"answered", "partial", "insufficient_evidence", "conflicting_evidence", "failed", "cancelled"}
)


class WorkerRequest(BaseModel):
    """Only the typed question scope crosses the server/child boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=4000)
    generation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    categories: list[str] | None = Field(default=None, max_length=256)
    doc_ids: list[str] | None = Field(default=None, max_length=256)
    topics: list[str] | None = Field(default=None, min_length=1, max_length=12)
    output_format: Literal["markdown", "table", "mermaid"] = "markdown"
    use_planner: bool = False

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("A nonempty question without NUL is required")
        if len(value.encode("utf-8")) > 16000:
            raise ValueError("Question exceeds the UTF-8 limit")
        return value

    @field_validator("categories", "doc_ids", "topics")
    @classmethod
    def valid_scope(cls, values, info):
        maximum = 2000 if info.field_name == "topics" else 512
        if values is not None and any(
            not value.strip() or len(value) > maximum or "\x00" in value for value in values
        ):
            raise ValueError("Scope entries must be nonempty bounded strings")
        if values is not None:
            for value in values:
                value.encode("utf-8")
            if info.field_name == "topics" and len(values) != len(set(values)):
                raise ValueError("Topics must be distinct")
        return values


def _valid_request(value: dict) -> WorkerRequest:
    try:
        return WorkerRequest.model_validate(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DocQAError("INVALID_REQUEST", "Invalid headless Agent question scope") from exc


def _strict_json(raw: str):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _result(raw: bytes, returncode: int) -> dict[str, str]:
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
        if "\n" in value or "\r" in value:
            raise ValueError("Expected one result line")
        result = _strict_json(value)
        if not isinstance(result, dict):
            raise ValueError("Expected a result object")
        if set(result) == {"error"}:
            error = result["error"]
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ValueError("Invalid error envelope")
            if not _ERROR_CODE.fullmatch(error["code"]):
                raise ValueError("Invalid error code")
            # Never propagate child exception text, endpoint URLs, or stdout.
            raise DocQAError(error["code"], "The headless Agent could not complete this request")
        if set(result) != {"answer_id", "session_id", "status"}:
            raise ValueError("Unexpected result fields")
        if any(
            not isinstance(result[key], str) or not _ID.fullmatch(result[key])
            for key in ("answer_id", "session_id")
        ):
            raise ValueError("Invalid result identity")
        if result["status"] not in _STATUS:
            raise ValueError("Invalid answer status")
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise DocQAError("RUNNER_INVALID_OUTPUT", "Invalid headless Agent result protocol") from exc
    if returncode != 0:
        raise DocQAError("RUNNER_PROCESS_FAILED", "The headless Agent exited without success")
    return result


def verify_saved_result(
    index: Path, agent_state: Path, job_id: str, request: WorkerRequest, result: dict
) -> dict[str, str]:
    """A small stdout receipt cannot substitute for a saved and grounded answer."""
    store = SessionStore(agent_state)
    answer = store.load_answer(result["answer_id"])
    session = store.get(result["session_id"], caller="web:" + job_id, require_open=False)
    if (
        answer.session_id != session.session_id
        or answer.status != result["status"]
        or answer.generation_id != request.generation_id
        or session.generation_id != request.generation_id
        or answer.question != request.question
        or session.question != request.question
        or answer.requested_format != request.output_format
        or Path(session.index_path).resolve() != index.resolve()
        or session.hard_filters.categories != request.categories
        or session.hard_filters.doc_ids != request.doc_ids
        or (
            request.topics is not None
            and [topic.text for topic in session.required_topics] != request.topics
        )
    ):
        raise DocQAError("RUNNER_RESULT_MISMATCH", "Saved answer differs from the pinned job scope")
    for citation in answer.citations:
        store.resolve_citation(answer.answer_id, citation.citation_id)
    return result


class _Capture:
    def __init__(self, limit):
        self.limit = limit
        self.stdout = bytearray()
        self.exceeded = threading.Event()
        self.failed = threading.Event()
        self.stages = queue.Queue(maxsize=16)

    def read(self, pipe, *, stdout):
        total, pending = 0, bytearray()
        try:
            while chunk := pipe.read1(4096):
                total += len(chunk)
                if total > self.limit:
                    self.exceeded.set()
                    return
                if stdout:
                    self.stdout.extend(chunk)
                    continue
                # Stderr is counted but never retained or exposed as a user log.
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if line.startswith(_STAGE_PREFIX):
                        stage = line[len(_STAGE_PREFIX) :].strip().decode("ascii", errors="ignore")
                        if stage in STAGES:
                            try:
                                self.stages.put_nowait(stage)
                            except queue.Full:
                                pass
                # Do not retain an arbitrarily long stderr line.
                if len(pending) > 128:
                    pending.clear()
        except (OSError, ValueError):
            self.failed.set()


class HeadlessRunner:
    def __init__(
        self,
        index: Path,
        agent_state: Path,
        options: AgentOptions,
        runs: Path,
        timeout_seconds=150,
        max_output_bytes=1_048_576,
    ):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or type(max_output_bytes) is not int
            or not 128 <= max_output_bytes <= 16_777_216
        ):
            raise DocQAError("INVALID_CONFIG", "Invalid headless Agent resource bounds")
        self.index = Path(index).resolve()
        self.agent_state = Path(agent_state).resolve()
        self.runs = Path(runs).resolve()
        self.options = AgentOptions.model_validate(options.model_dump())
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def _environment(self):
        keys = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "TEMP", "TMP", "PATH"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in keys}
        environment.update(
            PYTHONPATH=str(Path(__file__).resolve().parents[2]),
            PYTHONNOUSERSITE="1",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PYTHONDONTWRITEBYTECODE="1",
        )
        services = [
            self.options.generator,
            self.options.verifier,
            self.options.retrieval.dense,
            self.options.retrieval.sparse,
            self.options.retrieval.reranker,
        ]
        reserved = keys | {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
        }
        for service in services:
            name = service.api_key_env
            if name is None:
                continue
            if name.upper() in reserved or name.upper().startswith(("PYTHON", "DYLD_")):
                raise DocQAError("INVALID_CONFIG", "Credential variable names a runtime setting")
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    @staticmethod
    def _reap(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait()

    def run(
        self,
        job_id: str,
        request: dict,
        *,
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> dict[str, str]:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise DocQAError("INVALID_REQUEST", "Invalid headless Agent job identifier")
        selected = _valid_request(request)
        payload = json.dumps(
            {
                "job_id": job_id,
                "index": str(self.index),
                "agent_state": str(self.agent_state),
                "options": self.options.model_dump(mode="json"),
                "request": selected.model_dump(mode="json"),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise DocQAError("RUNNER_INPUT_LIMIT", "Headless Agent input exceeds its byte limit")
        if cancelled():
            raise DocQAError("WEB_JOB_CANCELLED", "Question was cancelled")
        environment = self._environment()
        directory = self.runs / job_id
        try:
            self.runs.mkdir(parents=True, exist_ok=True)
            directory.mkdir()
        except FileExistsError as exc:
            raise DocQAError("RUNNER_WORKDIR_EXISTS", "Job work directory already exists") from exc
        except OSError as exc:
            raise DocQAError(
                "RUNNER_START_FAILED", "Cannot create headless Agent work directory"
            ) from exc
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "docqa.web.worker"],
                cwd=directory,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, ValueError) as exc:
            raise DocQAError("RUNNER_START_FAILED", "Cannot start the headless Agent") from exc
        capture = _Capture(self.max_output_bytes)
        deadline = time.monotonic() + self.timeout_seconds

        def write_input():
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (OSError, ValueError):
                pass  # Child exit/output validation supplies a bounded public error.
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        threads = [
            threading.Thread(target=write_input, daemon=True),
            threading.Thread(
                target=capture.read, args=(process.stdout,), kwargs={"stdout": True}, daemon=True
            ),
            threading.Thread(
                target=capture.read, args=(process.stderr,), kwargs={"stdout": False}, daemon=True
            ),
        ]

        def check_bounds():
            if cancelled():
                raise DocQAError("WEB_JOB_CANCELLED", "Question was cancelled")
            if capture.exceeded.is_set():
                raise DocQAError(
                    "RUNNER_OUTPUT_LIMIT", "Headless Agent output exceeds its byte limit"
                )
            if time.monotonic() >= deadline:
                raise DocQAError("RUNNER_TIMEOUT", "Headless Agent deadline elapsed")

        try:
            for thread in threads:
                thread.start()
            last_stage = None

            def deliver_stages():
                nonlocal last_stage
                while not capture.stages.empty():
                    try:
                        stage = capture.stages.get_nowait()
                    except queue.Empty:
                        break
                    if stage != last_stage:
                        progress(stage)
                        last_stage = stage

            while True:
                check_bounds()
                deliver_stages()
                if process.poll() is not None:
                    break
                time.sleep(0.025)
            for thread in threads:
                thread.join(timeout=max(0.001, deadline - time.monotonic()))
            check_bounds()
            if capture.failed.is_set() or any(thread.is_alive() for thread in threads):
                raise DocQAError("RUNNER_IO_FAILED", "Cannot read headless Agent result")
            deliver_stages()
            result = _result(bytes(capture.stdout), process.returncode)
            result = verify_saved_result(self.index, self.agent_state, job_id, selected, result)
            check_bounds()
            return result
        finally:
            self._reap(process)
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
