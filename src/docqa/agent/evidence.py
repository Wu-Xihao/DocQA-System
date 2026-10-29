"""Evidence is a verified source component, never a generated retrieval wrapper.

All file references below are derived from an immutable DocBase generation. The
caller may provide a hit ID, but none of its text, paths or locators is trusted.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from docqa.docbase.admission import COVERAGE_WARNING, has_coverage_gaps
from docqa.docbase.chunker import resolve_span_text
from docqa.docbase.retrieval import generation_directory
from docqa.docbase.storage import _io_path, _safe_path, load_generation
from docqa.domain.answers import EvidenceItem, EvidencePack
from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError


def json_bytes(value) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")

    def encode_model(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        raise TypeError("Unsupported evidence JSON value")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=encode_model,
    ).encode("utf-8")


def _source(generation, chunk):
    member = next(
        (item for item in generation.members if item["artifact_id"] == chunk.artifact_id),
        None,
    )
    if member is None:
        raise DocQAError("EVIDENCE_SOURCE_INVALID", "Chunk has no generation member")
    artifact = _safe_path(generation.directory, member["artifact_ref"])
    ir = DocumentIR.model_validate_json(_io_path(artifact / "document.ir.json").read_bytes())
    return artifact, ir


def _reference(generation, chunk, span, artifact, ir) -> dict:
    manifest = json.loads(_io_path(artifact / "manifest.json").read_bytes())
    snapshot = _safe_path(artifact.parent.parent, manifest["snapshot_ref"])
    markdown = _io_path(artifact / "document.md").read_bytes()
    raw = markdown[span.markdown_start_byte : span.markdown_end_byte]
    # SourceSpan v1 identifies the owning Markdown block. Its exact component
    # codepoint interval is a separate coordinate, not a fake exact MD subrange.
    raw.decode("utf-8")
    images = []
    if span.figure_id:
        figure = next(item for item in ir.figures if item.figure_id == span.figure_id)
        asset_ids = {figure.asset_id, figure.original_asset_id}
        for asset in ir.assets:
            if asset.asset_id in asset_ids:
                images.append(
                    {
                        "asset_id": asset.asset_id,
                        "path": str(_safe_path(artifact, asset.relative_path)),
                        "sha256": asset.sha256,
                        "kind": "original"
                        if asset.asset_id == figure.original_asset_id
                        else "visible",
                    }
                )
    return {
        "schema_version": "docqa.evidence-source.v1",
        "generation_id": generation.manifest["generation_id"],
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "revision_id": chunk.revision_id,
        "artifact_id": chunk.artifact_id,
        "source_name": chunk.source_name,
        "artifact_dir": str(artifact),
        "markdown_path": str(artifact / "document.md"),
        "snapshot_path": str(snapshot),
        "markdown_start_byte": span.markdown_start_byte,
        "markdown_end_byte": span.markdown_end_byte,
        "markdown_start_line": markdown[: span.markdown_start_byte].count(b"\n") + 1,
        "markdown_end_line": markdown[: span.markdown_end_byte].count(b"\n") + 1,
        "markdown_range_precision": "whole_owning_block",
        "component": span.component,
        "text_start_codepoint": span.text_start_codepoint,
        "text_end_codepoint": span.text_end_codepoint,
        "origin": span.origin,
        "locators": [item.model_dump(mode="json") for item in span.locators],
        "images": images,
    }


def source_reference(index: str | Path, generation_id: str, chunk_id: str, span) -> dict:
    """Revalidate the entire package before resolving a previously delivered span."""
    from docqa.domain.rag import SourceSpan

    span = SourceSpan.model_validate(span)
    with load_generation(generation_directory(index, generation_id)) as generation:
        if generation.manifest["generation_id"] != generation_id:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence generation differs")
        chunk = next((item for item in generation.chunks if item.chunk_id == chunk_id), None)
        if chunk is None or span not in chunk.source_spans:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence span does not belong to Chunk")
        artifact, ir = _source(generation, chunk)
        text = resolve_span_text(ir, span)
        if sha256(text.encode("utf-8")).hexdigest() != span.source_text_sha256:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence text checksum differs")
        return {**_reference(generation, chunk, span, artifact, ir), "text": text}


def build_evidence_items(index: str | Path, generation_id: str, hits) -> list[EvidenceItem]:
    """Read each real source span independently; ignore all hit-provided prose."""
    values = []
    seen = set()
    with load_generation(generation_directory(index, generation_id)) as generation:
        if generation.manifest["generation_id"] != generation_id:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Requested generation differs")
        chunks = {item.chunk_id: item for item in generation.chunks}
        artifacts = {}
        for hit in hits:
            if hit.generation_id != generation_id or hit.chunk_id not in chunks:
                raise DocQAError("EVIDENCE_SOURCE_INVALID", "Hit is outside the fixed generation")
            chunk = chunks[hit.chunk_id]
            if chunk.artifact_id not in artifacts:
                artifacts[chunk.artifact_id] = _source(generation, chunk)
            _, ir = artifacts[chunk.artifact_id]
            partial_source = any(
                member["artifact_id"] == chunk.artifact_id and has_coverage_gaps(member)
                for member in generation.members
            )
            blocks = {block.block_id: block for block in ir.blocks}
            figures = {figure.figure_id: figure for figure in ir.figures}
            for span in chunk.source_spans:
                key = (chunk.chunk_id, json_bytes(span))
                if key in seen:
                    continue
                seen.add(key)
                text = resolve_span_text(ir, span)
                text_digest = sha256(text.encode("utf-8")).hexdigest()
                if text_digest != span.source_text_sha256:
                    raise DocQAError("EVIDENCE_SOURCE_INVALID", "Source span digest differs")
                figure = figures.get(span.figure_id)
                values.append(
                    EvidenceItem(
                        generation_id=generation_id,
                        chunk_id=chunk.chunk_id,
                        doc_id=chunk.doc_id,
                        revision_id=chunk.revision_id,
                        artifact_id=chunk.artifact_id,
                        source_name=chunk.source_name,
                        category=chunk.category,
                        source_span=span,
                        text=text,
                        text_sha256=text_digest,
                        origin=span.origin,
                        figure_ids=[span.figure_id] if span.figure_id else [],
                        uncertainties=(
                            list(figure.vision.uncertainties) if figure and figure.vision else []
                        ),
                        coverage_warnings=[COVERAGE_WARNING] if partial_source else [],
                        is_heading=blocks[span.block_id].type == "heading",
                    )
                )
    return values


def _sized_pack(**values) -> EvidencePack:
    pack = EvidencePack(payload_utf8_bytes=0, **values)
    while True:
        size = len(json_bytes(pack))
        if pack.payload_utf8_bytes == size:
            return pack
        pack = pack.model_copy(update={"payload_utf8_bytes": size})


def pack_candidates(
    items: list[EvidenceItem],
    *,
    session_id: str,
    generation_id: str,
    query_id: str,
    round_no: int,
    budget_utf8_bytes: int,
    next_evidence_no: int = 1,
    degraded: bool = False,
    warnings: list[str] | None = None,
) -> EvidencePack:
    """Bound the complete canonical JSON payload; never silently cut a source span."""
    common = dict(
        session_id=session_id,
        generation_id=generation_id,
        query_id=query_id,
        round_no=round_no,
        budget_utf8_bytes=budget_utf8_bytes,
        degraded=degraded,
        warnings=list(warnings or []),
    )
    selected = []
    omitted = 0
    for candidate in items:
        if candidate.generation_id != generation_id:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence crosses generations")
        evidence_id = f"E{next_evidence_no + len(selected):06d}"
        assigned = candidate.model_copy(
            update={
                "evidence_id": evidence_id,
                "source_ref": f"docqa://sessions/{session_id}/evidence/{evidence_id}",
            }
        )
        trial = _sized_pack(
            **common, items=[*selected, assigned], complete=False, omitted_count=len(items)
        )
        if trial.payload_utf8_bytes <= budget_utf8_bytes:
            selected.append(assigned)
        else:
            omitted += 1
    pack = _sized_pack(**common, items=selected, complete=omitted == 0, omitted_count=omitted)
    if pack.payload_utf8_bytes > budget_utf8_bytes:
        raise DocQAError("EVIDENCE_BUDGET_TOO_SMALL", "Evidence metadata exceeds byte budget")
    return pack


def revalidate_evidence(index: str | Path, item: EvidenceItem) -> dict:
    """Bind a delivered item to fresh source validation, not only its stored digest."""
    return revalidate_evidence_batch(index, [item])[0]


def revalidate_evidence_batch(index: str | Path, items) -> list[dict]:
    """Check package change identity and each source span on every batch.

    An unchanged generation can reuse its process-local validation. Changes
    invalidate the whole generation; individual evidence checks always run.
    """
    items = list(items)
    if not items:
        return []
    generation_ids = {item.generation_id for item in items}
    if len(generation_ids) != 1:
        raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence batch mixes generations")
    generation_id = next(iter(generation_ids))
    references = []
    with load_generation(generation_directory(index, generation_id)) as generation:
        if generation.manifest["generation_id"] != generation_id:
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence generation differs")
        chunks = {chunk.chunk_id: chunk for chunk in generation.chunks}
        sources = {}
        for item in items:
            chunk = chunks.get(item.chunk_id)
            if chunk is None or item.source_span not in chunk.source_spans:
                raise DocQAError(
                    "EVIDENCE_SOURCE_INVALID", "Evidence span does not belong to Chunk"
                )
            if chunk.artifact_id not in sources:
                sources[chunk.artifact_id] = _source(generation, chunk)
            artifact, ir = sources[chunk.artifact_id]
            text = resolve_span_text(ir, item.source_span)
            if sha256(text.encode("utf-8")).hexdigest() != item.source_span.source_text_sha256:
                raise DocQAError("EVIDENCE_SOURCE_INVALID", "Evidence source span checksum differs")
            reference = {
                **_reference(generation, chunk, item.source_span, artifact, ir),
                "text": text,
            }
            _validate_reference(item, reference)
            references.append(reference)
    return references


def _validate_reference(item: EvidenceItem, reference: dict) -> None:
    for field in (
        "generation_id",
        "chunk_id",
        "doc_id",
        "revision_id",
        "artifact_id",
        "source_name",
        "origin",
    ):
        if reference[field] != getattr(item, field):
            raise DocQAError("EVIDENCE_SOURCE_INVALID", "Delivered evidence metadata differs")
    if (
        reference["text"] != item.text
        or sha256(item.text.encode("utf-8")).hexdigest() != item.text_sha256
    ):
        raise DocQAError("EVIDENCE_SOURCE_INVALID", "Delivered evidence text differs")
