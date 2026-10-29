"""Owned, version-bound image associations; reference images are not answer evidence."""

from __future__ import annotations

import io
import json
from hashlib import sha256
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from docqa.agent.sessions import SessionStore
from docqa.docbase.storage import _io_path, _safe_path
from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError


def checksum(path):
    result = sha256()
    with _io_path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


class ImageService:
    def __init__(self, manager, store):
        self.manager, self.store = manager, store

    def gallery(self, owner, answer_id, *, citation_id=None, offset=0, limit=24):
        job = self.manager.store.by_answer(answer_id, owner)
        answer = self.manager.answer(job)
        selected = [
            c for c in answer.citations if citation_id is None or c.citation_id == citation_id
        ]
        if citation_id and not selected:
            raise DocQAError("CITATION_NOT_FOUND", "Citation was not found")
        # A figure referenced by multiple citations keeps its strongest role.
        selected.sort(key=lambda c: c.source_span.figure_id is None)
        resolved = SessionStore(self.manager.agent_state(job["job_id"])).resolve_citations(
            answer_id, [c.citation_id for c in selected]
        )
        rows, seen, source_cache = [], set(), {}
        for citation in selected:
            source = resolved[citation.citation_id]
            artifact = Path(source["artifact_dir"])
            if citation.artifact_id not in source_cache:
                ir = DocumentIR.model_validate_json(
                    _io_path(artifact / "document.ir.json").read_bytes()
                )
                manifest = json.loads(_io_path(artifact / "manifest.json").read_bytes())
                source_cache[citation.artifact_id] = ir, manifest
            ir, manifest = source_cache[citation.artifact_id]
            assets = {asset.asset_id: asset for asset in ir.assets}
            origin_block = next(b for b in ir.blocks if b.block_id == citation.source_span.block_id)
            orders = {f: b.order for b in ir.blocks for f in b.figure_ids}
            for figure in ir.figures:
                direct = figure.figure_id == citation.source_span.figure_id
                relation = "direct" if direct else None
                if not relation:
                    for locator in citation.locators:
                        if locator.slide and locator.slide == figure.locator.slide:
                            relation = "same_slide"
                        elif (
                            locator.locator_type == "pdf_page"
                            and locator.page == figure.locator.page
                        ):
                            relation = "same_page"
                        elif (
                            locator.locator_type == "docx_object"
                            and figure.locator.locator_type == "docx_object"
                            and abs(orders.get(figure.figure_id, -1000) - origin_block.order) <= 2
                        ):
                            relation = "adjacent_block"
                if not relation or (citation.artifact_id, figure.figure_id) in seen:
                    continue
                seen.add((citation.artifact_id, figure.figure_id))
                asset = assets[figure.asset_id]
                snapshot = _safe_path(artifact.parent.parent, manifest["snapshot_ref"])
                identity = sha256(
                    f"{owner}:{answer_id}:{citation.citation_id}:{figure.figure_id}".encode()
                ).hexdigest()[:32]
                record = {
                    "id": identity,
                    "answer_id": answer_id,
                    "citation_id": citation.citation_id,
                    "generation_id": answer.generation_id,
                    "doc_id": citation.doc_id,
                    "artifact_id": citation.artifact_id,
                    "figure_id": figure.figure_id,
                    "asset_id": asset.asset_id,
                    "asset_sha256": asset.sha256,
                    "artifact": str(artifact),
                    "relative_path": asset.relative_path,
                    "manifest_sha256": checksum(artifact / "manifest.json"),
                    "snapshot": str(snapshot),
                    "snapshot_sha256": ir.content_sha256,
                    "caption": figure.caption,
                    "source_name": ir.source_name,
                    "locator": figure.locator.model_dump(mode="json"),
                    "role": "evidence" if direct else "reference",
                    "related_by": relation,
                    "image_kind": "page_render" if figure.full_page else "figure",
                    "semantic_state": figure.semantic_state,
                    "width": asset.width,
                    "height": asset.height,
                }
                rows.append(record)
        page = rows[offset : offset + limit]
        public = []
        for record in page:
            # Handles bind the original validated identities. Subsequent image
            # reads check the targeted snapshot, manifest and asset hashes,
            # rather than scanning every unrelated document for each thumbnail.
            self.store.object_put("media", owner, record)
            public.append(
                {
                    key: val
                    for key, val in record.items()
                    if key
                    not in {
                        "artifact",
                        "relative_path",
                        "snapshot",
                        "manifest_sha256",
                        "snapshot_sha256",
                        "asset_sha256",
                    }
                }
                | {
                    "thumbnail_url": f"/api/v1/media/{record['id']}/content?variant=thumbnail",
                    "preview_url": f"/api/v1/media/{record['id']}/content?variant=preview",
                }
            )
        return {
            "items": public,
            "total": len(rows),
            "next_cursor": offset + limit if offset + limit < len(rows) else None,
        }

    def preview(self, owner, identity, variant):
        record = self.store.object_get("media", identity, owner)
        self.manager.store.by_answer(record["answer_id"], owner)
        artifact = Path(record["artifact"])
        path = _safe_path(artifact, record["relative_path"])
        if (
            checksum(artifact / "manifest.json") != record["manifest_sha256"]
            or checksum(Path(record["snapshot"])) != record["snapshot_sha256"]
            or checksum(path) != record["asset_sha256"]
        ):
            raise DocQAError("SOURCE_HASH_MISMATCH", "Image source integrity validation failed")
        if _io_path(path).stat().st_size > self.manager.options.max_asset_bytes:
            raise DocQAError("ASSET_TOO_LARGE", "Image exceeds preview byte limit")
        try:
            with Image.open(_io_path(path)) as picture:
                if picture.width * picture.height > 20_000_000:
                    raise DocQAError("ASSET_TOO_LARGE", "Image exceeds preview pixel limit")
                picture = picture.convert("RGB")
                picture.thumbnail((480, 360) if variant == "thumbnail" else (2000, 2000))
                output = io.BytesIO()
                picture.save(output, "PNG")
                if output.tell() > self.manager.options.max_asset_bytes:
                    raise DocQAError("ASSET_TOO_LARGE", "Converted preview exceeds limit")
                return output.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise DocQAError("IMAGE_PREVIEW_UNAVAILABLE", "Cannot decode image") from error
