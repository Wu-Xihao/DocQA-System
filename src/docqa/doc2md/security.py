"""Bounded file validation before native parsers or external converters run."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from docqa.doc2md.config import ParseOptions
from docqa.domain.errors import DocQAError

SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx"}
LEGACY = {".doc": "doc", ".ppt": "ppt"}


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def io_path(path: Path) -> Path:
    """Use extended Windows paths for IO, never for persisted logical identities.

    This only adapts paths; callers still perform their normal containment,
    type and integrity checks on canonical paths before opening files.
    """
    path = Path(path).absolute()
    value = str(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def validate_source(source: Path, options: ParseOptions) -> tuple[Path, str, bool]:
    lexical = source.absolute()
    try:
        resolved = source.resolve(strict=True)
        if not resolved.is_file():
            raise DocQAError("INVALID_REQUEST", "Input must be a regular file")
        roots = options.allowed_roots or [lexical.parent.resolve()]
        if not any(is_within(resolved, root.resolve()) for root in roots):
            raise DocQAError("SOURCE_ACCESS_DENIED", "Input is outside allowed source roots")
        if resolved.stat().st_size > options.max_file_mb * 1024 * 1024:
            raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "Input exceeds file size limit")
        suffix = resolved.suffix.lower()
        if suffix in LEGACY:
            if not options.allow_legacy_office:
                raise DocQAError("UNSUPPORTED_FORMAT", "DOC/PPT needs explicit legacy conversion")
            return resolved, LEGACY[suffix], True
        if suffix not in SUPPORTED:
            raise DocQAError("UNSUPPORTED_FORMAT", "Supported native formats: PDF, DOCX, PPTX")
        return resolved, SUPPORTED[suffix], False
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise DocQAError("SOURCE_ACCESS_DENIED", "Input cannot be read") from exc


def inspect_snapshot(path: Path, fmt: str, options: ParseOptions) -> dict:
    with path.open("rb") as handle:
        signature = handle.read(1024)
    if fmt == "pdf":
        if b"%PDF-" not in signature:
            raise DocQAError("FORMAT_MISMATCH", "PDF extension does not match its signature")
        return {}
    if fmt in LEGACY.values():
        if not signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            raise DocQAError("FORMAT_MISMATCH", "Legacy Office signature is invalid")
        return {"legacy": True}
    if signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise DocQAError(
            "PROTECTED_OR_BINARY_OFFICE", "Office input is protected or not plain OOXML"
        )
    if not signature.startswith(b"PK"):
        raise DocQAError("FORMAT_MISMATCH", "Office extension does not match a ZIP container")
    expected = "word/document.xml" if fmt == "docx" else "ppt/presentation.xml"
    external_resources = False
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > options.max_archive_members:
                raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "Too many archive members")
            if sum(info.file_size for info in infos) > options.max_uncompressed_mb * 1024 * 1024:
                raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "Archive expanded size exceeds limit")
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                raise DocQAError("PARSE_CORRUPT", "Duplicate archive member names")
            if expected not in names or "[Content_Types].xml" not in names:
                raise DocQAError("FORMAT_MISMATCH", "Office package lacks required document parts")
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or ":" in name:
                    raise DocQAError("UNSAFE_ARCHIVE", "Unsafe archive member path")
                if info.flag_bits & 0x1:
                    raise DocQAError(
                        "PASSWORD_REQUIRED", "Encrypted archive members are unsupported"
                    )
                if "vbaproject" in name.lower():
                    raise DocQAError("ACTIVE_CONTENT_REJECTED", "Macro content is not accepted")
                if info.file_size > max(1, info.compress_size) * 5000:
                    raise DocQAError(
                        "RESOURCE_LIMIT_EXCEEDED", "Archive compression ratio exceeds limit"
                    )
                if name.endswith((".xml", ".rels")):
                    if info.file_size > 32 * 1024 * 1024:
                        raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "XML part exceeds limit")
                    xml = archive.read(info)
                    # ASCII XML keywords may be NUL-interleaved in UTF-16/UTF-32.
                    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", xml.replace(b"\x00", b""), re.I):
                        raise DocQAError(
                            "UNSAFE_XML", "DTD and entity declarations are not accepted"
                        )
                    if name.endswith(".rels"):
                        from xml.etree import ElementTree

                        try:
                            relationships = ElementTree.fromstring(xml)
                        except ElementTree.ParseError as exc:
                            raise DocQAError("PARSE_CORRUPT", "Invalid relationships XML") from exc
                        for relationship in relationships:
                            if relationship.get(
                                "TargetMode"
                            ) == "External" and not relationship.get("Type", "").endswith(
                                "/hyperlink"
                            ):
                                external_resources = True
    except (BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise DocQAError("PARSE_CORRUPT", "Cannot read Office package") from exc
    return {"external_resources": external_resources}
