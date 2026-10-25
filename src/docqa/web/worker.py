"""Single-request, no-shell entry point launched only by HeadlessRunner."""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from docqa.agent.config import AgentOptions
from docqa.agent.pipeline import answer_question
from docqa.domain.errors import DocQAError
from docqa.web.runner import MAX_INPUT_BYTES, WorkerRequest, _strict_json, verify_saved_result


class WorkerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str
    index: str
    agent_state: str
    options: AgentOptions
    request: WorkerRequest


def main() -> int:
    protocol_output = sys.stdout
    try:
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
        # Native dependencies sometimes print diagnostics; stdout is reserved
        # for the one-line result receipt, and all stderr stays private.
        with contextlib.redirect_stdout(sys.stderr):
            print("DOCQA_STAGE:running", file=sys.stderr, flush=True)
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
            )
            result = {
                "answer_id": answer.answer_id,
                "session_id": answer.session_id,
                "status": answer.status,
            }
            verify_saved_result(
                Path(envelope.index), Path(envelope.agent_state), envelope.job_id, request, result
            )
            print("DOCQA_STAGE:complete", file=sys.stderr, flush=True)
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
    raise SystemExit(main())
