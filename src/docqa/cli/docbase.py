"""DocBase JSON commands; progress is confined to stderr."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress

from docqa.docbase.config import load_options
from docqa.docbase.evaluation import eval_retrieval
from docqa.docbase.pipeline import build_index
from docqa.docbase.providers import make_providers
from docqa.docbase.retrieval import generation_directory, query, resolve_chunk
from docqa.docbase.storage import read_current, verify_generation
from docqa.domain.errors import DocQAError

app = typer.Typer(
    help="Build, search and evaluate a traceable three-route DocBase", no_args_is_help=True
)


def _emit(value):
    from docqa.cli.main import emit

    emit(value)


def _fail(exc):
    from docqa.cli.main import fail

    fail(exc)


def _options(config, demo):
    value = load_options(config)
    if demo:
        value.mode = "demo"
    return value


@app.command("build")
def build_command(
    source: Annotated[
        Path | None, typer.Argument(help="Verified artifact, doc2md root or sources JSON")
    ] = None,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("runtime/docbase"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    demo: Annotated[
        bool, typer.Option("--demo", help="Explicit synthetic offline mechanics demo")
    ] = False,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Target set equals selected documents; otherwise merge"),
    ] = False,
    empty: Annotated[
        bool, typer.Option("--allow-empty", help="Permit explicit empty target with --replace")
    ] = False,
    progress: Annotated[bool, typer.Option("--progress/--no-progress")] = True,
):
    try:
        console = Console(stderr=True)
        with Progress(
            console=console, disable=not progress or not console.is_terminal, transient=True
        ) as bar:
            task = bar.add_task("DocBase", total=1)

            def update(stage, current, total):
                bar.update(task, description=stage, completed=current, total=max(total, 1))

            result = build_index(
                source,
                output,
                options=_options(config, demo),
                replace=replace,
                allow_empty=empty,
                progress=update,
            )
        _emit(result.model_dump(mode="json"))
    except DocQAError as exc:
        _fail(exc)


@app.command("query")
def query_command(
    question: Annotated[str, typer.Argument()],
    index: Annotated[Path, typer.Option("--index", "-i")] = Path("runtime/docbase"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    generation: Annotated[str | None, typer.Option("--generation")] = None,
    category: Annotated[list[str] | None, typer.Option("--category")] = None,
    document: Annotated[list[str] | None, typer.Option("--doc-id")] = None,
    top_k: Annotated[int | None, typer.Option("--top-k")] = None,
    mode: Annotated[
        str, typer.Option("--mode", help="hybrid | rrf | dense | bm25 | sparse")
    ] = "hybrid",
    degraded: Annotated[bool | None, typer.Option("--allow-degraded/--no-allow-degraded")] = None,
):
    try:
        result = query(
            index,
            question,
            options=_options(config, demo),
            generation_id=generation,
            categories=category,
            doc_ids=document,
            top_k=top_k,
            mode=mode,
            allow_degraded=degraded,
        )
        _emit(result.model_dump(mode="json"))
        if result.status == "degraded":
            raise typer.Exit(5)
    except DocQAError as exc:
        _fail(exc)


@app.command("verify")
def verify_command(
    index: Annotated[Path, typer.Argument()],
    generation: Annotated[str | None, typer.Option("--generation")] = None,
):
    try:
        _emit(verify_generation(generation_directory(index, generation)))
    except DocQAError as exc:
        _fail(exc)


@app.command("resolve")
def resolve_command(
    chunk_id: Annotated[str, typer.Argument()],
    index: Annotated[Path, typer.Option("--index", "-i")] = Path("runtime/docbase"),
    generation: Annotated[str | None, typer.Option("--generation")] = None,
):
    try:
        _emit(resolve_chunk(index, chunk_id, generation_id=generation))
    except DocQAError as exc:
        _fail(exc)


@app.command("test")
def test_command(
    test_set: Annotated[Path, typer.Argument(help="Frozen source-bound evaluation JSON")],
    index: Annotated[Path, typer.Option("--index", "-i")] = Path("runtime/docbase"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    generation: Annotated[str | None, typer.Option("--generation")] = None,
    top_k: Annotated[int | None, typer.Option("--top-k")] = None,
    modes: Annotated[str, typer.Option("--modes")] = "dense,bm25,sparse,rrf,hybrid",
    report: Annotated[Path | None, typer.Option("--report")] = None,
):
    try:
        result = eval_retrieval(
            index,
            test_set,
            options=_options(config, demo),
            generation_id=generation,
            top_k=top_k,
            modes=tuple(item.strip() for item in modes.split(",") if item.strip()),
        )
        if report:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        _emit(result)
        if result["failed_calls"]:
            raise typer.Exit(4)
    except DocQAError as exc:
        _fail(exc)
    except OSError:
        _fail(DocQAError("STORAGE_IO_FAILED", "Cannot write evaluation report"))


@app.command("doctor")
def doctor_command(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
):
    try:
        options = _options(config, demo)
        try:
            fingerprints = [service.fingerprint() for service in make_providers(options)]
            configured, error = True, None
        except DocQAError as exc:
            fingerprints, configured, error = [], False, exc.code
        _emit(
            {
                "mode": options.mode,
                "configured": configured,
                "services": fingerprints,
                "configuration_error": error,
                "connection_tested": False,
                "synthetic": options.mode == "demo",
                "budget_unit": "UTF-8 bytes; model tokenizer limits require service validation",
            }
        )
    except DocQAError as exc:
        _fail(exc)


@app.command("info")
def info_command(index: Annotated[Path, typer.Argument()]):
    try:
        current, sequence = read_current(index.resolve())
        _emit(
            {
                "current_generation_id": current,
                "publish_sequence": sequence,
                "validation": verify_generation(generation_directory(index)) if current else None,
            }
        )
    except DocQAError as exc:
        _fail(exc)
