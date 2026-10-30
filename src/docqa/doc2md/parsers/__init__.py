"""Native format adapters, registered by normalized format name.

Each parser takes a validated ParseContext and returns ParsedContent. Modules
are imported lazily so heavy dependencies load only for formats in use.
"""

from __future__ import annotations

from importlib import import_module

PARSERS = {
    "pdf": ("docqa.doc2md.parsers.pdf", "parse_pdf"),
    "docx": ("docqa.doc2md.parsers.docx", "parse_docx"),
    "pptx": ("docqa.doc2md.parsers.pptx", "parse_pptx"),
    "xlsx": ("docqa.doc2md.parsers.xlsx", "parse_xlsx"),
    "csv": ("docqa.doc2md.parsers.delimited", "parse_csv"),
}


def parse_native(context, fmt: str):
    try:
        module, name = PARSERS[fmt]
    except KeyError as exc:
        from docqa.domain.errors import DocQAError

        raise DocQAError("UNSUPPORTED_FORMAT", f"No native parser for {fmt}") from exc
    return getattr(import_module(module), name)(context)
