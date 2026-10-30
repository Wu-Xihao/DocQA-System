"""Local HTTP transport: no arbitrary paths, no model HTML, no second QA pipeline."""

from __future__ import annotations

import io
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import portalocker
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from starlette.exceptions import HTTPException

from docqa.agent.rendering import render_answer
from docqa.agent.sessions import SessionStore
from docqa.docbase.storage import _io_path
from docqa.domain.errors import DocQAError
from docqa.service.pipeline import Service
from docqa.service.scanner import canonical_path
from docqa.web.config import WebOptions
from docqa.web.models import QuestionRequest
from docqa.web.security import LocalSecurity, csrf_token
from docqa.web.static_files import WebStaticFiles
from docqa.web.tasks import QuestionManager
from docqa.web.workspace import Workspace
from docqa.web.workspace_routes import install_workspace_routes

STATIC = Path(__file__).parent / "static"


def _secret(root):
    _io_path(root).mkdir(parents=True, exist_ok=True)
    path = root / ".web-secret"
    with portalocker.Lock(root / ".secret.lock", timeout=10):
        if path.exists():
            canonical_path(path, directory=False)
            value = _io_path(path).read_bytes()
            if len(value) != 32:
                raise DocQAError("WEB_SECRET_INVALID", "Web session key has an invalid size")
            return value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=_io_path(root), prefix=".key-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                value = secrets.token_bytes(32)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(_io_path(path))
            return value
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _error(code, message, status=400, retryable=False):
    return JSONResponse(
        {
            "schema_version": "docqa.web-error.v1",
            "error": {"code": code, "message": message, "retryable": retryable},
        },
        status_code=status,
    )


def _status(code):
    if code in {
        "WEB_JOB_NOT_FOUND",
        "ANSWER_NOT_FOUND",
        "CITATION_NOT_FOUND",
        "CONVERSATION_NOT_FOUND",
        "WORKSPACE_ITEM_NOT_FOUND",
    }:
        return 404
    if code == "WEB_QUEUE_FULL":
        return 429
    if code in {
        "INVALID_REQUEST",
        "INVALID_PLAN",
        "INVALID_CONFIG",
        "INVALID_GENERATION",
        "ARTIFACT_PLAN_INVALID",
    }:
        return 422
    if code in {
        "PROFILE_MISMATCH",
        "PUBLISH_STALE",
        "CONVERSATION_CHANGED",
        "CONVERSATION_BUSY",
        "IDEMPOTENCY_CONFLICT",
        "SCOPE_CHANGED",
        "SCOPE_EMPTY",
        "ARTIFACT_RELATIONS_MISSING",
        "ARTIFACT_EVIDENCE_MISSING",
        "ARTIFACT_KEYWORDS_MISSING",
        "EXPORT_NOT_READY",
        "UPLOAD_ALREADY_COMMITTED",
    }:
        return 409
    if code in {"ASSET_TOO_LARGE", "BODY_TOO_LARGE", "ARTIFACT_TOO_LARGE"}:
        return 413
    if code == "FORMAT_UNSUPPORTED":
        return 415
    return 503


def _resources(answer_id, citation_id, image_count=0):
    base = f"/api/v1/queries/{answer_id}/citations/{citation_id}"
    return dict(
        metadata=base,
        markdown=base + "/content?kind=markdown",
        snapshot=base + "/content?kind=snapshot",
        original=base + "/content?kind=snapshot&inline=true",
        preview=base + "/original-preview",
        images_gallery=base + "/document-images",
        images=[base + f"/content?kind=image&image={index}" for index in range(image_count)],
    )


def web_answer(result):
    value = result.model_dump(mode="json", exclude={"trace", "answer_markdown"})
    value["performance"] = next(
        (event for event in reversed(result.trace) if event.get("stage") == "performance"), None
    )
    value["topic_texts"] = {
        item["topic_id"]: item["text"]
        for event in result.trace
        if event.get("stage") == "plan"
        for item in event.get("topics", [])
    }
    value["answer_markdown"] = render_answer(result)  # No local filesystem links.
    for citation in value["citations"]:
        count = len(citation.pop("image_paths"))
        citation.pop("markdown_path")
        citation.pop("snapshot_path")
        citation["resources"] = _resources(result.answer_id, citation["citation_id"], count)
    return value


def create_app(options: WebOptions | None = None, *, runner_factory=None):
    options = (options or WebOptions()).model_copy(deep=True)
    options.state = canonical_path(options.state, must_exist=False, directory=True)
    options.index = canonical_path(options.index, must_exist=False, directory=True)
    options.separate_paths()
    key = _secret(options.state)
    manager = QuestionManager(options, runner_factory=runner_factory)
    workspace = Workspace(manager)

    @asynccontextmanager
    async def lifespan(_app):
        manager.start()
        workspace.start()
        try:
            yield
        finally:
            workspace.stop()
            manager.stop()

    app = FastAPI(
        title=options.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.manager = manager
    app.state.options = options
    app.state.workspace = workspace

    @app.exception_handler(DocQAError)
    async def application_error(_request, exc):
        # Messages are deliberately not relayed from arbitrary low-level errors.
        return _error(
            exc.code,
            "本次操作未完成，请检查配置、任务状态或索引。",
            _status(exc.code),
            exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def input_error(_request, _exc):
        return _error("INVALID_REQUEST", "请求字段、长度或类型无效。", 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return _error(
            "HTTP_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
            "请求的资源或操作不可用。",
            exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request, _exc):
        return _error("WEB_INTERNAL_ERROR", "服务处理失败，未返回内部路径或异常正文。", 500)

    def client_id(request):
        return request.state.client_id

    def owned_answer(answer_id, request):
        job = manager.store.by_answer(answer_id, client_id(request))
        return job, manager.answer(job)

    def project_job(job, *, full=False):
        value = {
            key: job[key]
            for key in (
                "job_id",
                "state",
                "stage",
                "cancel_requested",
                "error_code",
                "created_at",
                "updated_at",
            )
        }
        value.update(
            question=job["payload"]["question"], generation_id=job["payload"]["generation_id"]
        )
        value["mode"] = job["payload"].get("mode", "retrieval")
        value["answer_id"] = job["result"].get("answer_id") if job["result"] else None
        value["chat_id"] = job["result"].get("chat_id") if job["result"] else None
        value["answer_status"] = job["result"]["status"] if job["result"] else None
        value["retrieval_preview"] = manager.retrieval_preview(job)
        value["chat_preview"] = manager.chat_preview(job)
        if full:
            result = manager.answer(job)
            value["answer"] = web_answer(result) if result else None
            value["chat"] = manager.chat(job)
        return value

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    app.mount("/static", WebStaticFiles(directory=STATIC, follow_symlink=False), name="static")

    @app.get("/api/v1/health/live")
    def live():
        return {"status": "live"}

    @app.get("/api/v1/bootstrap")
    def bootstrap(request: Request):
        errors, catalog = [], None
        try:
            catalog = manager.catalog(display_only=True)
        except DocQAError as exc:
            errors.append(exc.code)
        from docqa.agent.providers import make_chat_clients
        from docqa.docbase.providers import make_providers

        try:
            make_chat_clients(options.agent)
            make_providers(options.agent.retrieval)
        except DocQAError as exc:
            errors.append(exc.code)
        question_ready = bool(manager.ready and catalog is not None and not errors)
        service = None
        if options.service_state and (options.service_state / "service.sqlite").is_file():
            try:
                maintenance = Service(options.service_state)
                documents = maintenance.list_documents()
                service = {"documents": len(documents), "jobs": maintenance.queue.stats()}
                for name in ("desired_revision", "assembled_revision", "synced_revision"):
                    service[name] = maintenance.registry.meta(name, 0)
                service["unsatisfied_count"] = sum(
                    not d["removed"] and d["good_version"] != d["target_version"] for d in documents
                )
                service["source_errors"] = [
                    item["scan_error"] for item in maintenance.list_sources() if item["scan_error"]
                ]
            except (DocQAError, OSError):
                errors.append("SERVICE_STATUS_UNAVAILABLE")
        return {
            "csrf_token": csrf_token(key, client_id(request)),
            "settings": {
                "app_name": options.app_name,
                "mode": "builtin",
                "synthetic": bool(
                    options.agent.mode == "demo" or (catalog and catalog["synthetic"])
                ),
            },
            "knowledge_base": catalog,
            "worker_ready": manager.ready,
            "ready": question_ready,
            "errors": errors,
            "service": service,
        }

    @app.get("/api/v1/health/ready")
    def ready():
        if not manager.ready:
            raise DocQAError("WORKER_UNAVAILABLE", "Worker is not running")
        catalog = manager.catalog()
        from docqa.agent.providers import make_chat_clients
        from docqa.docbase.providers import make_providers

        make_chat_clients(options.agent)
        make_providers(options.agent.retrieval)
        return {
            "ready": True,
            "generation_id": catalog["generation_id"],
            "models_connection_tested": False,
        }

    @app.post("/api/v1/questions", status_code=202)
    def ask(value: QuestionRequest, request: Request):
        return project_job(manager.submit(client_id(request), value))

    @app.get("/api/v1/jobs")
    def jobs(request: Request):
        return {"jobs": [project_job(job) for job in manager.store.list(client_id(request))]}

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str, request: Request):
        return project_job(manager.store.get(job_id, client_id(request)), full=True)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        return project_job(manager.store.cancel(job_id, client_id(request)), full=True)

    @app.get("/api/v1/queries/{answer_id}")
    def answer(answer_id: str, request: Request):
        _, result = owned_answer(answer_id, request)
        return web_answer(result)

    def citation_source(answer_id, citation_id, request):
        job, result = owned_answer(answer_id, request)
        store = SessionStore(manager.agent_state(job["job_id"]))
        source = store.resolve_citation(answer_id, citation_id)
        citation = next(
            (item for item in result.citations if item.citation_id == citation_id), None
        )
        if citation is None:
            raise DocQAError("CITATION_NOT_FOUND", "Citation was not found")
        return source, citation

    @app.get("/api/v1/queries/{answer_id}/citations/{citation_id}")
    def citation(answer_id: str, citation_id: str, request: Request, include_preview: bool = False):
        source, selected = citation_source(answer_id, citation_id, request)
        public = {
            key: value
            for key, value in source.items()
            if key
            in {
                "quote",
                "generation_id",
                "chunk_id",
                "doc_id",
                "revision_id",
                "artifact_id",
                "source_name",
                "origin",
                "locators",
                "markdown_start_line",
                "markdown_end_line",
                "markdown_range_precision",
                "component",
                "text_start_codepoint",
                "text_end_codepoint",
                "quote_start_codepoint",
                "quote_end_codepoint",
                "quote_coordinate_basis",
            }
        }
        public["citation_id"] = citation_id
        public["resources"] = _resources(answer_id, citation_id, len(source["images"]))
        public["origin"] = selected.origin
        if include_preview:
            from docqa.web.documents import original_content

            public["original_content"] = original_content(
                source["artifact_dir"],
                max_bytes=options.max_asset_bytes,
                focus_block=selected.source_span.block_id,
            )
        return public

    @app.get("/api/v1/queries/{answer_id}/citations/{citation_id}/original-preview")
    def original_preview(
        answer_id: str,
        citation_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=200),
    ):
        from docqa.web.documents import original_content

        source, selected = citation_source(answer_id, citation_id, request)
        return original_content(
            source["artifact_dir"],
            offset=offset,
            limit=limit,
            max_bytes=options.max_asset_bytes,
            focus_block=selected.source_span.block_id,
        )

    @app.get("/api/v1/queries/{answer_id}/citations/{citation_id}/content")
    def content(
        answer_id: str,
        citation_id: str,
        request: Request,
        kind: Literal["markdown", "snapshot", "image"] = "markdown",
        image: int = Query(default=0, ge=0, le=1000),
        inline: bool = False,
    ):
        source, _ = citation_source(answer_id, citation_id, request)
        if kind == "image":
            if image >= len(source["images"]):
                raise HTTPException(404)
            path = Path(source["images"][image]["path"])
        else:
            path = Path(source["markdown_path"] if kind == "markdown" else source["snapshot_path"])
        if kind == "snapshot":
            from docqa.web.documents import original_file

            return original_file(
                path, source["source_name"], options.max_source_download_bytes, inline=inline
            )
        if _io_path(path).stat().st_size > options.max_asset_bytes:
            raise DocQAError("ASSET_TOO_LARGE", "Citation resource exceeds Web limit")
        raw = _io_path(path).read_bytes()
        if len(raw) > options.max_asset_bytes:
            raise DocQAError("ASSET_TOO_LARGE", "Citation resource exceeds Web limit")
        if kind == "image":
            try:
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.width * picture.height > 20000000:
                        raise DocQAError("ASSET_TOO_LARGE", "Image exceeds safe preview pixels")
                    output = io.BytesIO()
                    picture.convert("RGBA").save(output, format="PNG")
                    raw = output.getvalue()
                if len(raw) > options.max_asset_bytes:
                    raise DocQAError("ASSET_TOO_LARGE", "Preview exceeds Web byte limit")
                return Response(raw, media_type="image/png")
            except (UnidentifiedImageError, OSError) as exc:
                raise DocQAError(
                    "IMAGE_PREVIEW_UNAVAILABLE", "Original image cannot be safely previewed"
                ) from exc
        if kind == "markdown":
            return Response(raw, media_type="text/plain; charset=utf-8")
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=source" + path.suffix.lower()},
        )

    @app.get("/api/v1/queries/{answer_id}/citations/{citation_id}/document-images")
    def citation_document_images(
        answer_id: str,
        citation_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=48),
    ):
        from docqa.web.documents import document_images

        source, _ = citation_source(answer_id, citation_id, request)
        return document_images(
            source["artifact_dir"],
            _resources(answer_id, citation_id)["images_gallery"],
            offset=offset,
            limit=limit,
            max_bytes=options.max_asset_bytes,
        )

    @app.get(
        "/api/v1/queries/{answer_id}/citations/{citation_id}/document-images/{figure_id}/content"
    )
    def citation_document_image(
        answer_id: str,
        citation_id: str,
        figure_id: str,
        request: Request,
        size: int = Query(default=480, ge=64, le=2000),
    ):
        from docqa.web.documents import document_image

        source, _ = citation_source(answer_id, citation_id, request)
        return document_image(
            source["artifact_dir"], figure_id, size=size, max_bytes=options.max_asset_bytes
        )

    install_workspace_routes(app, workspace, project_job)
    wrapped = LocalSecurity(
        app,
        key=key,
        port=options.port,
        max_body_bytes=options.max_body_bytes,
        max_upload_bytes=options.max_upload_bytes,
    )
    wrapped.state = app.state  # Test/embedding access; never serialized to a browser.
    return wrapped
