"""Same-origin workspace endpoints; every conversation/resource is owner checked."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal
from uuid import uuid4

from fastapi import Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from docqa.docbase.storage import _io_path, _safe_path
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError
from docqa.domain.formats import UPLOAD_FORMATS
from docqa.web.media import checksum
from docqa.web.models import (
    ArtifactCreate,
    ArtifactEdit,
    ConversationCreate,
    ConversationPatch,
    MessageRequest,
    UploadCreate,
)


class ExportCreate(Contract):
    format: Literal["json", "md", "csv", "svg", "png", "docx", "pptx"]


def install_workspace_routes(app, workspace, project_job):
    from docqa.web.maintenance import install

    install(app, workspace)
    store, manager = workspace.store, workspace.manager

    def owner(request):
        return request.state.client_id

    from docqa.web.search import install as install_search

    install_search(app, workspace, store, owner)
    from docqa.web.stats import install as install_stats

    install_stats(app, workspace)

    @app.get("/api/v1/workspace")
    def workspace_info(request: Request):
        return {
            "uploads_enabled": workspace.enabled,
            "service_mode": workspace.options.service_mode,
            "max_upload_bytes": workspace.options.max_upload_bytes,
            "upload_types": list(UPLOAD_FORMATS),
            "worker_error": workspace.worker_error,
            "artifact_types": ["table", "relation", "mindmap", "report", "slides", "wordcloud"],
        }

    @app.get("/api/v1/conversations")
    def conversations(
        request: Request,
        q: str = Query(default="", max_length=100),
        archived: bool = False,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return store.list(owner(request), query=q, archived=archived, offset=cursor, limit=limit)

    @app.post("/api/v1/conversations", status_code=201)
    def create_conversation(value: ConversationCreate, request: Request):
        return store.create(owner(request), value.title)

    @app.get("/api/v1/conversations/{identity}")
    def conversation(identity: str, request: Request):
        return store.get(identity, owner(request))

    @app.patch("/api/v1/conversations/{identity}")
    def patch_conversation(identity: str, value: ConversationPatch, request: Request):
        return store.patch(identity, owner(request), value.model_dump(exclude_none=True))

    @app.delete("/api/v1/conversations/{identity}")
    def delete_conversation(identity: str, request: Request):
        result = workspace.deletions.request(owner(request), "conversation", identity)
        return Response(
            json.dumps(result),
            media_type="application/json",
            status_code=200 if result["state"] == "complete" else 202,
        )

    @app.post("/api/v1/conversations/{identity}/restore")
    def restore_conversation(identity: str, request: Request):
        raise DocQAError(
            "CONVERSATION_NOT_FOUND", "Permanently deleted conversations cannot be restored"
        )

    @app.get("/api/v1/deletions/{identity}")
    def deletion_state(identity: str, request: Request):
        workspace.deletions.get(owner(request), identity)
        workspace.deletions.process(identity)
        return workspace.deletions.get(owner(request), identity)

    @app.post("/api/v1/conversations/{identity}/upgrade")
    def upgrade_conversation(identity: str, request: Request):
        return store.upgrade(identity, owner(request), workspace.display_catalog()["generation_id"])

    @app.get("/api/v1/conversations/{identity}/messages")
    def messages(
        identity: str,
        request: Request,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        result = store.messages(identity, owner(request), offset=cursor, limit=limit)
        for message in result["items"]:
            if message["role"] == "assistant":
                job = manager.store.get(message["job_id"], owner(request))
                message["job"] = project_job(job, full=True)
        return result

    @app.post("/api/v1/conversations/{identity}/messages", status_code=202)
    def send_message(identity: str, value: MessageRequest, request: Request):
        return project_job(workspace.submit(identity, owner(request), value))

    @app.post("/api/v1/messages/{identity}/retry", status_code=202)
    def retry_message(identity: str, request: Request):
        with manager.store.transaction() as db:
            row = db.execute(
                "SELECT m.* FROM ws_messages m JOIN ws_conversations c ON c.id=m.conversation WHERE m.id=? AND c.owner=? AND c.deleted=0",
                (identity, owner(request)),
            ).fetchone()
        if row is None:
            raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Message was not found")
        job = manager.store.get(row["job_id"], owner(request))
        payload = job["payload"]
        value = MessageRequest(
            mode=payload.get("mode", "retrieval"),
            question=payload["question"],
            categories=payload.get("categories"),
            doc_ids=payload.get("doc_ids"),
            topics=payload.get("topics"),
            output_format=payload.get("output_format", "markdown"),
            scope_mode="selected" if payload.get("doc_ids") else "all",
            client_request_id=uuid4().hex,
            doc_types=json.loads(row["scope"]).get("doc_types"),
        )
        selected = store.get(row["conversation"], owner(request))
        if value.mode != "chat" and selected["generation_id"] != payload["generation_id"]:
            raise DocQAError("CONVERSATION_CHANGED", "Original message belongs to an older version")
        return project_job(
            workspace.submit(
                row["conversation"],
                owner(request),
                value,
                chat_history_override=payload.get("chat_history", [])
                if value.mode == "chat"
                else None,
            )
        )

    @app.get("/api/v1/documents")
    def documents(request: Request, conversation_id: str | None = None):
        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        try:
            latest = workspace.display_catalog()
        except DocQAError as error:
            if error.code not in {"INDEX_NOT_FOUND", "GENERATION_NOT_FOUND"}:
                raise
            latest = {"documents": [], "generation_id": None, "chunks": 0, "recall_routes": []}
        catalog = (
            workspace.display_catalog(generation)
            if generation and generation != latest["generation_id"]
            else dict(latest)
        )
        catalog.update(
            latest_generation_id=latest["generation_id"],
            library_documents=latest["documents"],
            library_chunks=latest["chunks"],
        )
        catalog["uploads"] = [
            workspace.public_upload(item) for item in store.objects("upload", owner(request), 1000)
        ]
        return catalog

    @app.get("/api/v1/documents/{identity}/content")
    def document_content(identity: str, request: Request, conversation_id: str | None = None):
        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        return Response(
            workspace.document_content(identity, generation), media_type="text/plain; charset=utf-8"
        )

    @app.get("/api/v1/documents/{identity}/preview")
    def preview_document(
        identity: str,
        request: Request,
        conversation_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=200),
    ):
        from docqa.web.documents import original_content

        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        artifact, _, _ = workspace.document_source(identity, generation)
        return original_content(
            artifact, offset=offset, limit=limit, max_bytes=workspace.options.max_asset_bytes
        )

    @app.get("/api/v1/documents/{identity}/original")
    @app.get("/api/v1/documents/{identity}/original/content")
    def original_document(
        identity: str, request: Request, conversation_id: str | None = None, inline: bool = False
    ):
        from docqa.web.documents import original_file

        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        _, snapshot, name = workspace.document_source(identity, generation)
        return original_file(
            snapshot, name, workspace.options.max_source_download_bytes, inline=inline
        )

    @app.delete("/api/v1/documents/{identity}", status_code=202)
    def remove_document(identity: str, request: Request):
        service = workspace.service()
        service.remove_document(identity)
        workspace._spawn_service(service)
        return {"doc_id": identity, "state": "removing"}

    @app.get("/api/v1/documents/{identity}/images")
    def all_document_images(
        identity: str,
        request: Request,
        conversation_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=48),
    ):
        from docqa.web.documents import document_images

        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        artifact, _, _ = workspace.document_source(identity, generation)
        return document_images(
            artifact,
            f"/api/v1/documents/{identity}/images",
            offset=offset,
            limit=limit,
            query="?conversation_id=" + conversation_id if conversation_id else "",
            max_bytes=workspace.options.max_asset_bytes,
        )

    @app.get("/api/v1/documents/{identity}/images/{figure_id}/content")
    def view_document_image(
        identity: str,
        figure_id: str,
        request: Request,
        conversation_id: str | None = None,
        size: int = Query(default=480, ge=64, le=2000),
    ):
        from docqa.web.documents import document_image

        generation = (
            store.get(conversation_id, owner(request))["generation_id"] if conversation_id else None
        )
        artifact, _, _ = workspace.document_source(identity, generation)
        return document_image(
            artifact, figure_id, size=size, max_bytes=workspace.options.max_asset_bytes
        )

    @app.post("/api/v1/documents/{identity}/reprocess", status_code=202)
    def reprocess_document(identity: str, request: Request):
        service = workspace.service()
        doc = service.document(identity)
        service.enqueue_parse(doc["path"], force=True)
        for upload in store.objects("upload", owner(request), 1000):
            if upload.get("doc_id") == identity:
                upload.update(state="queued", error_code=None)
                store.object_put("upload", owner(request), upload)
        workspace._spawn_service(service)
        return {"doc_id": identity, "state": "queued"}

    @app.post("/api/v1/uploads", status_code=201)
    def new_upload(value: UploadCreate, request: Request):
        return workspace.public_upload(workspace.create_upload(owner(request), value))

    @app.put("/api/v1/uploads/{identity}/content")
    async def upload_content(identity: str, request: Request):
        return await workspace.receive_upload(owner(request), identity, request)

    @app.post("/api/v1/uploads/{identity}/commit", status_code=202)
    def commit_upload(identity: str, request: Request):
        return workspace.commit_upload(owner(request), identity)

    @app.get("/api/v1/uploads/{identity}")
    def upload_state(identity: str, request: Request):
        return workspace.public_upload(store.object_get("upload", identity, owner(request)))

    @app.delete("/api/v1/uploads/{identity}")
    def cancel_upload(identity: str, request: Request):
        record = store.object_get("upload", identity, owner(request))
        if record["state"] not in {"selected", "received", "upload_failed"}:
            raise DocQAError(
                "UPLOAD_ALREADY_COMMITTED", "Committed documents must be removed from the library"
            )
        record["state"] = "cancelled"
        store.object_put("upload", owner(request), record)
        return workspace.public_upload(record)

    @app.post("/api/v1/studio/jobs", status_code=202)
    def create_artifact(value: ArtifactCreate, request: Request):
        return workspace.create_artifact(owner(request), value)

    @app.get("/api/v1/studio/jobs/{identity}")
    def artifact_job(identity: str, request: Request):
        return store.object_get("artifact_job", identity, owner(request))

    @app.get("/api/v1/conversations/{identity}/artifacts")
    def artifacts(identity: str, request: Request):
        store.get(identity, owner(request))
        return {
            "items": [
                {k: v for k, v in item.items() if k != "sha256"}
                for item in store.objects("artifact", owner(request), 500)
                if item["conversation_id"] == identity
            ]
        }

    @app.get("/api/v1/artifacts/{identity}")
    def artifact(identity: str, request: Request):
        return workspace.artifact(owner(request), identity)

    @app.delete("/api/v1/artifacts/{identity}")
    def delete_artifact(identity: str, request: Request):
        result = workspace.deletions.request(owner(request), "artifact", identity)
        return Response(
            json.dumps(result),
            media_type="application/json",
            status_code=200 if result["state"] == "complete" else 202,
        )

    @app.post("/api/v1/artifacts/{identity}/edits", status_code=201)
    def edit_artifact(identity: str, value: ArtifactEdit, request: Request):
        return workspace.edit_artifact(owner(request), identity, value)

    @app.post("/api/v1/artifacts/{identity}/regenerate", status_code=202)
    def regenerate_artifact(identity: str, request: Request):
        artifact = workspace.artifact(owner(request), identity)
        value = ArtifactCreate(
            conversation_id=artifact["conversation_id"],
            answer_id=artifact["answer_id"],
            kind=artifact["kind"],
            title=artifact["title"],
        )
        return workspace.create_artifact(
            owner(request), value, previous_id=identity, version=artifact["version"] + 1
        )

    @app.post("/api/v1/artifacts/{identity}/exports", status_code=202)
    def create_export(identity: str, value: ExportCreate, request: Request):
        workspace.artifact(owner(request), identity)
        record = {
            "id": uuid4().hex,
            "artifact_id": identity,
            "format": value.format,
            "state": "queued",
            "created_at": time.time(),
            "error_code": None,
        }
        store.object_put("export", owner(request), record)
        return record

    @app.get("/api/v1/exports/{identity}")
    def export_state(identity: str, request: Request):
        record = store.object_get("export", identity, owner(request))
        return {k: v for k, v in record.items() if k not in {"sha256", "mime"}}

    @app.get("/api/v1/exports/{identity}/content")
    def export_content(identity: str, request: Request):
        record = store.object_get("export", identity, owner(request))
        if record["state"] != "ready":
            raise DocQAError("EXPORT_NOT_READY", "Export is not ready")
        path = _safe_path(
            workspace.options.state / "studio/exports", identity + "." + record["format"]
        )
        if checksum(path) != record["sha256"]:
            raise DocQAError("SOURCE_HASH_MISMATCH", "Export integrity check failed")
        return FileResponse(
            _io_path(path),
            media_type=record["mime"],
            filename="DocQA-" + identity[:8] + "." + record["format"],
        )

    @app.get("/api/v1/queries/{answer_id}/related-images")
    def related_images(
        answer_id: str,
        request: Request,
        citation_id: str | None = None,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=50),
    ):
        return workspace.images.gallery(
            owner(request), answer_id, citation_id=citation_id, offset=cursor, limit=limit
        )

    @app.get("/api/v1/media/{identity}/content")
    def image(
        identity: str, request: Request, variant: Literal["thumbnail", "preview"] = "thumbnail"
    ):
        return Response(
            workspace.images.preview(owner(request), identity, variant), media_type="image/png"
        )

    @app.post("/api/v1/media/{identity}/visual-check", status_code=202)
    def request_visual(identity: str, request: Request):
        return workspace.request_visual_check(owner(request), identity)

    @app.get("/api/v1/visual-checks/{identity}")
    def visual_result(identity: str, request: Request):
        return store.object_get("visual_check", identity, owner(request))

    @app.get("/api/v1/events")
    async def events(request: Request, conversation_id: str):
        client = owner(request)
        store.get(conversation_id, client)

        async def stream():
            previous = None
            streamed = set()
            live = {}
            while not await request.is_disconnected():
                try:
                    records = store.messages(conversation_id, client, limit=200)["items"]
                    jobs = [
                        project_job(manager.store.get(m["job_id"], client))
                        for m in records
                        if m["role"] == "assistant"
                    ]
                except DocQAError as error:
                    if error.code in {"CONVERSATION_NOT_FOUND", "WEB_JOB_NOT_FOUND"}:
                        return
                    raise
                if jobs and jobs[-1].get("chat_preview"):
                    latest = jobs[-1]
                    preview = latest["chat_preview"]
                    if preview["content"] != live.get(latest["job_id"]):
                        live[latest["job_id"]] = preview["content"]
                        yield (
                            "event: chat_live\ndata: "
                            + json.dumps(
                                {"kind": "chat_live", "job_id": latest["job_id"], "chat": preview},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                # Ordinary replies have their own channel and no source claims.
                # Retrieval below only emits independently checked paragraphs.
                if jobs and jobs[-1].get("chat_id") and jobs[-1]["job_id"] not in streamed:
                    latest = jobs[-1]
                    full = project_job(manager.store.get(latest["job_id"], client), full=True)
                    chat = full["chat"]
                    if latest["job_id"] in live:
                        yield (
                            "event: chat_done\ndata: "
                            + json.dumps({"kind": "chat_done", "job": full}, ensure_ascii=False)
                            + "\n\n"
                        )
                        return
                    yield (
                        "event: chat_start\ndata: "
                        + json.dumps(
                            {
                                "kind": "chat_start",
                                "job_id": latest["job_id"],
                                "chat": {**chat, "content": ""},
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    for offset in range(0, len(chat["content"]), 200):
                        if await request.is_disconnected():
                            return
                        try:
                            manager.store.get(latest["job_id"], client)
                        except DocQAError:
                            return
                        yield (
                            "event: chat_delta\ndata: "
                            + json.dumps(
                                {
                                    "kind": "chat_delta",
                                    "job_id": latest["job_id"],
                                    "text": chat["content"][offset : offset + 200],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        await asyncio.sleep(0)
                    yield (
                        "event: chat_done\ndata: "
                        + json.dumps({"kind": "chat_done", "job": full}, ensure_ascii=False)
                        + "\n\n"
                    )
                    streamed.add(latest["job_id"])
                    if not any(j["state"] in {"queued", "running"} for j in jobs):
                        return
                if (
                    jobs
                    and jobs[-1]["state"] in {"succeeded", "failed"}
                    and jobs[-1]["answer_id"]
                    and jobs[-1]["job_id"] not in streamed
                ):
                    latest = jobs[-1]
                    job = manager.store.get(latest["job_id"], client)
                    full = project_job(job, full=True)
                    answer = full.get("answer")
                    if answer:
                        header = {**answer, "claims": [], "diagram_edges": []}
                        yield (
                            "event: answer_start\ndata: "
                            + json.dumps(
                                {
                                    "kind": "answer_start",
                                    "job_id": latest["job_id"],
                                    "answer": header,
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        for claim in answer["claims"]:
                            if await request.is_disconnected():
                                return
                            # Recheck ownership/tombstones between paragraphs.
                            try:
                                manager.store.get(latest["job_id"], client)
                            except DocQAError:
                                return
                            yield (
                                "event: answer_delta\ndata: "
                                + json.dumps(
                                    {
                                        "kind": "answer_delta",
                                        "job_id": latest["job_id"],
                                        "claim": claim,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            await asyncio.sleep(0)
                        yield (
                            "event: answer_done\ndata: "
                            + json.dumps({"kind": "answer_done", "job": full}, ensure_ascii=False)
                            + "\n\n"
                        )
                        streamed.add(latest["job_id"])
                        if not any(j["state"] in {"queued", "running"} for j in jobs):
                            return
                payload = json.dumps({"jobs": jobs}, ensure_ascii=False)
                if payload != previous:
                    yield f"event: state\ndata: {payload}\n\n"
                    previous = payload
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.15 if jobs and jobs[-1].get("mode") == "chat" else 1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
