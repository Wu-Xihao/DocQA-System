"""Standalone doc2md use case: snapshot -> parse -> enrich -> validate -> commit.

This local catalog belongs only to doc2md. It is not a substitute for the future
knowledge-base generation publisher, document lifecycle service or distributed jobs.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from uuid import UUID, uuid4

import portalocker

from docqa.doc2md.adapters import HttpVisionClient, LibreOfficeRenderer, TesseractOCR
from docqa.doc2md.common import ParseContext, digest
from docqa.doc2md.config import ParseOptions
from docqa.doc2md.markdown import render_markdown
from docqa.doc2md.security import inspect_snapshot, io_path, is_within, validate_source
from docqa.domain.documents import (
    Block,
    DocumentIR,
    FigureOccurrence,
    ParseResult,
    ParseWarning,
    SourceLocator,
)
from docqa.domain.errors import DocQAError


def _json(path: Path, value) -> None:
    io_path(path).write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def _checksum(path: Path) -> str:
    checksum = sha256()
    with io_path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _binary_identity(executable: str) -> dict:
    actual = shutil.which(executable)
    if actual is None and Path(executable).is_file():
        actual = str(Path(executable).resolve())
    if actual is None:
        return {"configured": executable, "available": False}
    try:
        stat = Path(actual).stat()
        process = subprocess.run(
            [actual, "--version"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output = (process.stdout + process.stderr)[:4096].decode("utf-8", errors="replace")
        return {
            "path": actual,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "version": output.splitlines()[0] if output.splitlines() else "unknown",
        }
    except (OSError, subprocess.SubprocessError):
        return {"path": actual, "available": True, "version": "probe_failed"}


def _profile(options: ParseOptions, ocr, vision, renderer) -> dict:
    package_root = Path(__file__).resolve().parents[1]
    sources = [
        *package_root.joinpath("doc2md").rglob("*.py"),
        *package_root.joinpath("domain").glob("*.py"),
    ]
    code = {
        str(p.relative_to(package_root)).replace("\\", "/"): _checksum(p) for p in sorted(sources)
    }
    versions = {
        name: metadata.version(name)
        for name in ("pydantic", "pdfplumber", "pypdfium2", "python-docx", "python-pptx", "Pillow")
    }
    settings = options.model_dump(mode="json", exclude={"allowed_roots", "vision"})
    services = {
        "ocr": ocr.fingerprint(),
        "vision": vision.fingerprint() if vision else {"mode": "disabled"},
        "office": renderer.fingerprint(),
    }
    return {
        "schema_version": "docqa.parse-profile.v1",
        "code": digest(code),
        "versions": versions,
        "settings": settings,
        "services": services,
        "executables": {
            "ocr": _binary_identity(options.tesseract_executable),
            "office": _binary_identity(options.office_executable),
        },
    }


def _snapshot(source: Path, staging: Path, output: Path, max_bytes: int) -> tuple[Path, str]:
    before = source.stat()
    temporary = staging / ("input" + source.suffix.lower())
    copied = 0
    checksum = sha256()
    with source.open("rb") as src, temporary.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            copied += len(chunk)
            if copied > max_bytes:
                raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "Source grew beyond file limit")
            checksum.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    after = source.stat()
    value = checksum.hexdigest()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _checksum(
        source
    ) != value:
        raise DocQAError(
            "SOURCE_CHANGED_DURING_READ", "Source changed while making its snapshot", retryable=True
        )
    blob = output / "blobs" / value[:2] / (value + source.suffix.lower())
    blob.parent.mkdir(parents=True, exist_ok=True)
    if blob.exists():
        if _checksum(blob) != value:
            raise DocQAError("SNAPSHOT_CORRUPT", "Existing source snapshot has a checksum mismatch")
        temporary.unlink()
    else:
        temporary.replace(blob)
    return blob, value


def _open_catalog(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "catalog.sqlite", timeout=20)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        connection.close()
        raise DocQAError("SCHEMA_UNSUPPORTED", "Unsupported doc2md catalog schema")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            path_key TEXT PRIMARY KEY, doc_id TEXT NOT NULL,
            content_sha256 TEXT, revision_id TEXT
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            parse_key TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
            doc_id TEXT NOT NULL, revision_id TEXT NOT NULL
        );
        PRAGMA user_version=1;
    """)
    connection.commit()
    return connection


def _warn(content, code: str, message: str, severity: str = "warning") -> None:
    content.warnings.append(ParseWarning(code=code, message=message, severity=severity))


def _parse_native(context: ParseContext, fmt: str):
    if fmt == "pdf":
        from docqa.doc2md.parsers.pdf import parse_pdf

        return parse_pdf(context)
    if fmt == "docx":
        from docqa.doc2md.parsers.docx import parse_docx

        return parse_docx(context)
    from docqa.doc2md.parsers.pptx import parse_pptx

    return parse_pptx(context)


def _add_office_render(content, ctx, renderer, options, preflight):
    if options.office_render == "never":
        return
    if not content.metadata.get("needs_office_render") and options.office_render != "required":
        return
    if preflight.get("external_resources"):
        if options.office_render == "required":
            raise DocQAError(
                "OFFICE_EXTERNAL_RESOURCES", "External resources block required Office rendering"
            )
        _warn(
            content,
            "OFFICE_EXTERNAL_RESOURCES",
            "Office rendering skipped: external non-hyperlink resources exist",
        )
        return
    try:
        from docqa.doc2md.parsers.pdf import render_pdf_pages

        rendered = renderer.render(ctx.source, ctx.work_dir / "renders")
        # Stable path: converter UUIDs must not make content identities nondeterministic.
        stable = ctx.work_dir / "renders" / "document.pdf"
        if rendered != stable:
            rendered.replace(stable)
        assets = render_pdf_pages(replace(ctx, source=stable, render_pdf=True))
        content.assets.extend(assets)
        for page, asset in enumerate(assets, 1):
            location = SourceLocator(
                locator_type="derived_page",
                page=page,
                precision="page_only",
                display_label=f"{ctx.source_name} · 本次固定渲染版第 {page} 页",
                native_ref={"derived_pdf": "renders/document.pdf", "office_mapping": "page_only"},
                doc_id=ctx.doc_id,
                revision_id=ctx.revision_id,
            )
            figure_id = ctx.identity("office-render", page)
            content.figures.append(
                FigureOccurrence(
                    figure_id=figure_id,
                    asset_id=asset.asset_id,
                    locator=location,
                    full_page=True,
                    metadata={"office_render": True},
                )
            )
            content.blocks.append(
                Block(
                    block_id=ctx.identity("office-render-block", page),
                    type="figure",
                    order=len(content.blocks),
                    locators=[location],
                    figure_ids=[figure_id],
                )
            )
        content.metadata["office_render_completed"] = True
        # Keep the native coverage warnings; rendering cannot prove accurate structure recovery.
        _warn(
            content,
            "OFFICE_RENDER_AVAILABLE",
            "Fixed-page visual baseline produced; native limitations remain in the quality report",
            "info",
        )
    except DocQAError as exc:
        if options.office_render == "required":
            raise
        _warn(content, exc.code, exc.message)


def _enrich(content, ctx, options, ocr, vision, root: Path):
    assets = {asset.asset_id: asset for asset in content.assets}
    cache_dir = root / "cache" / "vision"
    cache_dir.mkdir(parents=True, exist_ok=True)
    text_context = "\n".join(block.text for block in content.blocks if block.text)[:8000]
    for figure in content.figures:
        if not figure.analysis_required:
            continue
        asset = assets[figure.asset_id]
        image = ctx.work_dir / asset.relative_path
        if options.ocr_mode != "disabled":
            try:
                figure.ocr_text = ocr.recognize(image)
            except DocQAError as exc:
                if options.ocr_mode == "required":
                    raise
                severity = "warning" if figure.metadata.get("needs_ocr") else "info"
                _warn(content, exc.code, f"{figure.locator.display_label}: {exc.message}", severity)
        elif figure.metadata.get("needs_ocr"):
            _warn(
                content,
                "OCR_DISABLED",
                f"{figure.locator.display_label}: scanned text has no OCR transcription",
            )
        if figure.metadata.get("needs_ocr") and figure.ocr_text:
            content.blocks.append(
                Block(
                    block_id=ctx.identity("ocr-block", figure.figure_id),
                    type="paragraph",
                    text=figure.ocr_text,
                    order=len(content.blocks),
                    locators=[figure.locator],
                    origin="ocr",
                    figure_ids=[figure.figure_id],
                    metadata={"source_figure": figure.figure_id},
                )
            )
        if vision is None:
            figure.semantic_state = "ocr_only" if figure.ocr_text else "disabled"
            _warn(
                content,
                "VISION_DISABLED",
                f"{figure.locator.display_label}: image semantics were not interpreted",
            )
            continue
        key = digest(
            {
                "image": asset.sha256,
                "ocr": figure.ocr_text,
                "context": text_context,
                "profile": vision.fingerprint(),
                "analysis_revision": options.analysis_revision,
            }
        )
        cached = cache_dir / (key + ".json")
        try:
            from docqa.domain.documents import VisionDescription

            if cached.exists():
                description = VisionDescription.model_validate_json(cached.read_bytes())
            else:
                description = vision.describe(image, ocr_text=figure.ocr_text, context=text_context)
                description = VisionDescription.model_validate(description)
                if (
                    description.status in {"ok", "no_semantic_content"}
                    and not description.uncertainties
                    and not any(edge.direction == "unknown" for edge in description.relations)
                ):
                    _json(cached, description.model_dump(mode="json"))
            figure.vision = description
            figure.semantic_state = "described"
            if (
                description.status in {"partial", "unreadable"}
                or description.uncertainties
                or any(edge.direction == "unknown" for edge in description.relations)
            ):
                _warn(
                    content,
                    "VISION_REVIEW_REQUIRED",
                    f"{figure.locator.display_label}: vision result contains uncertainty",
                )
        except (DocQAError, ValueError, OSError) as exc:
            figure.semantic_state = "failed"
            code = exc.code if isinstance(exc, DocQAError) else "VISION_CACHE_INVALID"
            _warn(content, code, f"{figure.locator.display_label}: image description unavailable")
    # Keep source order; OCR text is already visible with its figure and has its own IR block.
    for order, block in enumerate(content.blocks):
        block.order = order


def _quality(ir: DocumentIR) -> dict:
    state = "complete"
    if any(warning.severity in {"warning", "error"} for warning in ir.warnings):
        state = "review_required"
    if any(figure.semantic_state == "failed" for figure in ir.figures):
        state = "partial"
    if not ir.blocks:
        state = "review_required"
        if not any(item.code == "EMPTY_DOCUMENT" for item in ir.warnings):
            ir.warnings.append(
                ParseWarning(code="EMPTY_DOCUMENT", message="No extractable document blocks")
            )
    counts = {
        "blocks": len(ir.blocks),
        "figures": len(ir.figures),
        "assets": len(ir.assets),
        "pages": ir.page_count or 0,
        "warnings": len(ir.warnings),
    }
    return {
        "schema_version": "docqa.quality.v1",
        "status": state,
        "eligible_for_indexing": state == "complete",
        "counts": counts,
        "warnings": [item.model_dump() for item in ir.warnings],
    }


def _canonical_ir(ir: DocumentIR) -> dict:
    value = ir.model_dump(mode="json")
    value.pop("artifact_id", None)
    for block in value["blocks"]:
        for locator in block["locators"]:
            locator.pop("artifact_id", None)
    for figure in value["figures"]:
        figure["locator"].pop("artifact_id", None)
    return value


def verify_artifact(artifact_dir: str | Path) -> dict:
    """Validate a complete saved artifact, including source snapshot and source-map offsets."""
    directory = Path(artifact_dir).resolve()
    try:
        manifest = json.loads(io_path(directory / "manifest.json").read_bytes())
        if not isinstance(manifest, dict):
            raise ValueError("Artifact manifest must be an object")
        if manifest.get("schema_version") != "docqa.artifact.v1":
            raise ValueError("Unsupported artifact schema")
        if directory.name != manifest["artifact_id"] and directory.parent.name != "staging":
            raise ValueError("Artifact directory identity mismatch")
        required = {
            "document.ir.json",
            "document.md",
            "source_map.json",
            "quality_report.json",
            "profile.json",
        }
        if not isinstance(manifest["files"], dict):
            raise ValueError("Manifest files must be an object")
        if not required.issubset(manifest["files"]):
            raise ValueError("Manifest omits required artifact files")
        for relative, checksum in manifest["files"].items():
            path = (directory / relative).resolve()
            if (
                not is_within(path, directory)
                or not io_path(path).is_file()
                or _checksum(path) != checksum
            ):
                raise ValueError("Artifact file missing or modified")
        ir = DocumentIR.model_validate_json(io_path(directory / "document.ir.json").read_bytes())
        if ir.artifact_id != manifest["artifact_id"]:
            raise ValueError("IR identity differs from manifest")
        if digest(_canonical_ir(ir)) != ir.artifact_id:
            raise ValueError("Artifact content identity mismatch")
        for field in ("doc_id", "revision_id", "parse_profile_id", "content_sha256"):
            if getattr(ir, field) != manifest[field]:
                raise ValueError("Manifest identity differs from IR")
        if digest([ir.doc_id, ir.content_sha256]) != ir.revision_id:
            raise ValueError("Invalid revision identity")
        if digest([ir.revision_id, ir.parse_profile_id]) != manifest["parse_key"]:
            raise ValueError("Invalid parse key")
        profile = json.loads(io_path(directory / "profile.json").read_bytes())
        if digest(profile) != ir.parse_profile_id:
            raise ValueError("Profile differs from parse profile identity")
        quality = json.loads(io_path(directory / "quality_report.json").read_bytes())
        if quality != manifest["quality"] or quality != _quality(ir):
            raise ValueError("Quality report differs from IR or manifest")
        block_ids = [block.block_id for block in ir.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("Duplicate block identity")
        assets = {asset.asset_id: asset for asset in ir.assets}
        figures = {figure.figure_id: figure for figure in ir.figures}
        if len(assets) != len(ir.assets) or len(figures) != len(ir.figures):
            raise ValueError("Duplicate asset or figure identity")
        for asset in ir.assets:
            path = (directory / asset.relative_path).resolve()
            if not is_within(path, directory) or _checksum(path) != asset.sha256:
                raise ValueError("Image source cannot be resolved")
            if asset.relative_path not in manifest["files"] or asset.sha256 != asset.asset_id:
                raise ValueError("Asset is not bound to manifest or content identity")
            if asset.derived_from and asset.derived_from not in assets:
                raise ValueError("Missing original image")
        for figure in ir.figures:
            if figure.asset_id not in assets:
                raise ValueError("Missing figure image")
            if figure.original_asset_id and figure.original_asset_id not in assets:
                raise ValueError("Missing original figure image")
            if (figure.locator.doc_id, figure.locator.revision_id, figure.locator.artifact_id) != (
                ir.doc_id,
                ir.revision_id,
                ir.artifact_id,
            ):
                raise ValueError("Figure locator version mismatch")
        for block in ir.blocks:
            if any(value not in figures for value in block.figure_ids):
                raise ValueError("Missing figure occurrence")
            for locator in block.locators:
                if (locator.doc_id, locator.revision_id, locator.artifact_id) != (
                    ir.doc_id,
                    ir.revision_id,
                    ir.artifact_id,
                ):
                    raise ValueError("Locator version mismatch")
        markdown = io_path(directory / "document.md").read_bytes()
        source_map = json.loads(io_path(directory / "source_map.json").read_bytes())
        if b"\r" in markdown or sha256(markdown).hexdigest() != source_map["markdown_sha256"]:
            raise ValueError("Markdown digest or newline mismatch")
        if [entry["block_id"] for entry in source_map["entries"]] != block_ids:
            raise ValueError("Source-map block coverage differs")
        end = 0
        for entry in source_map["entries"]:
            start, next_end = entry["start_byte"], entry["end_byte"]
            if not end <= start < next_end <= len(markdown):
                raise ValueError("Source-map interval out of bounds")
            payload = markdown[start:next_end].decode("utf-8")
            if f'id="{entry["anchor"]}"' not in payload:
                raise ValueError("Source-map anchor does not match interval")
            end = next_end
        root = directory.parent.parent
        blob = (root / manifest["snapshot_ref"]).resolve()
        if not is_within(blob, root / "blobs") or _checksum(blob) != manifest["content_sha256"]:
            raise ValueError("Source snapshot cannot be resolved")
        expected_markdown, expected_map = render_markdown(
            ir,
            snapshot_link=os.path.relpath(blob, directory),
            status=quality["status"],
        )
        if markdown != expected_markdown or source_map != expected_map:
            raise ValueError("Markdown or source-map projection differs from its IR")
        return {
            "valid": True,
            "artifact_id": ir.artifact_id,
            "status": manifest["quality"]["status"],
            "files_checked": len(manifest["files"]),
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DocQAError(
            "ARTIFACT_CORRUPT", f"Artifact validation failed ({type(exc).__name__})"
        ) from exc


def _result(manifest: dict, directory: Path, *, reused: bool = False) -> ParseResult:
    quality = manifest["quality"]
    return ParseResult(
        status=quality["status"],
        doc_id=manifest["doc_id"],
        revision_id=manifest["revision_id"],
        artifact_id=manifest["artifact_id"],
        artifact_dir=str(directory),
        markdown_path=str(directory / "document.md"),
        eligible_for_indexing=quality["eligible_for_indexing"],
        reused=reused,
        warnings=quality["warnings"],
        counts=quality["counts"],
    )


def parse_document(
    source: str | Path,
    output: str | Path = "runtime/doc2md",
    *,
    options: ParseOptions | None = None,
    doc_id: str | None = None,
    ocr_client=None,
    vision_client=None,
    office_renderer=None,
) -> ParseResult:
    try:
        return _parse_document(
            source,
            output,
            options=options,
            doc_id=doc_id,
            ocr_client=ocr_client,
            vision_client=vision_client,
            office_renderer=office_renderer,
        )
    except OSError as exc:
        raise DocQAError(
            "STORAGE_IO_FAILED", "Cannot read/write doc2md assets; check permissions and disk space"
        ) from exc
    except sqlite3.Error as exc:
        raise DocQAError("CATALOG_FAILED", "Cannot read/write the doc2md catalog") from exc


def _parse_document(
    source: str | Path,
    output: str | Path = "runtime/doc2md",
    *,
    options: ParseOptions | None = None,
    doc_id: str | None = None,
    ocr_client=None,
    vision_client=None,
    office_renderer=None,
) -> ParseResult:
    """Parse one explicitly selected file without rewriting an existing artifact."""
    options = options or ParseOptions()
    source, fmt, legacy = validate_source(Path(source), options)
    root = Path(output).resolve()
    if source == root or is_within(source, root):
        raise DocQAError("INVALID_OUTPUT", "Input cannot be inside the output data root")
    root.mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    try:
        ocr = ocr_client or TesseractOCR(
            options.tesseract_executable,
            options.ocr_languages,
            options.external_timeout,
            tessdata_dir=options.tessdata_dir,
        )
        renderer = office_renderer or LibreOfficeRenderer(
            options.office_executable, options.external_timeout
        )
    except ValueError as exc:
        raise DocQAError(
            "INVALID_CONFIG", "Invalid OCR/Office executable, language or timeout configuration"
        ) from exc
    vision = vision_client
    if vision is None and options.vision.mode == "http":
        v = options.vision
        try:
            vision = HttpVisionClient(
                v.endpoint,
                v.model,
                v.revision,
                v.api_key_env,
                v.timeout,
                v.retries,
                thinking=v.thinking,
                enable_thinking=v.enable_thinking,
            )
        except ValueError as exc:
            raise DocQAError(
                "INVALID_CONFIG", "VL endpoint, model and revision must be configured explicitly"
            ) from exc
    profile = _profile(options, ocr, vision, renderer)
    profile_id = digest(profile)
    try:
        with portalocker.Lock(root / ".doc2md.lock", timeout=20):
            connection = _open_catalog(root)
            try:
                return _parse_locked(
                    source,
                    root,
                    fmt,
                    legacy,
                    options,
                    doc_id,
                    ocr,
                    vision,
                    renderer,
                    profile,
                    profile_id,
                    connection,
                )
            finally:
                connection.close()
    except portalocker.exceptions.LockException as exc:
        raise DocQAError(
            "LOCK_TIMEOUT", "Another doc2md task holds this output root", retryable=True
        ) from exc
    except OSError as exc:
        raise DocQAError(
            "STORAGE_IO_FAILED", "Cannot read/write doc2md assets; check permissions and disk space"
        ) from exc
    except sqlite3.Error as exc:
        raise DocQAError("CATALOG_FAILED", "Cannot read/write the doc2md catalog") from exc


def _parse_locked(
    source,
    root,
    fmt,
    legacy,
    options,
    requested_id,
    ocr,
    vision,
    renderer,
    profile,
    profile_id,
    connection,
):
    path_key = os.path.normcase(str(source))
    row = connection.execute(
        "SELECT doc_id FROM documents WHERE path_key=?", (path_key,)
    ).fetchone()
    if requested_id is not None:
        try:
            requested_id = str(UUID(requested_id))
        except ValueError as exc:
            raise DocQAError("INVALID_REQUEST", "doc_id must be a UUID") from exc
        if row and row[0] != requested_id:
            raise DocQAError(
                "DOC_ID_CONFLICT", "Source already has a different registered document ID"
            )
    document_id = row[0] if row else (requested_id or str(uuid4()))
    with tempfile.TemporaryDirectory(prefix="parse-", dir=root / "staging") as temp:
        staging = Path(temp)
        blob, checksum = _snapshot(source, staging, root, options.max_file_mb * 1024 * 1024)
        revision_id = digest([document_id, checksum])
        parse_key = digest([revision_id, profile_id])
        preflight = inspect_snapshot(blob, fmt, options)
        with connection:
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(path_key) DO UPDATE SET content_sha256=excluded.content_sha256,revision_id=excluded.revision_id",
                (path_key, document_id, checksum, revision_id),
            )
        cached = connection.execute(
            "SELECT artifact_id FROM artifacts WHERE parse_key=?", (parse_key,)
        ).fetchone()
        if cached:
            directory = root / "artifacts" / cached[0]
            verify_artifact(directory)
            previous = json.loads(io_path(directory / "manifest.json").read_bytes())
            # An unavailable service/key must not permanently cache a partial result.
            if previous["quality"]["status"] == "complete":
                return _result(previous, directory, reused=True)
        ctx = ParseContext(
            blob,
            staging,
            document_id,
            revision_id,
            source.name,
            options.max_pages,
            options.render_dpi,
            options.max_pixels,
            options.render_pdf,
        )
        if legacy:
            if preflight.get("external_resources"):
                raise DocQAError("OFFICE_EXTERNAL_RESOURCES", "External resources block conversion")
            converted = renderer.render(blob, staging / "renders")
            stable = staging / "renders" / "document.pdf"
            converted.replace(stable)
            content = _parse_native(replace(ctx, source=stable), "pdf")
            for block in content.blocks:
                for location in block.locators:
                    location.locator_type = "derived_page"
                    location.display_label = f"{source.name} · 转换版第 {location.page} 页"
                    location.native_ref["derived_pdf"] = "renders/document.pdf"
            for figure in content.figures:
                figure.locator.locator_type = "derived_page"
                figure.locator.display_label = f"{source.name} · 转换版第 {figure.locator.page} 页"
        else:
            content = _parse_native(ctx, fmt)
            if fmt in {"docx", "pptx"}:
                _add_office_render(content, ctx, renderer, options, preflight)
        _enrich(content, ctx, options, ocr, vision, root)
        from docqa.doc2md.figure_names import name_figures

        name_figures(content, ctx)
        from docqa.doc2md.classification import classify

        category = classify(content, source.name, options.category)
        assets = {asset.asset_id: asset for asset in [*ctx.raw_assets.values(), *content.assets]}
        ir = DocumentIR(
            **content.model_dump(exclude={"assets"}),
            assets=list(assets.values()),
            doc_id=document_id,
            revision_id=revision_id,
            content_sha256=checksum,
            parse_profile_id=profile_id,
            source_name=source.name,
            source_format=fmt,
            category=category,
        )
        for block in ir.blocks:
            for location in block.locators:
                location.doc_id, location.revision_id = document_id, revision_id
        for figure in ir.figures:
            figure.locator.doc_id, figure.locator.revision_id = document_id, revision_id
        quality = _quality(ir)
        artifact_id = digest(_canonical_ir(ir))
        ir.artifact_id = artifact_id
        for block in ir.blocks:
            for locator in block.locators:
                locator.doc_id, locator.revision_id, locator.artifact_id = (
                    document_id,
                    revision_id,
                    artifact_id,
                )
        for figure in ir.figures:
            figure.locator.doc_id, figure.locator.revision_id, figure.locator.artifact_id = (
                document_id,
                revision_id,
                artifact_id,
            )
        directory = root / "artifacts" / artifact_id
        md, source_map = render_markdown(
            ir,
            snapshot_link=os.path.relpath(blob, directory),
            status=quality["status"],
        )
        (staging / "document.md").write_bytes(md)
        _json(staging / "document.ir.json", ir.model_dump(mode="json"))
        _json(staging / "source_map.json", source_map)
        _json(staging / "quality_report.json", quality)
        _json(staging / "profile.json", profile)
        files = {
            path.relative_to(staging).as_posix(): _checksum(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema_version": "docqa.artifact.v1",
            "artifact_id": artifact_id,
            "doc_id": document_id,
            "revision_id": revision_id,
            "parse_key": parse_key,
            "parse_profile_id": profile_id,
            "content_sha256": checksum,
            "snapshot_ref": blob.relative_to(root).as_posix(),
            "quality": quality,
            "files": files,
        }
        _json(staging / "manifest.json", manifest)
        verify_artifact(staging)
        for path in staging.rglob("*"):
            if path.is_file():
                with path.open("r+b") as handle:
                    os.fsync(handle.fileno())
        if directory.exists():
            verify_artifact(directory)
        else:
            # Filesystem package first; catalog pointer last. Crash leaves an unreferenced package.
            staging.replace(directory)
        with connection:
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?) ON CONFLICT(parse_key) DO UPDATE SET artifact_id=excluded.artifact_id",
                (parse_key, artifact_id, document_id, revision_id),
            )
        return _result(manifest, directory)


def batch_parse(
    directory: str | Path,
    output: str | Path = "runtime/doc2md",
    *,
    recursive: bool = True,
    options: ParseOptions | None = None,
    types: tuple[str, ...] = ("pdf", "docx", "pptx"),
) -> dict:
    """Continue after individual failures; batch result is explicit about omissions."""
    source = Path(directory).resolve()
    root = Path(output).resolve()
    if not source.is_dir():
        raise DocQAError("INVALID_REQUEST", "Batch input must be a directory")
    if is_within(root, source) or is_within(source, root):
        raise DocQAError(
            "INVALID_OUTPUT", "Batch source and output directories must not contain each other"
        )
    if not set(types).issubset({"pdf", "docx", "pptx", "doc", "ppt"}):
        raise DocQAError("INVALID_REQUEST", "Unknown batch file type")
    selected = options or ParseOptions(allowed_roots=[source])
    files = sorted(source.rglob("*") if recursive else source.iterdir())
    results, failures = [], []
    for path in files:
        if (
            not path.is_file()
            or path.name.startswith("~$")
            or path.suffix.lower().lstrip(".") not in types
        ):
            continue
        try:
            results.append(parse_document(path, root, options=selected).model_dump(mode="json"))
        except DocQAError as exc:
            failures.append({"source": str(path), "error": exc.as_dict()})
    complete = sum(result["status"] == "complete" for result in results)
    report = {
        "schema_version": "docqa.batch.v1",
        "attempted": len(results) + len(failures),
        "complete": complete,
        "needs_review": len(results) - complete,
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    root.mkdir(parents=True, exist_ok=True)
    _json(root / f"batch-report-{uuid4().hex}.json", report)
    return report
