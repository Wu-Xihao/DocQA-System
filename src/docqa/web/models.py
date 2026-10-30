"""HTTP inputs contain questions and scope, never server paths or runner commands."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from docqa.domain.answers import OutputFormat
from docqa.domain.documents import Contract
from docqa.domain.formats import ALL_FORMATS
from docqa.domain.study import StudyPlan


class QuestionRequest(Contract):
    question: str = Field(min_length=1, max_length=4000)
    categories: list[str] | None = Field(default=None, max_length=100)
    doc_ids: list[str] | None = Field(default=None, max_length=1000)
    topics: list[str] | None = Field(default=None, min_length=1, max_length=12)
    output_format: OutputFormat = "markdown"
    doc_types: list[str] | None = Field(default=None, min_length=1, max_length=len(ALL_FORMATS))

    @field_validator("doc_types")
    @classmethod
    def unique_types(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Document types must be distinct")
        if value is not None and not set(value) <= set(ALL_FORMATS):
            raise ValueError("Unknown document type")
        return value

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


class MessageRequest(QuestionRequest):
    mode: Literal["retrieval", "chat"] = "retrieval"
    client_request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    scope_mode: Literal["all", "selected"] = "all"

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.mode == "chat":
            return self
        if self.scope_mode == "selected" and not self.doc_ids:
            raise ValueError("Selected scope cannot be empty")
        if self.doc_ids is not None and not self.doc_ids:
            raise ValueError("Empty document selection never means all")
        return self


class ConversationCreate(Contract):
    title: str = Field(default="新对话", min_length=1, max_length=100)


class ConversationPatch(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    pinned: bool | None = None
    archived: bool | None = None
    version: int | None = Field(default=None, ge=1)


class UploadCreate(Contract):
    relative_path: str | None = Field(default=None, max_length=2048)

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value):
        if value is not None:
            parts = value.split("/")
            if any(p in {"", ".", ".."} for p in parts) or any(c in value for c in "\\:\x00\r\n"):
                raise ValueError("Folder path must be a relative browser path")
        return value

    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=1024 * 1024 * 1024)
    category: str = Field(default="auto", min_length=1, max_length=100)


class ArtifactCreate(Contract):
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    answer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["table", "relation", "mindmap", "report", "slides", "wordcloud"]
    title: str | None = Field(default=None, min_length=1, max_length=100)


class ArtifactEdit(Contract):
    title: str = Field(min_length=1, max_length=100)
    plan: StudyPlan | None = None
