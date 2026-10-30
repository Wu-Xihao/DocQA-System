"""Explicit offline backup/restore and dry-run-first derived-output cleanup."""

import sqlite3
from pathlib import Path

import typer

from docqa.domain.errors import DocQAError

app = typer.Typer(
    help="Offline backup, verify, restore and conservative Web GC", no_args_is_help=True
)


def call(function, *args, **kwargs):
    from docqa.cli.main import emit, fail

    try:
        emit(function(*args, **kwargs))
    except (DocQAError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        fail(
            error
            if isinstance(error, DocQAError)
            else DocQAError(
                "MAINTENANCE_FAILED", "Maintenance failed; no unsafe overwrite was performed"
            )
        )


@app.command("backup")
def create(
    service_state: Path = typer.Option(...),
    web_state: Path = typer.Option(...),
    output: Path = typer.Option(...),
):
    from docqa.operations import backup

    call(backup, service_state, web_state, output)


@app.command("verify-backup")
def verify(bundle: Path):
    from docqa.operations import verify_backup

    call(lambda: {"valid": bool(verify_backup(bundle))})


@app.command("restore")
def recover(
    bundle: Path, service_state: Path = typer.Option(...), web_state: Path = typer.Option(...)
):
    from docqa.operations import restore

    call(restore, bundle, service_state, web_state)


@app.command("gc")
def cleanup(
    web_state: Path = typer.Option(...),
    apply: bool = False,
    min_age_hours: int = typer.Option(24, min=1),
):
    from docqa.operations import cleanup_web

    call(cleanup_web, web_state, apply=apply, min_age_seconds=min_age_hours * 3600)
