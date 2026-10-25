"""HTTP inputs contain questions and scope, never server paths or runner commands."""

from __future__ import annotations

from pydantic import Field, field_validator

from docqa.domain.answers import OutputFormat
from docqa.domain.documents import Contract


class QuestionRequest(Contract):
    question: str = Field(min_length=1, max_length=4000)
    categories: list[str] | None = Field(default=None, max_length=100)
    doc_ids: list[str] | None = Field(default=None, max_length=100)
    topics: list[str] | None = Field(default=None, min_length=1, max_length=12)
    output_format: OutputFormat = "markdown"

    @field_validator("question")
    @classmethod
    def question_text(cls, value):
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 16000:
            raise ValueError("Question is empty or exceeds the UTF-8 limit")
        return value.strip()

    @field_validator("categories", "doc_ids", "topics")
    @classmethod
    def identifiers(cls, values, info):
        maximum = 2000 if info.field_name == "topics" else 512
        if values is not None and any(
            not value.strip() or "\x00" in value or len(value) > maximum for value in values
        ):
            raise ValueError("Scope/topic values must be nonempty and bounded")
        if values is not None:
            for value in values:
                value.encode("utf-8")
        if info.field_name == "topics" and values is not None and len(values) != len(set(values)):
            raise ValueError("Required topics must be distinct")
        return values
