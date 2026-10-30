"""Repeatable quality gates and latency baselines; no private content in reports."""

import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from docqa.agent.pipeline import answer_question
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError


class QualityCase(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    question: str = Field(min_length=1, max_length=4000)
    expected_status: list[
        Literal[
            "answered",
            "partial",
            "insufficient_evidence",
            "conflicting_evidence",
            "failed",
            "cancelled",
        ]
    ] = Field(min_length=1, max_length=6)
    must_include: list[str] = Field(default_factory=list, max_length=100)
    must_not_include: list[str] = Field(default_factory=list, max_length=100)
    required_doc_ids: list[str] = Field(default_factory=list, max_length=100)
    categories: list[str] | None = None
    doc_ids: list[str] | None = None


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    at = (len(values) - 1) * fraction
    low, high = math.floor(at), math.ceil(at)
    return round(values[low] + (values[high] - values[low]) * (at - low), 6)


def write_report(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return value


def evaluate(index, cases_path, output, options, *, repeat=1, allow_http=False):
    if not 1 <= repeat <= 10:
        raise DocQAError("INVALID_REQUEST", "Repeat must be 1..10")
    if options.mode != "demo" and not allow_http:
        raise DocQAError(
            "QUALITY_HTTP_DISABLED", "Real model evaluation requires explicit --allow-http"
        )
    index, output = Path(index).resolve(), Path(output).resolve()
    if output.is_relative_to(index) or index.is_relative_to(output):
        raise DocQAError("INVALID_OUTPUT", "Evaluation output and index must be separate")
    source = Path(cases_path)
    if source.stat().st_size > 1048576:
        raise DocQAError("INVALID_REQUEST", "Quality set exceeds 1 MiB")
    values = json.loads(source.read_text("utf-8"))
    if not isinstance(values, list):
        raise DocQAError("INVALID_REQUEST", "Quality set must be a JSON array")
    cases = [QualityCase.model_validate(case) for case in values]
    if not 1 <= len(cases) <= 500 or len({c.id for c in cases}) != len(cases):
        raise DocQAError("INVALID_REQUEST", "Use 1..500 unique quality cases")
    run = output / uuid4().hex
    rows, phases = [], defaultdict(list)
    for turn in range(repeat):
        for case in cases:
            started = time.perf_counter()
            try:
                answer = answer_question(
                    index,
                    case.question,
                    output=run / f"{case.id}-{turn}",
                    options=options,
                    categories=case.categories,
                    doc_ids=case.doc_ids,
                )
            except DocQAError as error:
                rows.append(
                    {
                        "id": case.id,
                        "iteration": turn + 1,
                        "passed": False,
                        "checks": {"execution": False},
                        "status": "failed",
                        "stop_reason": error.code,
                        "seconds": round(time.perf_counter() - started, 6),
                        "model_tasks": 0,
                        "repairs": 0,
                        "synthetic": options.mode == "demo",
                        "verified": False,
                    }
                )
                continue
            text = "\n".join(claim.text for claim in answer.claims)
            cited = {citation.doc_id for citation in answer.citations}
            checks = {
                "status": answer.status in case.expected_status,
                "required_text": all(t.casefold() in text.casefold() for t in case.must_include),
                "forbidden_text": not any(
                    t.casefold() in text.casefold() for t in case.must_not_include
                ),
                "required_sources": set(case.required_doc_ids).issubset(cited),
            }
            metrics = next((e for e in answer.trace if e.get("stage") == "performance"), {})
            for name, seconds in metrics.get("operations", {}).items():
                phases[name].append(seconds)
            rows.append(
                {
                    "id": case.id,
                    "iteration": turn + 1,
                    "passed": all(checks.values()),
                    "checks": checks,
                    "status": answer.status,
                    "stop_reason": answer.stop_reason,
                    "seconds": round(time.perf_counter() - started, 6),
                    "model_tasks": metrics.get("model_tasks", 0),
                    "repairs": sum(e.get("stage") == "repair" for e in answer.trace),
                    "synthetic": answer.synthetic,
                    "verified": answer.verified,
                }
            )
    report = {
        "schema_version": "docqa.quality.v1",
        "evaluation": "annotated lexical/source/status regression; not an automatic semantic judge",
        "passed": all(r["passed"] for r in rows),
        "cases": len(rows),
        "pass_rate": sum(r["passed"] for r in rows) / len(rows),
        "latency": {
            "p50": percentile([r["seconds"] for r in rows], 0.5),
            "p95": percentile([r["seconds"] for r in rows], 0.95),
        },
        "phases": {
            name: {"p50": percentile(v, 0.5), "p95": percentile(v, 0.95)}
            for name, v in phases.items()
        },
        "results": rows,
    }
    return write_report(run / "report.json", report) | {"report": str(run / "report.json")}


def diagnostics(state, *, limit=100):
    state = Path(state).resolve()
    database = state / "questions.sqlite"
    if not database.exists():
        raise DocQAError("WEB_STORAGE_FAILED", "No Web task database")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT job_id,state,error_code,created_at,json_extract(result,'$.answer_id') FROM jobs ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 1000),),
        ).fetchall()
    timings, rules, details, waits = [], Counter(), [], []
    validation_rules, phases = Counter(), defaultdict(list)
    for jid, status, code, created, answer_id in rows:
        if len(jid) != 32 or any(c not in "0123456789abcdef" for c in jid):
            continue
        path = state / "runs" / jid / "timing.json"
        elapsed = None
        if path.exists() and path.stat().st_size <= 65536:
            try:
                value = json.loads(path.read_text("utf-8"))
                if not isinstance(value, dict):
                    continue
            except (ValueError, OSError):
                value = {}
            elapsed = value.get("elapsed_seconds")
            if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed >= 0:
                timings.append(elapsed)
            else:
                elapsed = None
            if isinstance(value.get("started_at"), (int, float)) and math.isfinite(
                value["started_at"]
            ):
                waits.append(max(0, value["started_at"] - created))
        details.append(
            {"job_id": jid, "state": status, "error_code": code, "runner_seconds": elapsed}
        )
        rules[code or status] += 1
        if (
            isinstance(answer_id, str)
            and len(answer_id) == 32
            and all(c in "0123456789abcdef" for c in answer_id)
        ):
            answer_path = state / "agent" / jid / "answers" / answer_id / "answer.json"
            try:
                if answer_path.stat().st_size > 4 * 1024 * 1024:
                    continue
                trace = json.loads(answer_path.read_text("utf-8")).get("trace", [])
                for entry in trace:
                    rule = entry.get("validation_rule")
                    if (
                        isinstance(rule, str)
                        and 0 < len(rule) <= 100
                        and all(c.isascii() and (c.isalnum() or c == "_") for c in rule)
                    ):
                        validation_rules[rule] += 1
                    if entry.get("stage") == "performance":
                        for name, seconds in entry.get("operations", {}).items():
                            if (
                                isinstance(name, str)
                                and len(name) <= 100
                                and isinstance(seconds, (int, float))
                                and math.isfinite(seconds)
                                and seconds >= 0
                            ):
                                phases[name].append(seconds)
            except (OSError, ValueError, AttributeError, TypeError):
                pass
    return {
        "schema_version": "docqa.diagnostics.v1",
        "jobs": len(details),
        "outcomes": dict(rules),
        "runner_p50": percentile(timings, 0.5),
        "runner_p95": percentile(timings, 0.95),
        "queue_p95": percentile(waits, 0.95),
        "validation_rules": dict(validation_rules),
        "phases": {
            name: {"p50": percentile(v, 0.5), "p95": percentile(v, 0.95)}
            for name, v in phases.items()
        },
        "details": details,
    }
