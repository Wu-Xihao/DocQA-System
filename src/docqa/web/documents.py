"""Read-only original-content projection from already verified source packages."""

from pathlib import Path

from fastapi.responses import FileResponse

from docqa.docbase.retrieval import generation_directory
from docqa.docbase.storage import SCHEMA, _checksum, _io_path, _read_json, _safe_path
from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError
from docqa.integrity import VerifiedCache, file_stamp, tree_stamp

_SOURCE_PACKAGES = VerifiedCache(max_entries=32, max_bytes=1024 * 1024)
_READER_IR = VerifiedCache(max_entries=16, max_bytes=64 * 1024 * 1024)


def document_package(index, identity, generation=None):
    """Verify the selected document, not every other document/vector in the KB."""
    from docqa.doc2md.pipeline import verify_artifact

    directory = _io_path(generation_directory(index, generation))
    try:
        manifest = _read_json(directory / "manifest.json", maximum=16 * 1024 * 1024)
        if manifest["schema_version"] != SCHEMA or manifest["generation_id"] != directory.name:
            raise ValueError("Generation identity differs")
        members_path = _safe_path(directory, "members.json")
        metadata = manifest["files"]["members.json"]
        if (
            members_path.stat().st_size != metadata["size"]
            or _checksum(members_path) != metadata["sha256"]
        ):
            raise ValueError("Member snapshot differs")
        members = _read_json(members_path)
        if len(members) != manifest["member_count"] or len({m["doc_id"] for m in members}) != len(
            members
        ):
            raise ValueError("Invalid member snapshot")
        member = next((m for m in members if m["doc_id"] == identity), None)
        if member is None:
            raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Document is outside this version")
        relative = "sources/artifacts/" + member["artifact_id"]
        if member.get("artifact_ref", relative) != relative:
            raise ValueError("Invalid source reference")
        artifact = _safe_path(directory, relative)
        source_manifest_path = _safe_path(artifact, "manifest.json")
        bound = manifest["files"][relative + "/manifest.json"]
        if (
            source_manifest_path.stat().st_size != bound["size"]
            or _checksum(source_manifest_path) != bound["sha256"]
        ):
            raise ValueError("Source manifest differs from the generation")
        source = _read_json(source_manifest_path)
        if (source["doc_id"], source["artifact_id"]) != (identity, member["artifact_id"]):
            raise ValueError("Source identity differs")
        snapshot = _safe_path(artifact.parent.parent, source["snapshot_ref"])

        def signature():
            value = tree_stamp(artifact)
            try:
                return (value, file_stamp(snapshot)) if value is not None else None
            except OSError:
                return None

        key = str(artifact)
        with _SOURCE_PACKAGES.guard(key):
            before = signature()
            verified = _SOURCE_PACKAGES.get(key, before)
            if verified is None:
                verify_artifact(artifact)
                ir = _read_json(_safe_path(artifact, "document.ir.json"))
                verified = {
                    name: ir[name]
                    for name in ("doc_id", "revision_id", "artifact_id", "source_name", "category")
                }
                after = signature()
                if before is not None and after != before:
                    raise ValueError("Source changed during validation")
                _SOURCE_PACKAGES.put(key, after, verified, 2048)
            if any(member.get(name) != value for name, value in verified.items()):
                raise ValueError("Member metadata differs from its source")
        return artifact, snapshot, member["source_name"]
    except DocQAError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DocQAError("GENERATION_CORRUPT", "Cannot verify the selected source") from error


def original_file(path, name, max_bytes, *, inline=False):
    path = _io_path(path)
    if path.stat().st_size > max_bytes:
        raise DocQAError("ASSET_TOO_LARGE", "Original file exceeds download limit")
    pdf = path.suffix.lower() == ".pdf"
    return FileResponse(
        path,
        filename=name,
        media_type="application/pdf" if pdf else "application/octet-stream",
        content_disposition_type="inline" if inline and pdf else "attachment",
    )


def original_content(artifact, *, offset=0, limit=200, max_bytes=33554432, focus_block=None):
    path = _io_path(Path(artifact) / "document.ir.json")
    if path.stat().st_size > max_bytes:
        raise DocQAError("ASSET_TOO_LARGE", "Original text exceeds preview budget")
    ir = DocumentIR.model_validate_json(path.read_bytes())
    native = [b for b in ir.blocks if b.origin == "native" and b.type != "figure"]
    if offset == 0 and focus_block:
        offset = max(0, next((i for i, b in enumerate(native) if b.block_id == focus_block), 0) - 3)
    selected = native[offset : offset + limit]
    return {
        "source_name": ir.source_name,
        "format": Path(ir.source_name).suffix.lower().lstrip("."),
        "blocks": [
            {
                "id": b.block_id,
                "type": b.type,
                "text": b.text,
                "table_rows": b.table_rows,
                "heading_level": b.heading_level,
                "location": "；".join(loc.display_label for loc in b.locators),
            }
            for b in selected
        ],
        "next_offset": offset + len(selected) if offset + len(selected) < len(native) else None,
        "total_blocks": len(native),
        "start_offset": offset,
        "notice": "原文件的已提取正文视图；版式、图片和未解析内容以原文件为准。",
    }


def document_images(artifact, base_url, *, offset=0, limit=24, query="", max_bytes=33554432):
    """All extracted occurrences, not just images cited in the current answer."""
    ir = _image_ir(artifact, max_bytes)
    figures = ir.figures[offset : offset + limit]
    separator = "&" if query else "?"
    return {
        "source_name": ir.source_name,
        "total": len(ir.figures),
        "items": [
            {
                "id": f.figure_id,
                "caption": f.caption or f"图片 {offset + i + 1}",
                "location": f.locator.display_label,
                "state": f.semantic_state,
                "analysis_required": f.analysis_required,
                "full_page": f.full_page,
                "thumbnail": base_url
                + "/"
                + f.figure_id
                + "/content"
                + query
                + separator
                + "size=480",
                "preview": base_url
                + "/"
                + f.figure_id
                + "/content"
                + query
                + separator
                + "size=2000",
            }
            for i, f in enumerate(figures)
        ],
        "next_offset": offset + len(figures) if offset + len(figures) < len(ir.figures) else None,
        "notice": "展示该版本文档已提取的全部图片（含页面渲染及归档图）；图片可查看不等于已获准作为回答证据。未提取内容请核对原文件。",
    }


def _image_ir(artifact, maximum):
    path = _safe_path(Path(artifact), "document.ir.json")
    if path.stat().st_size > maximum:
        raise DocQAError("ASSET_TOO_LARGE", "Document image metadata exceeds preview budget")
    key = str(path)
    with _READER_IR.guard(key):
        before = file_stamp(path)
        cached = _READER_IR.get(key, before)
        if cached is not None:
            return cached
        raw = path.read_bytes()
        value = DocumentIR.model_validate_json(raw)
        after = file_stamp(path)
        if after != before:
            raise DocQAError("SOURCE_HASH_MISMATCH", "Document changed during preview")
        _READER_IR.put(key, after, value, len(raw) * 4)
        return value


def document_image(artifact, figure_id, *, size=480, max_bytes=33554432):
    import io

    from fastapi.responses import Response
    from PIL import Image, UnidentifiedImageError

    ir = _image_ir(artifact, max_bytes)
    figure = next((f for f in ir.figures if f.figure_id == figure_id), None)
    asset = next((a for a in ir.assets if figure and a.asset_id == figure.asset_id), None)
    if not asset:
        raise DocQAError("WORKSPACE_ITEM_NOT_FOUND", "Image is outside the selected document")
    path = _safe_path(Path(artifact), asset.relative_path)
    if path.stat().st_size > max_bytes:
        raise DocQAError("ASSET_TOO_LARGE", "Image exceeds preview budget")
    try:
        raw = path.read_bytes()
        # Package verification precedes this helper; bind this read to the same asset.
        from hashlib import sha256

        if sha256(raw).hexdigest() != asset.sha256:
            raise DocQAError("SOURCE_HASH_MISMATCH", "Image changed during preview")
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width * picture.height > 20_000_000:
                raise DocQAError("ASSET_TOO_LARGE", "Image exceeds preview pixel budget")
            picture.thumbnail((size, size))
            output = io.BytesIO()
            picture.convert("RGBA").save(output, format="PNG")
        return Response(
            output.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=60"},
        )
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise DocQAError("IMAGE_PREVIEW_UNAVAILABLE", "Image cannot be safely previewed") from exc
