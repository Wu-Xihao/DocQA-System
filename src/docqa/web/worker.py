"""Single-request, no-shell entry point launched only by HeadlessRunner."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import portalocker
from pydantic import BaseModel, ConfigDict, ValidationError

from docqa.agent.config import AgentOptions
from docqa.agent.pipeline import answer_question
from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError
from docqa.web.runner import MAX_INPUT_BYTES, WorkerRequest, _strict_json, verify_saved_result


class WorkerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str
    index: str
    agent_state: str
    options: AgentOptions
    request: WorkerRequest


@contextlib.contextmanager
def managed_writer(envelope):
    directory = Path(envelope.agent_state)
    if directory.name != envelope.job_id or directory.parent.name != "agent":
        yield
        return
    database = _io_path(directory.parent.parent / "questions.sqlite")

    def allowed():
        if database.is_file():
            with sqlite3.connect(database, timeout=10) as db:
                row = db.execute(
                    "SELECT state,cancel_requested FROM jobs WHERE job_id=?", (envelope.job_id,)
                ).fetchone()
            if row is None or row[0] != "running" or row[1]:
                raise DocQAError("WEB_JOB_CANCELLED", "Managed question was cancelled or deleted")

    allowed()
    _io_path(directory.parent).mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(_io_path(directory.with_suffix(".writer.lock")), timeout=0):
        allowed()
        yield


def main(raw=None, *, persistent=False) -> int:
    protocol_output = sys.stdout
    try:
        if raw is None:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise DocQAError("RUNNER_INPUT_LIMIT", "Headless Agent input exceeds its byte limit")
        try:
            envelope = WorkerEnvelope.model_validate(_strict_json(raw.decode("utf-8")))
            if not re.fullmatch(r"[0-9a-f]{32}", envelope.job_id):
                raise ValueError("Invalid job identity")
            if (
                not Path(envelope.index).is_absolute()
                or not Path(envelope.agent_state).is_absolute()
            ):
                raise ValueError("Server paths must be absolute")
        except (ValidationError, ValueError, TypeError, UnicodeError) as exc:
            raise DocQAError("INVALID_REQUEST", "Invalid headless Agent input") from exc
        request = envelope.request

        def stage(value):
            label = envelope.job_id + ":" + value if persistent else value
            print("DOCQA_STAGE:" + label, file=sys.stderr, flush=True)

        def preview(items):
            rows, seen = [], set()
            for item in items:
                if item.is_heading or item.chunk_id in seen:
                    continue
                seen.add(item.chunk_id)
                rows.append({"source_name": item.source_name[:256], "text": item.text[:360]})
                if len(rows) == 4:
                    break
            directory = Path(envelope.agent_state)
            temporary = directory / "retrieval-preview.tmp"
            temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            temporary.replace(directory / "retrieval-preview.json")

        # Native dependencies sometimes print diagnostics; stdout is reserved
        # for the one-line result receipt, and all stderr stays private.
        with managed_writer(envelope), contextlib.redirect_stdout(sys.stderr):
            stage("running")
            if request.mode == "chat":
                from docqa.web.chat import direct_chat

                stage("composing")
                result = direct_chat(
                    envelope.job_id, request, envelope.options, envelope.agent_state
                )
                verify_saved_result(
                    Path(envelope.index),
                    Path(envelope.agent_state),
                    envelope.job_id,
                    request,
                    result,
                )
                print(json.dumps(result, separators=(",", ":")), file=protocol_output, flush=True)
                return 0
            answer = answer_question(
                envelope.index,
                request.question,
                output=envelope.agent_state,
                options=envelope.options,
                caller="web:" + envelope.job_id,
                categories=request.categories,
                doc_ids=request.doc_ids,
                generation_id=request.generation_id,
                required_topics=request.topics,
                output_format=request.output_format,
                use_planner=request.use_planner,
                context_questions=request.context_questions,
                progress=stage,
                evidence_preview=preview,
            )
            result = {
                "answer_id": answer.answer_id,
                "session_id": answer.session_id,
                "status": answer.status,
            }
            verify_saved_result(
                Path(envelope.index), Path(envelope.agent_state), envelope.job_id, request, result
            )
            stage("complete")
        print(json.dumps(result, separators=(",", ":")), file=protocol_output, flush=True)
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, DocQAError) else "RUNNER_WORKER_FAILED"
        # An exception may embed credentials or untrusted document text. Only a
        # stable code crosses this process boundary; no traceback is printed.
        print(
            json.dumps({"error": {"code": code}}, separators=(",", ":")),
            file=protocol_output,
            flush=True,
        )
        return 4


if __name__ == "__main__":
    if sys.argv[1:] == ["--persistent"]:
        while raw := sys.stdin.buffer.readline(MAX_INPUT_BYTES + 2):
            if len(raw) > MAX_INPUT_BYTES + 1:
                raise SystemExit(4)
            main(raw, persistent=True)
    else:
        raise SystemExit(main())
