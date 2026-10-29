"""Loopback Web launcher. Optional dependencies are loaded only when needed."""

from pathlib import Path
from typing import Annotated

import typer

from docqa.domain.errors import DocQAError

app = typer.Typer(help="Local browser UI using the built-in headless Agent", no_args_is_help=True)


@app.command("run")
def run(config: Annotated[Path | None, typer.Option("--config", "-c")] = None):
    try:
        import uvicorn

        from docqa.web.app import create_app
        from docqa.web.config import load_options

        options = load_options(config)
        application = create_app(options)
        typer.echo(f"DocQA Web: http://127.0.0.1:{options.port}", err=True)
        uvicorn.run(
            application,
            host=options.host,
            port=options.port,
            workers=1,
            proxy_headers=False,
            server_header=False,
            log_level="warning",
        )
    except ImportError:
        from docqa.cli.main import fail

        fail(
            DocQAError(
                "WEB_DEPENDENCY_MISSING", "Install requirements.txt in the docqa environment"
            )
        )
    except DocQAError as exc:
        from docqa.cli.main import fail

        fail(exc)


@app.command("doctor")
def doctor(config: Annotated[Path | None, typer.Option("--config", "-c")] = None):
    from docqa.cli.main import emit, fail
    from docqa.web.config import load_options

    try:
        options = load_options(config)
        from importlib.util import find_spec

        emit(
            dict(
                host=options.host,
                port=options.port,
                backend=options.backend,
                dependencies_available=bool(find_spec("fastapi") and find_spec("uvicorn")),
                synthetic=options.agent.mode == "demo",
                connection_tested=False,
            )
        )
    except DocQAError as exc:
        fail(exc)
