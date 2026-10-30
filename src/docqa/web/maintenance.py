"""Bounded same-origin controls for server-local directory monitoring."""

import time

from fastapi import Request
from pydantic import Field, field_validator

from docqa.domain.documents import Contract
from docqa.domain.formats import UPLOAD_FORMATS


class WatchCreate(Contract):
    path: str = Field(min_length=1, max_length=4096)
    types: list[str] = Field(
        default_factory=lambda: list(UPLOAD_FORMATS),
        min_length=1,
        max_length=len(UPLOAD_FORMATS),
    )

    @field_validator("types")
    @classmethod
    def known_types(cls, value):
        if len(value) != len(set(value)) or not set(value) <= set(UPLOAD_FORMATS):
            raise ValueError("Unknown or repeated document type")
        return value
    recursive: bool = True
    interval_seconds: float = Field(default=1800, ge=15, le=604800)
    category: str = Field(default="auto", min_length=1, max_length=100)


class WatchPatch(Contract):
    enabled: bool


def poll_monitors(workspace):
    if (
        not workspace.enabled
        or workspace.options.service_mode != "auto"
        or time.monotonic() - workspace._last_monitor_poll < 15
    ):
        return
    workspace._last_monitor_poll = time.monotonic()
    entries = workspace.store.objects("watch_source", limit=1000)
    if not entries:
        return
    service = workspace.service()
    active = {v["source_id"] for v in service.list_sources() if v["enabled"] and not v["removed"]}
    if active & {v["id"] for v in entries}:
        workspace._spawn_service(service)


def install(app, workspace):
    @app.get("/api/v1/maintenance/sources")
    def sources(request: Request):
        service = workspace.service()
        entries = {
            item["id"]
            for item in workspace.store.objects("watch_source", request.state.client_id, 1000)
        }
        return {
            "allowed_roots": [str(p) for p in service.options.allowed_source_roots],
            "items": [
                s for s in service.list_sources() if s["source_id"] in entries and not s["removed"]
            ],
            "service_mode": workspace.options.service_mode,
        }

    @app.post("/api/v1/maintenance/sources", status_code=201)
    def create(value: WatchCreate, request: Request):
        service = workspace.service()
        source = service.add_source(
            value.path,
            types=value.types,
            recursive=value.recursive,
            interval_seconds=value.interval_seconds,
            category=value.category,
        )
        source = service.update_source(
            source["source_id"],
            enabled=True,
            types=value.types,
            recursive=value.recursive,
            interval_seconds=value.interval_seconds,
            category=value.category,
        )
        workspace.store.object_put(
            "watch_source", request.state.client_id, {"id": source["source_id"]}
        )
        workspace._spawn_service(service)
        return source

    def selected(identity, request):
        workspace.store.object_get("watch_source", identity, request.state.client_id)
        return workspace.service()

    @app.patch("/api/v1/maintenance/sources/{identity}")
    def pause(identity: str, value: WatchPatch, request: Request):
        service = selected(identity, request)
        return service.update_source(identity, enabled=value.enabled)

    @app.post("/api/v1/maintenance/sources/{identity}/scan", status_code=202)
    def scan(identity: str, request: Request):
        service = selected(identity, request)
        service.enqueue_scan(identity)
        workspace._spawn_service(service)
        return {"state": "queued"}

    @app.get("/api/v1/maintenance/sources/{identity}/files")
    def discover(identity: str, request: Request):
        service = selected(identity, request)
        return service.discover(identity)

    @app.delete("/api/v1/maintenance/sources/{identity}", status_code=202)
    def remove(identity: str, request: Request):
        service = selected(identity, request)
        service.remove_source(identity)
        workspace._spawn_service(service)
        return {"state": "removing"}

    @app.get("/api/v1/documents/{identity}/analysis")
    def analysis(identity: str, request: Request):
        return workspace.service().images(identity)

    @app.post("/api/v1/documents/{identity}/analysis/retry", status_code=202)
    def reanalyze(identity: str, request: Request):
        service = workspace.service()
        service.reparse_images(identity)
        workspace._spawn_service(service)
        return {"state": "queued"}

    @app.get("/api/v1/maintenance/index")
    def index_status(request: Request):
        return workspace.service().index_status()

    @app.post("/api/v1/maintenance/index/build", status_code=202)
    def index_build(request: Request):
        service = workspace.service()
        service.enqueue_build()
        workspace._spawn_service(service)
        return {"state": "queued"}
