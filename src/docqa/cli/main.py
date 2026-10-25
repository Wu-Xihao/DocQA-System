"""docqa doc2md parse/batch/verify/doctor; JSON stdout stays machine-readable."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from docqa.cli.agent import answer_app, ask_command, source_app
from docqa.cli.agent import app as agent_app
from docqa.cli.docbase import app as docbase_app
from docqa.cli.service import app as service_app
from docqa.cli.web import app as web_app
from docqa.doc2md.config import load_options
from docqa.doc2md.pipeline import _binary_identity, batch_parse, parse_document, verify_artifact
from docqa.domain.errors import DocQAError

app = typer.Typer(help="DocQA modular tools", no_args_is_help=True, pretty_exceptions_enable=False)
doc2md = typer.Typer(help="Convert PDF/DOCX/PPTX into traceable Markdown", no_args_is_help=True)
app.add_typer(doc2md, name="doc2md")
app.add_typer(docbase_app, name="docbase")
app.add_typer(agent_app, name="agent")
app.add_typer(answer_app, name="answer")
app.add_typer(source_app, name="source")
app.add_typer(service_app, name="service")
app.add_typer(web_app, name="web")
app.command("ask")(ask_command)


def emit(value) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def fail(exc: DocQAError) -> None:
    emit({"schema_version": "docqa.error.v1", "error": exc.as_dict()})
    code = (
        2
        if exc.code in {"INVALID_REQUEST", "INVALID_CONFIG", "INVALID_OUTPUT", "UNSUPPORTED_FORMAT"}
        else 4
    )
    raise typer.Exit(code)


@doc2md.command("parse")
def parse_command(
    source: Annotated[Path, typer.Argument(help="Explicitly selected input file")],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("runtime/doc2md"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    allowed_root: Annotated[list[Path] | None, typer.Option("--allow-root")] = None,
    document_id: Annotated[str | None, typer.Option("--doc-id")] = None,
    analysis_revision: Annotated[str | None, typer.Option("--analysis-revision")] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit 5 when output needs review")
    ] = False,
):
    """Save an artifact; complete and needs-review are distinct results."""
    try:
        options = load_options(config)
        if allowed_root:
            options.allowed_roots = [path.resolve() for path in allowed_root]
        if analysis_revision is not None:
            options.analysis_revision = analysis_revision
        result = parse_document(source, output, options=options, doc_id=document_id)
        emit(result.model_dump(mode="json"))
        if strict and not result.eligible_for_indexing:
            raise typer.Exit(5)
    except DocQAError as exc:
        fail(exc)


@doc2md.command("batch")
def batch_command(
    source: Annotated[Path, typer.Argument(help="Input directory, separate from output")],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("runtime/doc2md"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    recursive: Annotated[bool, typer.Option("--recursive/--no-recursive")] = True,
    types: Annotated[str, typer.Option("--types")] = "pdf,docx,pptx",
    strict: Annotated[bool, typer.Option("--strict")] = False,
):
    """Convert selected types, continuing after a corrupt individual file."""
    try:
        report = batch_parse(
            source,
            output,
            recursive=recursive,
            options=load_options(config),
            types=tuple(
                item.strip().lower().lstrip(".") for item in types.split(",") if item.strip()
            ),
        )
        emit(report)
        if report["failed"]:
            raise typer.Exit(4)
        if strict and report["needs_review"]:
            raise typer.Exit(5)
    except DocQAError as exc:
        fail(exc)


@doc2md.command("verify")
def verify_command(
    artifact: Annotated[Path, typer.Argument(help="Artifact directory, not just MD file")],
):
    """Check manifest, snapshot, IR, images, quality and UTF-8 source mapping."""
    try:
        emit(verify_artifact(artifact))
    except DocQAError as exc:
        fail(exc)


@doc2md.command("doctor")
def doctor_command(config: Annotated[Path | None, typer.Option("--config", "-c")] = None):
    """Inspect local binary availability; does not call VL or parse documents."""
    try:
        options = load_options(config)
        emit(
            {
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "ocr": _binary_identity(options.tesseract_executable),
                "office": _binary_identity(options.office_executable),
                "vision": {
                    "mode": options.vision.mode,
                    "model": options.vision.model,
                    "revision": options.vision.revision,
                    "connection_tested": False,
                },
            }
        )
    except DocQAError as exc:
        fail(exc)


if __name__ == "__main__":
    app()
