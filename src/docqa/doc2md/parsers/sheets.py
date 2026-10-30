"""Shared tabular block construction for XLSX worksheets and CSV files.

A sheet becomes one heading plus table segments of at most SEGMENT_ROWS data
rows. Every segment repeats the sheet's real first row (marked in metadata), so
a retrieved fragment keeps its column meaning; locators carry A1-style ranges.
"""

from __future__ import annotations

from datetime import date, datetime, time

from docqa.doc2md.common import ParseContext
from docqa.domain.documents import Block, ParsedContent, ParseWarning, SourceLocator

SEGMENT_ROWS = 50
MAX_COLUMNS = 256
MAX_CELLS = 2_000_000


def column_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).strip()


def trim(rows: list[list[str]]) -> tuple[list[list[str]], int, int]:
    """Drop empty edges; return rows plus the 1-based first row/column kept."""
    filled = [i for i, row in enumerate(rows) if any(row)]
    if not filled:
        return [], 1, 1
    rows = rows[filled[0] : filled[-1] + 1]
    columns = [j for row in rows for j, value in enumerate(row) if value]
    first, last = min(columns), max(columns)
    kept = [row[first : last + 1] + [""] * (last + 1 - len(row)) for row in rows]
    return [row[: last + 1 - first] for row in kept], filled[0] + 1, first + 1


class SheetBuilder:
    def __init__(self, ctx: ParseContext, fmt: str, result: ParsedContent):
        self.ctx, self.fmt, self.result = ctx, fmt, result
        self.cells = 0
        self._warned: set[str] = set()

    def warn(self, code: str, message: str):
        if code not in self._warned:
            self._warned.add(code)
            self.result.warnings.append(ParseWarning(code=code, message=message))

    def locator(self, sheet: str, cell_range: str | None, label: str, **extra) -> SourceLocator:
        return SourceLocator(
            locator_type="sheet_range",
            native_ref={"sheet": sheet, "range": cell_range, **extra},
            precision="exact_region" if cell_range else "section_only",
            display_label=label,
            doc_id=self.ctx.doc_id,
            revision_id=self.ctx.revision_id,
        )

    def block(self, kind, locator, text="", **kwargs):
        self.result.blocks.append(
            Block(
                block_id=self.ctx.identity(
                    "block", self.fmt, locator.native_ref, len(self.result.blocks)
                ),
                type=kind,
                text=text,
                order=len(self.result.blocks),
                locators=[locator],
                **kwargs,
            )
        )

    def add_sheet(self, sheet: str, rows: list[list[str]], *, sheet_index: int, metadata=None):
        rows, row0, column0 = trim(rows)
        if len(rows) and len(rows[0]) > MAX_COLUMNS:
            self.warn("SHEET_COLUMNS_TRUNCATED", f"Only the first {MAX_COLUMNS} columns are kept")
            rows = [row[:MAX_COLUMNS] for row in rows]
        heading = f"工作表 {sheet}" if self.fmt == "xlsx" else sheet
        self.block(
            "heading",
            self.locator(sheet, None, f"{self.ctx.source_name} · {heading}", index=sheet_index),
            sheet,
            heading_level=2,
            metadata={"sheet_index": sheet_index, **(metadata or {})},
        )
        if not rows:
            return
        width = len(rows[0])
        if self.cells + width * len(rows) > MAX_CELLS:
            allowed = max(0, (MAX_CELLS - self.cells) // max(width, 1))
            self.warn("SHEET_CELLS_TRUNCATED", f"Cell budget {MAX_CELLS} reached; rows truncated")
            rows = rows[:allowed]
            if not rows:
                return
        self.cells += width * len(rows)
        last_column = column_letter(column0 + width - 1)
        first_column = column_letter(column0)
        header, body = rows[0], rows[1:] or []
        header_range = f"{first_column}{row0}:{last_column}{row0}"
        if not body:
            body_segments = [(0, [])]
        else:
            body_segments = [
                (start, body[start : start + SEGMENT_ROWS])
                for start in range(0, len(body), SEGMENT_ROWS)
            ]
        for number, (start, segment) in enumerate(body_segments):
            top = row0 + (1 + start if segment else 0)
            bottom = row0 + start + len(segment)
            cell_range = f"{first_column}{top}:{last_column}{bottom}"
            label = f"{self.ctx.source_name} · {heading} {cell_range}"
            table = [header, *segment]
            self.block(
                "table",
                self.locator(sheet, cell_range, label, header_range=header_range),
                "\n".join("\t".join(row) for row in table),
                table_rows=table,
                metadata={
                    "sheet": sheet,
                    "sheet_index": sheet_index,
                    "segment": number,
                    "header_range": header_range,
                    # Row 0 of every segment after the first is the copied sheet header.
                    "repeated_header": number > 0,
                },
            )
