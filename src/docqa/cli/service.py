"""Persistent service controls. Commands enqueue work unless explicitly run/work."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from docqa.domain.errors import DocQAError

app = typer.Typer(
    help="Manage monitored sources, persistent jobs and incremental publication",
    no_args_is_help=True,
)
source_app = typer.Typer(
    help="Register, inspect and discover monitored directories", no_args_is_help=True
)
document_app = typer.Typer(
    help="Inspect and logically remove or restore documents", no_args_is_help=True
)
images_app = typer.Typer(
    help="Inspect image analysis and queue explicit reanalysis", no_args_is_help=True
)
index_app = typer.Typer(
    help="Publish, inspect and verify the managed DocBase", no_args_is_help=True
)
job_app = typer.Typer(help="Inspect, retry and cancel persistent jobs", no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(document_app, name="document")
app.add_typer(images_app, name="images")
app.add_typer(index_app, name="index")
app.add_typer(job_app, name="job")

State = Annotated[
    Path, typer.Option("--state", help="Service state root; keep outside monitored directories")
]
Config = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Service YAML including parse/retrieval options"),
]
DEFAULT_STATE = Path("runtime/service")


def _service(state: Path, config: Path | None):
    # Lazy imports avoid the CLI main/emit cycle and keep --help independent of I/O.
    from docqa.service.config import load_options
    from docqa.service.pipeline import Service

    return Service(state, options=load_options(config))


def _emit(value: Any) -> None:
    from docqa.cli.main import emit

    emit(value)


def _fail(exc: DocQAError) -> None:
    from docqa.cli.main import fail

    fail(exc)


def _call(state: Path, config: Path | None, action: Callable[[Any], Any]) -> Any:
    try:
        result = action(_service(state, config))
        _emit(result)
        return result
    except DocQAError as exc:
        _fail(exc)
    except OSError:
        _fail(DocQAError("STORAGE_IO_FAILED", "Cannot access service state or selected files"))
    except ValueError:
        _fail(DocQAError("INVALID_REQUEST", "Invalid service request or configuration"))


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and (
        result.get("state") == "failed" or result.get("status") == "failed"
    )


def _run_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if _failed(result) or result.get("failed") or result.get("failed_jobs"):
        return True
    return any(
        _failed(job)
        for key in ("jobs", "results")
        if isinstance(result.get(key), list)
        for job in result[key]
    )


@source_app.command("add")
def source_add(
    path: Annotated[Path, typer.Argument(help="Existing directory to monitor")],
    state: State = DEFAULT_STATE,
    config: Config = None,
    recursive: Annotated[bool, typer.Option("--recursive/--no-recursive")] = True,
    types: Annotated[
        str | None,
        typer.Option("--types", help="Comma-separated types; defaults to Service config"),
    ] = None,
    category: Annotated[
        str, typer.Option("--category", help="Document classification")
    ] = "default",
    interval_seconds: Annotated[
        float | None,
        typer.Option("--interval-seconds", min=1, help="Scheduled scan interval; config default"),
    ] = None,
) -> None:
    """Register a source; existing pending or historical work remains inspectable."""
    _call(
        state,
        config,
        lambda service: service.add_source(
            path,
            recursive=recursive,
            types=None
            if types is None
            else tuple(
                item.strip().lower().lstrip(".") for item in types.split(",") if item.strip()
            ),
            category=category,
            interval_seconds=interval_seconds,
        ),
    )


@source_app.command("list")
def source_list(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """List registered sources, including their scheduling state."""
    _call(state, config, lambda service: service.list_sources())


@source_app.command("update")
def source_update(
    source_id: Annotated[str, typer.Argument(help="Registered source ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
    enabled: Annotated[
        bool | None, typer.Option("--enabled/--disabled", help="Enable or suspend future scans")
    ] = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    interval_seconds: Annotated[float | None, typer.Option("--interval-seconds", min=0.001)] = None,
) -> None:
    """Change scheduling or classification without editing the state database."""
    _call(
        state,
        config,
        lambda service: service.update_source(
            source_id,
            enabled=enabled,
            category=category,
            interval_seconds=interval_seconds,
        ),
    )


@source_app.command("remove")
def source_remove(
    source_id: Annotated[str, typer.Argument(help="Registered source ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Disable/remove a source and queue index exclusion; preserve source files and history."""
    _call(state, config, lambda service: service.remove_source(source_id))


@source_app.command("search")
def source_search(
    source_id: Annotated[
        str, typer.Argument(help="Source whose configured types and recursion apply")
    ],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Read-only discovery of selected files; does not parse or change the index."""
    _call(state, config, lambda service: service.discover(source_id))


@app.command("scan")
def scan_command(
    source_id: Annotated[
        str | None, typer.Argument(help="One source, or all enabled sources")
    ] = None,
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Queue an incremental scan; execute it later with work or run."""
    _call(state, config, lambda service: service.enqueue_scan(source_id))


@app.command("parse")
def parse_command(
    path: Annotated[Path, typer.Argument(help="Explicit file inside a selected monitored source")],
    state: State = DEFAULT_STATE,
    config: Config = None,
    source: Annotated[str | None, typer.Option("--source", help="Registered source ID")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Reparse even when content hash is unchanged")
    ] = False,
) -> None:
    """Queue a single-document parse under the source boundary and type policy."""
    _call(state, config, lambda service: service.enqueue_parse(path, source_id=source, force=force))


@app.command("documents")
def documents_command(
    state: State = DEFAULT_STATE,
    config: Config = None,
    source: Annotated[str | None, typer.Option("--source")] = None,
    include_removed: Annotated[bool, typer.Option("--include-removed")] = False,
) -> None:
    """List tracked documents; optionally include logically removed entries."""
    _call(
        state,
        config,
        lambda service: service.list_documents(source_id=source, include_removed=include_removed),
    )


@document_app.command("show")
def document_show(
    doc_id: Annotated[str, typer.Argument(help="Service document ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Inspect a document's current parser artifact, source and last result."""
    _call(state, config, lambda service: service.document(doc_id))


@document_app.command("remove")
def document_remove(
    doc_id: Annotated[str, typer.Argument(help="Service document ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Logically exclude a document and queue publication; never delete the original."""
    _call(state, config, lambda service: service.remove_document(doc_id))


@document_app.command("restore")
def document_restore(
    doc_id: Annotated[str, typer.Argument(help="Logically removed document ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Restore tracking and queue work while retaining the document's identity."""
    _call(state, config, lambda service: service.restore_document(doc_id))


@images_app.command("list")
def images_list(
    doc_id: Annotated[str, typer.Argument(help="Service document ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Inspect images and their current OCR/VL provenance and review state."""
    _call(state, config, lambda service: service.images(doc_id))


@images_app.command("reparse")
def images_reparse(
    doc_id: Annotated[str, typer.Argument(help="Service document ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Queue image reanalysis through a new document artifact; preserve prior versions."""
    _call(state, config, lambda service: service.reparse_images(doc_id))


@index_app.command("build")
def index_build(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """Queue publication of the active, eligible artifact set."""
    _call(state, config, lambda service: service.enqueue_build())


@index_app.command("status")
def index_status(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """Inspect current managed generation and pending publication state."""
    _call(state, config, lambda service: service.index_status())


@index_app.command("verify")
def index_verify(
    state: State = DEFAULT_STATE,
    config: Config = None,
    generation: Annotated[
        str | None, typer.Option("--generation", help="Historical or current generation")
    ] = None,
) -> None:
    """Verify index hashes, source assets and generation contracts without rebuilding."""
    _call(state, config, lambda service: service.verify_index(generation_id=generation))


@job_app.command("list")
def job_list(
    state: State = DEFAULT_STATE,
    config: Config = None,
    job_state: Annotated[
        str | None, typer.Option("--job-state", help="Queue state filter, e.g. pending or failed")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """List persistent jobs, newest first, within a bounded result count."""
    _call(state, config, lambda service: service.queue.list(state=job_state, limit=limit))


@job_app.command("show")
def job_show(
    job_id: Annotated[str, typer.Argument(help="Persistent job ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Inspect one job, its attempts, result or stable error."""
    _call(state, config, lambda service: service.queue.get(job_id))


@job_app.command("retry")
def job_retry(
    job_id: Annotated[str, typer.Argument(help="Failed job ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Requeue a retryable failed job under the queue's attempt limits."""
    _call(state, config, lambda service: service.queue.retry(job_id))


@job_app.command("cancel")
def job_cancel(
    job_id: Annotated[str, typer.Argument(help="Pending job ID")],
    state: State = DEFAULT_STATE,
    config: Config = None,
) -> None:
    """Cancel pending work; does not interrupt in-progress filesystem or model calls."""
    _call(state, config, lambda service: service.queue.cancel(job_id))


@app.command("work")
def work_command(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """Process at most one pending job; JSON null means no runnable job."""
    if _failed(_call(state, config, lambda service: service.process_next())):
        raise typer.Exit(4)


@app.command("run")
def run_command(
    state: State = DEFAULT_STATE,
    config: Config = None,
    once: Annotated[
        bool, typer.Option("--once", help="Schedule due scans and drain a bounded batch")
    ] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", min=0.01)] = 3,
    max_ticks: Annotated[
        int | None, typer.Option("--max-ticks", min=1, help="Bound the foreground scheduler loop")
    ] = None,
) -> None:
    """Run the foreground worker; stop with Ctrl+C or the separate stop command."""
    if once and max_ticks is not None:
        _fail(DocQAError("INVALID_REQUEST", "--once and --max-ticks cannot be combined"))
        return
    try:
        result = _call(
            state,
            config,
            lambda service: (
                service.run_once()
                if once
                else service.run(poll_seconds=poll_seconds, max_ticks=max_ticks)
            ),
        )
    except KeyboardInterrupt:
        _emit({"schema_version": "docqa.service.run.v1", "status": "interrupted"})
        raise typer.Exit(130) from None
    if _run_failed(result):
        raise typer.Exit(5)


@app.command("status")
def status_command(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """Inspect source, document, queue and publication health."""
    _call(state, config, lambda service: service.status())


@app.command("stop")
def stop_command(state: State = DEFAULT_STATE, config: Config = None) -> None:
    """Request a cooperative foreground-loop stop; current job finishes first."""
    _call(state, config, lambda service: service.request_stop())
