"""Controlled ask and external CodeAgent evidence/session commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from docqa.agent.config import load_options
from docqa.agent.pipeline import (
    answer_question,
    create_evidence_session,
    finalize_answer,
    retrieve_in_session,
)
from docqa.agent.providers import HttpChatClient
from docqa.agent.sessions import SessionStore
from docqa.docbase.config import load_options as load_retrieval_options
from docqa.domain.answers import AnswerDraft
from docqa.domain.errors import DocQAError

app = typer.Typer(
    help="Knowledge-consumption configuration and evidence sessions", no_args_is_help=True
)
session_app = typer.Typer(help="Version-pinned external CodeAgent evidence", no_args_is_help=True)
answer_app = typer.Typer(help="Validate, display and resolve saved answers", no_args_is_help=True)
source_app = typer.Typer(
    help="Resolve system-owned evidence and citation IDs", no_args_is_help=True
)
app.add_typer(session_app, name="session")


def emit(value):
    from docqa.cli.main import emit as output

    output(value)


def fail(exc):
    from docqa.cli.main import fail as output

    output(exc)


def options_for(config, retrieval_config, demo):
    options = load_options(config)
    if retrieval_config:
        options.retrieval = load_retrieval_options(retrieval_config)
    if demo:
        options.mode = "demo"
        options.retrieval.mode = "demo"
    return options


def result_exit(result):
    if result.status == "failed":
        raise typer.Exit(4)
    if result.status in {"partial", "conflicting_evidence"} or result.degraded:
        raise typer.Exit(5)


def ask_command(
    question: Annotated[str, typer.Argument()],
    index: Annotated[Path, typer.Option("--index", "-i")] = Path("runtime/docbase"),
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    retrieval_config: Annotated[Path | None, typer.Option("--retrieval-config")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    category: Annotated[list[str] | None, typer.Option("--category")] = None,
    document: Annotated[list[str] | None, typer.Option("--doc-id")] = None,
    topic: Annotated[list[str] | None, typer.Option("--topic")] = None,
    generation: Annotated[str | None, typer.Option("--generation")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "markdown",
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Use model decomposition; default keeps original question"),
    ] = False,
):
    try:
        result = answer_question(
            index,
            question,
            output=state,
            options=options_for(config, retrieval_config, demo),
            categories=category,
            doc_ids=document,
            generation_id=generation,
            required_topics=topic,
            output_format=output_format,
            use_planner=plan,
        )
        emit(result.model_dump(mode="json"))
        result_exit(result)
    except DocQAError as exc:
        fail(exc)
    except (OSError, ValidationError):
        fail(DocQAError("AGENT_STORAGE_FAILED", "Cannot read/write agent state or validate input"))


@app.command("doctor")
def doctor_command(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    retrieval_config: Annotated[Path | None, typer.Option("--retrieval-config")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
):
    try:
        options = options_for(config, retrieval_config, demo)
        services = {}
        for role in ("generator", "verifier"):
            try:
                if options.mode == "demo":
                    from docqa.agent.providers import DemoChatClient

                    fingerprint = DemoChatClient(role=role).fingerprint()
                else:
                    fingerprint = HttpChatClient(getattr(options, role), role=role).fingerprint()
                services[role] = {"configured": True, "profile": fingerprint}
            except DocQAError as exc:
                services[role] = {"configured": False, "error_code": exc.code}
        emit(
            {
                "mode": options.mode,
                "services": services,
                "retrieval_mode": options.retrieval.mode,
                "connection_tested": False,
                "external_host_tested": False,
                "budgets": {
                    key: getattr(options, key)
                    for key in (
                        "max_rounds",
                        "max_queries",
                        "max_model_calls",
                        "max_evidence_bytes",
                        "max_output_bytes",
                        "deadline_seconds",
                    )
                },
            }
        )
    except DocQAError as exc:
        fail(exc)


@session_app.command("create")
def create_command(
    question: Annotated[str, typer.Argument()],
    index: Annotated[Path, typer.Option("--index", "-i")] = Path("runtime/docbase"),
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    retrieval_config: Annotated[Path | None, typer.Option("--retrieval-config")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    category: Annotated[list[str] | None, typer.Option("--category")] = None,
    document: Annotated[list[str] | None, typer.Option("--doc-id")] = None,
    generation: Annotated[str | None, typer.Option("--generation")] = None,
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
    topic: Annotated[list[str] | None, typer.Option("--topic")] = None,
):
    try:
        result = create_evidence_session(
            index,
            question,
            output=state,
            options=options_for(config, retrieval_config, demo),
            categories=category,
            doc_ids=document,
            generation_id=generation,
            caller=caller,
            required_topics=topic,
        )
        emit(result.model_dump(mode="json"))
    except DocQAError as exc:
        fail(exc)


@session_app.command("query")
def session_query_command(
    session: Annotated[str, typer.Argument()],
    question: Annotated[str, typer.Argument()],
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    retrieval_config: Annotated[Path | None, typer.Option("--retrieval-config")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    category: Annotated[list[str] | None, typer.Option("--category")] = None,
    document: Annotated[list[str] | None, typer.Option("--doc-id")] = None,
    round_no: Annotated[int, typer.Option("--round")] = 1,
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
):
    try:
        result = retrieve_in_session(
            session,
            question,
            output=state,
            options=options_for(config, retrieval_config, demo),
            categories=category,
            doc_ids=document,
            round_no=round_no,
            caller=caller,
        )
        emit(result.model_dump(mode="json"))
        if result.degraded or not result.complete:
            raise typer.Exit(5)
    except DocQAError as exc:
        fail(exc)


@session_app.command("show")
def show_session_command(
    session: Annotated[str, typer.Argument()],
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
):
    try:
        store = SessionStore(state)
        emit(
            {
                "session": store.get(session, caller=caller, require_open=False).model_dump(
                    mode="json"
                ),
                "evidence": [
                    item.model_dump(mode="json")
                    for item in store.items(session, caller=caller, require_open=False)
                ],
            }
        )
    except DocQAError as exc:
        fail(exc)


@session_app.command("close")
def close_session_command(
    session: Annotated[str, typer.Argument()],
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
):
    try:
        emit(SessionStore(state).close(session, caller=caller).model_dump(mode="json"))
    except DocQAError as exc:
        fail(exc)


@answer_app.command("validate")
def validate_command(
    draft: Annotated[Path, typer.Argument()],
    session: Annotated[str, typer.Option("--session")],
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    retrieval_config: Annotated[Path | None, typer.Option("--retrieval-config")] = None,
    demo: Annotated[bool, typer.Option("--demo")] = False,
    topic: Annotated[list[str] | None, typer.Option("--topic")] = None,
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
):
    try:
        if draft.stat().st_size > 1048576:
            raise ValueError("Draft size limit")
        value = AnswerDraft.model_validate_json(draft.read_bytes())
        if topic:
            from docqa.agent.planning import conservative_plan

            question = SessionStore(state).get(session, caller=caller).question
            topics = conservative_plan(question, topic).required_topics
        else:
            topics = None
        result = finalize_answer(
            session,
            value,
            output=state,
            options=options_for(config, retrieval_config, demo),
            caller=caller,
            topics=topics,
        )
        emit(result.model_dump(mode="json"))
        result_exit(result)
    except DocQAError as exc:
        fail(exc)
    except (OSError, ValueError, ValidationError):
        fail(DocQAError("INVALID_REQUEST", "Cannot read a valid bounded answer-draft JSON"))


@answer_app.command("show")
def show_answer_command(
    answer: Annotated[str, typer.Argument()],
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
):
    try:
        emit(SessionStore(state).load_answer(answer).model_dump(mode="json"))
    except DocQAError as exc:
        fail(exc)


@source_app.command("resolve")
def resolve_command(
    state: Annotated[Path, typer.Option("--state")] = Path("runtime/agent"),
    answer: Annotated[str | None, typer.Option("--answer")] = None,
    citation: Annotated[str | None, typer.Option("--citation")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
    evidence: Annotated[str | None, typer.Option("--evidence")] = None,
    caller: Annotated[str, typer.Option("--caller")] = "local-codeagent",
):
    try:
        store = SessionStore(state)
        if answer and citation and not session and not evidence:
            emit(store.resolve_citation(answer, citation))
        elif session and evidence and not answer and not citation:
            emit(store.resolve_evidence(session, evidence, caller=caller))
        else:
            raise DocQAError(
                "INVALID_REQUEST", "Select exactly answer+citation or session+evidence IDs"
            )
    except DocQAError as exc:
        fail(exc)
