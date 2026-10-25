"""Source-bound UTF-8 byte-budgeted chunks from verified doc2md artifacts.

The Markdown interval is deliberately the complete block's interval. Component
codepoint intervals are exact; they are not falsely presented as exact Markdown
subranges after escaping, table rendering or image-description insertion.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from docqa.doc2md.common import digest
from docqa.domain.documents import Block, DocumentIR, FigureOccurrence
from docqa.domain.errors import DocQAError
from docqa.domain.rag import Chunk, ChunkOptions, GeneratedContext, SourceSpan, chunk_identity

_IDENTIFIER = re.compile(
    r"(?:https?://[^\s<>\"']+|[A-Za-z]:[\\/][^\s<>\"']+|"
    r"--?[A-Za-z][A-Za-z0-9_-]*(?:=[A-Za-z0-9_.:/\\-]+)?|"
    r"[A-Za-z0-9_]+(?:[.:/\\@-][A-Za-z0-9_]+)*)"
)
_LABELS = {"native": "[原生]", "ocr": "[OCR 派生]", "vl_generated": "[VL 派生]"}


def chunk_profile(options: ChunkOptions) -> dict:
    return {
        "schema_version": "docqa.chunk-profile.v1",
        "strategy": "block-component-byte-windows-v1",
        "budget_unit": "UTF-8 bytes, including labels and generated context",
        "markdown_range": "whole owning block; component codepoint range is exact",
        "table_row_projection": "join cells with ' | '; copied first row is not a verified header",
        "figure_projection": "readable-v1; observations/graph/interpretations/uncertainties",
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "options": options.model_dump(mode="json"),
    }


def figure_vl_text(figure: FigureOccurrence) -> str:
    """Canonical readable VL component; geometry remains available in the IR.

    A no-semantic-content result alone is not searchable knowledge. Status and
    image type only accompany actual observations, graph or uncertainty content.
    """
    vision = figure.vision
    if vision is None or not (
        vision.observations
        or vision.entities
        or vision.relations
        or vision.interpretations
        or vision.uncertainties
    ):
        return ""
    lines = [f"图片类型：{vision.image_type}", f"释义状态：{vision.status}"]
    lines.extend(f"观察：{item}" for item in vision.observations)
    lines.extend(f"节点 {item.id}：{item.label}" for item in vision.entities)
    labels = {item.id: item.label for item in vision.entities}
    for edge in vision.relations:
        symbol = {"directed": "→", "undirected": "—（无向）", "unknown": "方向未知"}[edge.direction]
        lines.append(f"关系：{labels[edge.source]} {symbol} {labels[edge.target]}；{edge.label}")
    lines.extend(f"解释（非逐字原文）：{item}" for item in vision.interpretations)
    lines.extend(f"不确定项：{item}" for item in vision.uncertainties)
    return "\n".join(lines)


def _component(ir: DocumentIR, span: SourceSpan) -> str:
    block = next((item for item in ir.blocks if item.block_id == span.block_id), None)
    if block is None:
        raise DocQAError("CHUNK_INVALID_SPAN", "Source span references an unknown block")
    if span.component == "block_text":
        if (
            span.figure_id is not None
            or span.table_row_start is not None
            or span.table_row_end is not None
        ):
            raise DocQAError(
                "CHUNK_INVALID_SPAN", "Block-text span has unrelated component coordinates"
            )
        return block.text
    if span.component == "table_row":
        row = span.table_row_start
        if (
            block.type != "table"
            or row is None
            or span.table_row_end != row + 1
            or not 0 <= row < len(block.table_rows)
            or span.figure_id is not None
        ):
            raise DocQAError(
                "CHUNK_INVALID_SPAN", "Table span must reference one actual original row"
            )
        return " | ".join(block.table_rows[row])
    if span.table_row_start is not None or span.table_row_end is not None:
        raise DocQAError("CHUNK_INVALID_SPAN", "Figure span has table coordinates")
    figure = next((item for item in ir.figures if item.figure_id == span.figure_id), None)
    if figure is None or figure.figure_id not in block.figure_ids:
        raise DocQAError("CHUNK_INVALID_SPAN", "Figure span references another block's image")
    return figure.ocr_text if span.component == "figure_ocr" else figure_vl_text(figure)


def resolve_span_text(ir: DocumentIR, span: SourceSpan) -> str:
    component = _component(ir, span)
    if not 0 <= span.text_start_codepoint < span.text_end_codepoint <= len(component):
        raise DocQAError("CHUNK_INVALID_SPAN", "Source component interval is out of bounds")
    return component[span.text_start_codepoint : span.text_end_codepoint]


def _ranges(text: str, capacity: int, overlap: int) -> list[tuple[int, int]]:
    if capacity < 1:
        raise DocQAError(
            "CHUNK_CONTEXT_TOO_LONG", "Generated context leaves no room for source text"
        )
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + len(character.encode("utf-8")))
    protected = [(match.start(), match.end()) for match in _IDENTIFIER.finditer(text)]
    for start, end in protected:
        if offsets[end] - offsets[start] > capacity:
            raise DocQAError(
                "CHUNK_IDENTIFIER_TOO_LONG",
                "A complete identifier exceeds the available chunk byte budget",
            )
    protected_starts = [item[0] for item in protected]

    def protect(cut: int) -> int:
        position = bisect.bisect_left(protected_starts, cut) - 1
        if position >= 0 and protected[position][0] < cut < protected[position][1]:
            return protected[position][0]
        return cut

    result = []
    start = 0
    while start < len(text):
        end = bisect.bisect_right(offsets, offsets[start] + capacity) - 1
        end = protect(end)
        if end <= start:
            raise DocQAError(
                "CHUNK_CONTEXT_TOO_LONG", "One source character cannot fit the byte budget"
            )
        if end < len(text):
            floor = bisect.bisect_left(offsets, offsets[start] + capacity // 2)
            candidates = [
                start + match.end()
                for match in re.finditer(r"[\n。！？；.!?;]\s*|\s+", text[start:end])
                if start + match.end() >= floor
                and protect(start + match.end()) == start + match.end()
            ]
            if candidates:
                end = candidates[-1]
        result.append((start, end))
        if end == len(text):
            break
        next_start = bisect.bisect_left(offsets, max(offsets[start], offsets[end] - overlap))
        next_start = protect(next_start)
        start = next_start if next_start > start else end
    return result


@dataclass
class _Unit:
    text: str
    span: SourceSpan
    figure_ids: list[str]


def _context_text(contexts: list[GeneratedContext]) -> str:
    labels = {
        "section": "章节上下文（复制）",
        "table_header": "续表首行（复制，未确认表头）",
        "figure_caption": "图注上下文（非独立证据）",
    }
    return "\n\n".join(f"[{labels[item.kind]}]\n{item.text}" for item in contexts)


def _body(units: list[_Unit]) -> str:
    return "\n\n".join(f"{_LABELS[item.span.origin]}\n{item.text}" for item in units)


def chunk_artifact(ir: DocumentIR, source_map: dict, options: ChunkOptions) -> list[Chunk]:
    if not ir.artifact_id:
        raise DocQAError("CHUNK_INVALID_ARTIFACT", "Chunks require an immutable artifact identity")
    if source_map.get("artifact_id") != ir.artifact_id:
        raise DocQAError("CHUNK_INVALID_SOURCE_MAP", "Source map and artifact identity differ")
    entries = source_map.get("entries")
    if not isinstance(entries, list):
        raise DocQAError("CHUNK_INVALID_SOURCE_MAP", "Source map entries must be a list")
    mapped = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("block_id"), str)
            or entry["block_id"] in mapped
        ):
            raise DocQAError(
                "CHUNK_INVALID_SOURCE_MAP", "Source map contains malformed or duplicate entries"
            )
        if (
            type(entry.get("start_byte")) is not int
            or type(entry.get("end_byte")) is not int
            or not 0 <= entry["start_byte"] < entry["end_byte"]
        ):
            raise DocQAError(
                "CHUNK_INVALID_SOURCE_MAP", "Source map has invalid UTF-8 byte intervals"
            )
        mapped[entry["block_id"]] = entry
    if len({block.block_id for block in ir.blocks}) != len(ir.blocks):
        raise DocQAError("CHUNK_INVALID_ARTIFACT", "IR has duplicate block identities")
    if any(block.block_id not in mapped for block in ir.blocks):
        raise DocQAError("CHUNK_INVALID_SOURCE_MAP", "A block is missing from the source map")
    figures = {figure.figure_id: figure for figure in ir.figures}
    if len(figures) != len(ir.figures):
        raise DocQAError("CHUNK_INVALID_ARTIFACT", "IR has duplicate figure identities")
    profile_id = digest(chunk_profile(options))
    chunks: list[Chunk] = []
    headings: list[tuple[int, str, str]] = []
    pending: list[_Unit] = []

    def section_context() -> list[GeneratedContext]:
        if not headings:
            return []
        return [
            GeneratedContext(
                kind="section",
                text=" > ".join(item[1] for item in headings),
                source_block_ids=[item[2] for item in headings],
            )
        ]

    def emit(units: list[_Unit], extra: list[GeneratedContext] | None = None):
        if not units:
            return
        contexts = section_context() + (extra or [])
        body = _body(units)
        prefix = _context_text(contexts)
        index_text = (prefix + "\n\n" if prefix else "") + body
        if len(index_text.encode("utf-8")) > options.max_utf8_bytes:
            raise DocQAError(
                "CHUNK_BUDGET_EXCEEDED", "Internal chunk grouping exceeded its UTF-8 byte budget"
            )
        chunk = Chunk(
            chunk_profile_id=profile_id,
            doc_id=ir.doc_id,
            revision_id=ir.revision_id,
            artifact_id=ir.artifact_id,
            source_name=ir.source_name,
            category=ir.category,
            section=[item[1] for item in headings],
            display_text=body,
            index_text=index_text,
            index_text_sha256=sha256(index_text.encode("utf-8")).hexdigest(),
            index_utf8_bytes=len(index_text.encode("utf-8")),
            source_spans=[item.span for item in units],
            figure_ids=list(dict.fromkeys(figure for item in units for figure in item.figure_ids)),
            generated_context=contexts,
        )
        chunk.chunk_id = chunk_identity(chunk)
        chunks.append(chunk)

    def flush():
        nonlocal pending
        emit(pending)
        pending = []

    def units_for(
        block: Block,
        text: str,
        component: str,
        origin: str,
        *,
        figure: FigureOccurrence | None = None,
        row: int | None = None,
        extra: list[GeneratedContext] | None = None,
    ) -> list[_Unit]:
        if not text.strip():
            return []
        prefix = _context_text(section_context() + (extra or []))
        overhead = len((prefix + "\n\n" if prefix else "").encode("utf-8")) + len(
            (_LABELS[origin] + "\n").encode("utf-8")
        )
        capacity = options.max_utf8_bytes - overhead
        # Each long-component window can fit alone. target is a grouping goal;
        # maximum is a hard byte limit. Never claim these are tokenizer counts.
        ranges = _ranges(text, capacity, min(options.overlap_utf8_bytes, max(0, capacity // 3)))
        output = []
        for start, end in ranges:
            fragment = text[start:end]
            span = SourceSpan(
                block_id=block.block_id,
                component=component,
                text_start_codepoint=start,
                text_end_codepoint=end,
                source_text_sha256=sha256(fragment.encode("utf-8")).hexdigest(),
                markdown_start_byte=mapped[block.block_id]["start_byte"],
                markdown_end_byte=mapped[block.block_id]["end_byte"],
                locators=[figure.locator] if figure else block.locators,
                origin=origin,
                figure_id=figure.figure_id if figure else None,
                table_row_start=row,
                table_row_end=row + 1 if row is not None else None,
            )
            output.append(
                _Unit(fragment, span, [figure.figure_id] if figure else list(block.figure_ids))
            )
        return output

    def group(
        units: list[_Unit],
        extra: list[GeneratedContext] | None = None,
        *,
        merge_pending: bool = False,
    ):
        nonlocal pending
        current = pending if merge_pending else []
        prefix = _context_text(section_context() + (extra or []))
        prefix_bytes = len((prefix + "\n\n" if prefix else "").encode("utf-8"))
        for unit in units:
            proposed = current + [unit]
            size = prefix_bytes + len(_body(proposed).encode("utf-8"))
            if current and (size > options.target_utf8_bytes or size > options.max_utf8_bytes):
                emit(current, extra)
                current = []
            current.append(unit)
        if merge_pending:
            pending = current
        else:
            emit(current, extra)

    for block in sorted(ir.blocks, key=lambda item: item.order):
        if block.excluded_from_index:
            continue
        if block.type == "heading":
            flush()
            level = block.heading_level or 2
            headings = [item for item in headings if item[0] < level]
            if block.text.strip():
                headings.append((level, block.text, block.block_id))
            group(units_for(block, block.text, "block_text", block.origin), merge_pending=True)
        elif block.type == "table" and block.table_rows:
            flush()
            # A short table remains one semantic group. For a long table,
            # subsequent groups copy the first row as unverified display context.
            all_units = []
            for row, cells in enumerate(block.table_rows):
                all_units.extend(
                    units_for(block, " | ".join(cells), "table_row", block.origin, row=row)
                )
            prefix = _context_text(section_context())
            whole_size = len(
                ((prefix + "\n\n" if prefix else "") + _body(all_units)).encode("utf-8")
            )
            if whole_size <= options.max_utf8_bytes:
                emit(all_units)
            else:
                current = []
                continued = False
                header = GeneratedContext(
                    kind="table_header",
                    text=" | ".join(block.table_rows[0]),
                    source_block_ids=[block.block_id],
                )
                for row, cells in enumerate(block.table_rows):
                    extra = [header] if continued else []
                    for unit in units_for(
                        block, " | ".join(cells), "table_row", block.origin, row=row, extra=extra
                    ):
                        contexts = _context_text(section_context() + extra)
                        proposed = len(
                            (
                                (contexts + "\n\n" if contexts else "") + _body(current + [unit])
                            ).encode("utf-8")
                        )
                        if current and proposed > options.target_utf8_bytes:
                            emit(current, extra)
                            current = []
                            continued = True
                            extra = [header]
                            # Header overhead changed; re-split the unconsumed
                            # source interval rather than silently overfill.
                        capacity_prefix = _context_text(section_context() + extra)
                        available = options.max_utf8_bytes - len(
                            (
                                (capacity_prefix + "\n\n" if capacity_prefix else "")
                                + _LABELS[block.origin]
                                + "\n"
                            ).encode("utf-8")
                        )
                        if len(unit.text.encode("utf-8")) > available:
                            for local_start, local_end in _ranges(unit.text, available, 0):
                                start = unit.span.text_start_codepoint + local_start
                                end = unit.span.text_start_codepoint + local_end
                                fragment = unit.text[local_start:local_end]
                                sub = _Unit(
                                    fragment,
                                    unit.span.model_copy(
                                        update={
                                            "text_start_codepoint": start,
                                            "text_end_codepoint": end,
                                            "source_text_sha256": sha256(
                                                fragment.encode("utf-8")
                                            ).hexdigest(),
                                        }
                                    ),
                                    [],
                                )
                                if current:
                                    emit(current, extra)
                                    current = []
                                emit([sub], extra)
                        else:
                            current.append(unit)
                emit(current, [header] if continued else [])
        elif block.type == "figure":
            flush()
            for figure_id in block.figure_ids:
                if figure_id not in figures:
                    raise DocQAError(
                        "CHUNK_INVALID_ARTIFACT", "Figure block references a missing figure"
                    )
            extra = [
                GeneratedContext(
                    kind="figure_caption",
                    text=figures[figure_id].caption,
                    source_block_ids=[block.block_id],
                )
                for figure_id in block.figure_ids
                if figures[figure_id].caption.strip()
            ]
            units = units_for(block, block.text, "block_text", block.origin, extra=extra)
            for figure_id in block.figure_ids:
                figure = figures[figure_id]
                units.extend(
                    units_for(
                        block, figure.ocr_text, "figure_ocr", "ocr", figure=figure, extra=extra
                    )
                )
                units.extend(
                    units_for(
                        block,
                        figure_vl_text(figure),
                        "figure_vl",
                        "vl_generated",
                        figure=figure,
                        extra=extra,
                    )
                )
            context = _context_text(section_context() + extra)
            full_size = len(((context + "\n\n" if context else "") + _body(units)).encode("utf-8"))
            if full_size <= options.max_utf8_bytes:
                emit(units, extra)
            else:
                group(units, extra)
        else:
            group(units_for(block, block.text, "block_text", block.origin), merge_pending=True)
    flush()
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise DocQAError("CHUNK_DUPLICATE_ID", "Chunk identities unexpectedly collide")
    return chunks
