"""Read-only knowledge-base statistics for the dashboard panel.

Everything here comes from local metadata: the frozen generation, the Service
registry and Web task records. No document text, question text or model call
is involved, and nothing here is evidence for an answer.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import PurePath

from docqa.domain.errors import DocQAError
from docqa.quality import percentile

_COMPONENT = {
    "block_text": "text",
    "table_row": "table",
    "figure_ocr": "figure",
    "figure_vl": "figure",
}
_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()


def _composition(index, generation_id):
    """Chunk-level composition, cached per immutable generation."""
    with _LOCK:
        if generation_id in _CACHE:
            return _CACHE[generation_id]
    from docqa.docbase.retrieval import generation_directory
    from docqa.docbase.storage import load_generation

    content, origins, sizes = Counter(), Counter(), []
    figures = set()
    with load_generation(generation_directory(index, generation_id)) as generation:
        for chunk in generation.chunks:
            kinds = {_COMPONENT[span.component] for span in chunk.source_spans}
            if chunk.figure_ids:
                kinds.add("figure")
                figures.update((chunk.doc_id, f) for f in chunk.figure_ids)
            for kind in kinds:
                content[kind] += 1
            for origin in {span.origin for span in chunk.source_spans}:
                origins[origin] += 1
            sizes.append(chunk.index_utf8_bytes)
    value = {
        "content_types": dict(content),
        "origins": dict(origins),
        "chunk_bytes_p50": percentile(sizes, 0.5),
        "chunk_bytes_p95": percentile(sizes, 0.95),
        "figures_indexed": len(figures),
    }
    with _LOCK:
        if len(_CACHE) > 8:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[generation_id] = value
    return value


def knowledge_base(workspace):
    catalog = workspace.display_catalog()
    documents = catalog["documents"]
    formats = Counter(
        PurePath(d["source_name"]).suffix.lower().lstrip(".") or "other" for d in documents
    )
    return {
        "generation_id": catalog["generation_id"],
        "documents": len(documents),
        "chunks": catalog["chunks"],
        "recall_routes": catalog["recall_routes"],
        "synthetic": catalog["synthetic"],
        "formats": dict(formats.most_common()),
        "categories": dict(Counter(d["category"] for d in documents).most_common()),
        "index_modes": dict(Counter(d["quality"] for d in documents).most_common()),
        **_composition(workspace.options.index, catalog["generation_id"]),
    }


def service(workspace):
    path = workspace.options.service_state
    if not path or not (path / "service.sqlite").is_file():
        return None
    from docqa.service.pipeline import Service

    maintenance = Service(path)
    documents = [d for d in maintenance.list_documents() if not d["removed"]]
    return {
        "documents": len(documents),
        "states": dict(Counter(d["state"] for d in documents).most_common()),
        "pending_updates": sum(d["good_version"] != d["target_version"] for d in documents),
        "jobs": maintenance.queue.stats(),
        "sources": len(maintenance.list_sources()),
    }


def questions(state, limit=500):
    database = state / "questions.sqlite"
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT state,error_code,json_extract(payload,'$.mode'),"
            "json_extract(result,'$.status'),created_at,updated_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    states, modes, statuses, errors, durations, daily = (
        Counter(),
        Counter(),
        Counter(),
        Counter(),
        [],
        Counter(),
    )
    for job_state, error, mode, status, created, updated in rows:
        states[job_state] += 1
        modes[mode or "retrieval"] += 1
        if status and (mode or "retrieval") != "chat":
            statuses[status] += 1
        if error:
            errors[error] += 1
        if job_state == "succeeded" and updated and created and updated >= created:
            durations.append(updated - created)
        daily[time.strftime("%Y-%m-%d", time.localtime(created))] += 1
    retrieval = sum(statuses.values())
    return {
        "total": len(rows),
        "states": dict(states),
        "modes": dict(modes),
        "answer_status": dict(statuses.most_common()),
        "answered_rate": round(statuses["answered"] / retrieval, 3) if retrieval else None,
        "top_errors": dict(errors.most_common(5)),
        "latency_p50": percentile(durations, 0.5),
        "latency_p95": percentile(durations, 0.95),
        "daily": dict(sorted(daily.items())[-14:]),
    }


def search(workspace):
    items = workspace.search_metrics.snapshot()
    times = [item["ms"] for item in items if item["status"] != "failed"]
    return {
        "count": len(items),
        "failed": sum(item["status"] == "failed" for item in items),
        "p50_ms": percentile(times, 0.5),
        "p95_ms": percentile(times, 0.95),
        "recent_ms": [round(item["ms"]) for item in items[-30:]],
    }


def collect(workspace):
    errors, result = [], {"schema_version": "docqa.stats.v1", "generated_at": time.time()}
    for name, function in (
        ("knowledge_base", lambda: knowledge_base(workspace)),
        ("service", lambda: service(workspace)),
        ("questions", lambda: questions(workspace.options.state)),
        ("search", lambda: search(workspace)),
    ):
        try:
            result[name] = function()
        except (DocQAError, OSError, sqlite3.Error, ValueError) as exc:
            result[name] = None
            errors.append(f"{name}:{getattr(exc, 'code', type(exc).__name__)}")
    result["errors"] = errors
    return result


def install(app, workspace):
    @app.get("/api/v1/stats")
    def stats():
        return collect(workspace)
