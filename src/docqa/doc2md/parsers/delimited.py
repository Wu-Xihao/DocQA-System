"""Native CSV/delimited-text extraction into one sheet-range-located table.

Encoding is decoded strictly (UTF-8 with or without BOM, then GB18030); bytes
that decode under neither are rejected instead of being replaced silently.
"""

from __future__ import annotations

import csv
import io

from docqa.doc2md.common import ParseContext
from docqa.doc2md.parsers.sheets import SheetBuilder
from docqa.domain.documents import ParsedContent
from docqa.domain.errors import DocQAError

ENCODINGS = ("utf-8-sig", "gb18030")


def decode(raw: bytes) -> tuple[str, str]:
    if b"\x00" in raw:
        raise DocQAError("FORMAT_MISMATCH", "CSV contains NUL bytes; it is not delimited text")
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise DocQAError("DOCUMENT_PARSE_FAILED", "CSV encoding is not UTF-8 or GB18030")


def parse_csv(ctx: ParseContext) -> ParsedContent:
    text, encoding = decode(ctx.source.read_bytes())
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    try:
        rows = [
            [cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        ]
    except csv.Error as exc:
        raise DocQAError("DOCUMENT_PARSE_FAILED", "CSV structure cannot be read") from exc
    result = ParsedContent(
        metadata={
            "format": "csv",
            "native_coverage": "all delimited cells",
            "needs_office_render": False,
            "physical_page_mapping": "unavailable",
            "encoding": encoding,
            "delimiter": delimiter,
        }
    )
    stem = ctx.source_name.rsplit(".", 1)[0] or "CSV"
    SheetBuilder(ctx, "csv", result).add_sheet(stem, rows, sheet_index=1)
    return result
