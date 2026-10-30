"""Quality/diagnostic commands default to offline operation."""

from pathlib import Path

import typer

from docqa.domain.errors import DocQAError

app = typer.Typer(
    help="Annotated quality gates, diagnostics and synthetic benchmarks", no_args_is_help=True
)


@app.command("benchmark")
def measure(
    output: Path = typer.Option(Path("runtime/benchmarks")),
    documents: int = typer.Option(4, min=2, max=100),
    repeat: int = typer.Option(3, min=2, max=10),
):
    from docqa.benchmark import benchmark
    from docqa.cli.main import emit, fail

    try:
        emit(benchmark(output, documents=documents, repeat=repeat))
    except (DocQAError, OSError, ValueError) as error:
        fail(
            error
            if isinstance(error, DocQAError)
            else DocQAError("BENCHMARK_FAILED", "Synthetic benchmark failed")
        )


@app.command("stress")
def stress_command(
    output: Path = typer.Option(Path("runtime/benchmarks")),
    documents: int = typer.Option(60, min=3, max=3000),
    rows: int = typer.Option(200, min=1, max=20000),
    queries: int = typer.Option(40, min=1, max=1000),
    workers: int = typer.Option(8, min=1, max=32),
):
    """Offline scale/robustness test on generated DOCX/XLSX/CSV; no model requests."""
    from docqa.cli.main import emit, fail
    from docqa.stress import stress

    try:
        report = stress(output, documents=documents, rows=rows, queries=queries, workers=workers)
        emit({key: value for key, value in report.items() if key != "robustness"})
        if not report["passed"]:
            raise typer.Exit(6)
    except (DocQAError, OSError, ValueError) as error:
        fail(
            error
            if isinstance(error, DocQAError)
            else DocQAError("BENCHMARK_FAILED", "Synthetic stress test failed")
        )


@app.command("eval")
def evaluate(
    cases: Path,
    index: Path = typer.Option(...),
    config: Path = typer.Option(...),
    output: Path = typer.Option(Path("runtime/quality")),
    repeat: int = typer.Option(1, min=1, max=10),
    allow_http: bool = False,
):
    from docqa.agent.config import load_options
    from docqa.cli.main import emit, fail
    from docqa.quality import evaluate as run

    try:
        result = run(
            index, cases, output, load_options(config), repeat=repeat, allow_http=allow_http
        )
        emit(result)
        if not result["passed"]:
            raise typer.Exit(5)
    except (DocQAError, OSError, ValueError) as error:
        fail(
            error
            if isinstance(error, DocQAError)
            else DocQAError("INVALID_EVALUATION_SET", "Cannot load or run evaluation")
        )


@app.command("diagnostics")
def diagnose(
    state: Path = typer.Option(Path("runtime/web-real")),
    limit: int = typer.Option(100, min=1, max=1000),
):
    from docqa.cli.main import emit, fail
    from docqa.quality import diagnostics

    try:
        emit(diagnostics(state, limit=limit))
    except DocQAError as error:
        fail(error)
