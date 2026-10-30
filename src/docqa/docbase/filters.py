"""Multi-condition hard filters evaluated on chunk metadata before recall.

Every condition narrows the eligible rows; conditions combine with AND and the
values inside one list combine with OR. Filters never widen categories/doc_ids
and never reorder candidates; ranking stays with the fusion and reranker.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from docqa.domain.documents import Contract
from docqa.domain.formats import ALL_FORMATS
from docqa.domain.rag import Chunk

ContentType = Literal["text", "table", "figure"]
Origin = Literal["native", "ocr", "vl_generated"]
_COMPONENT_TYPES = {
    "block_text": "text",
    "table_row": "table",
    "figure_ocr": "figure",
    "figure_vl": "figure",
}


class ChunkFilter(Contract):
    doc_types: list[str] | None = Field(default=None, min_length=1, max_length=len(ALL_FORMATS))
    content_types: list[ContentType] | None = Field(default=None, min_length=1, max_length=3)
    origins: list[Origin] | None = Field(default=None, min_length=1, max_length=3)
    # PDF physical pages, PPTX slides and converted pages share one 1-based range.
    page_from: int | None = Field(default=None, ge=1, le=100000)
    page_to: int | None = Field(default=None, ge=1, le=100000)
    source_name_contains: str | None = Field(default=None, min_length=1, max_length=200)
    section_contains: str | None = Field(default=None, min_length=1, max_length=200)
    # Each term must appear (case-insensitive) in the chunk text: exact must-match terms.
    must_contain: list[str] | None = Field(default=None, min_length=1, max_length=10)
    exclude_doc_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)

    @field_validator("doc_types")
    @classmethod
    def known_types(cls, value):
        if value is not None:
            value = [item.strip().lower().lstrip(".") for item in value]
            if not set(value) <= set(ALL_FORMATS):
                raise ValueError("Unknown document type")
        return value

    @field_validator("must_contain", "exclude_doc_ids")
    @classmethod
    def nonempty_terms(cls, value):
        if value is not None and any(not item.strip() or len(item) > 200 for item in value):
            raise ValueError("Filter terms must be nonempty and at most 200 characters")
        return value

    @model_validator(mode="after")
    def ordered_pages(self):
        if self.page_from and self.page_to and self.page_from > self.page_to:
            raise ValueError("page_from must not exceed page_to")
        return self

    def is_empty(self) -> bool:
        return not any(value is not None for value in self.model_dump().values())

    def matches(self, chunk: Chunk) -> bool:
        if self.exclude_doc_ids and chunk.doc_id in self.exclude_doc_ids:
            return False
        if self.doc_types and PurePath(chunk.source_name).suffix.lower().lstrip(".") not in (
            self.doc_types
        ):
            return False
        if self.content_types:
            kinds = {_COMPONENT_TYPES[span.component] for span in chunk.source_spans}
            if chunk.figure_ids:
                kinds.add("figure")
            if not kinds & set(self.content_types):
                return False
        if self.origins and not {span.origin for span in chunk.source_spans} & set(self.origins):
            return False
        if self.page_from or self.page_to:
            low, high = self.page_from or 1, self.page_to or 100000
            pages = {
                locator.page or locator.slide
                for span in chunk.source_spans
                for locator in span.locators
                if (locator.page or locator.slide)
            }
            if not any(low <= page <= high for page in pages):
                return False
        if self.source_name_contains and (
            self.source_name_contains.casefold() not in chunk.source_name.casefold()
        ):
            return False
        if self.section_contains and not any(
            self.section_contains.casefold() in heading.casefold() for heading in chunk.section
        ):
            return False
        if self.must_contain:
            text = chunk.display_text.casefold()
            if not all(term.casefold() in text for term in self.must_contain):
                return False
        return True


def coerce_filter(value) -> ChunkFilter | None:
    if value is None:
        return None
    selected = value if isinstance(value, ChunkFilter) else ChunkFilter.model_validate(value)
    return None if selected.is_empty() else selected
