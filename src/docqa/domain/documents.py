"""doc2md v1 contracts; all positions are version-bound, never guessed page numbers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

UnitFloat = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
BBox = tuple[UnitFloat, UnitFloat, UnitFloat, UnitFloat]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParseWarning(Contract):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


class SourceLocator(Contract):
    locator_type: Literal["pdf_page", "docx_object", "pptx_shape", "sheet_range", "derived_page"]
    native_ref: dict[str, JsonValue] = Field(default_factory=dict)
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    bbox: BBox | None = None
    precision: Literal["exact_object", "exact_region", "page_only", "section_only"]
    display_label: str
    doc_id: str = ""
    revision_id: str = ""
    artifact_id: str | None = None

    @model_validator(mode="after")
    def check_position(self):
        if self.bbox and (self.bbox[0] > self.bbox[2] or self.bbox[1] > self.bbox[3]):
            raise ValueError("bbox is not ordered")
        if self.locator_type == "docx_object" and self.page is not None:
            raise ValueError("DOCX native locators must not invent page numbers")
        if self.locator_type == "pdf_page" and self.page is None:
            raise ValueError("PDF locator requires a physical page number")
        if self.locator_type == "pptx_shape" and self.slide is None:
            raise ValueError("PPTX locator requires slide index")
        if self.locator_type == "sheet_range" and (
            not self.native_ref.get("sheet") or self.page is not None or self.slide is not None
        ):
            raise ValueError("Sheet locator requires a sheet name and no page/slide")
        return self


class VisionEntity(Contract):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(max_length=2000)
    bbox: BBox | None = None

    @model_validator(mode="after")
    def check_bbox(self):
        if self.bbox and (self.bbox[0] > self.bbox[2] or self.bbox[1] > self.bbox[3]):
            raise ValueError("vision bbox is not ordered")
        return self


class VisionRelation(Contract):
    source: str
    target: str
    direction: Literal["directed", "undirected", "unknown"] = "unknown"
    label: str = Field(default="", max_length=2000)


class VisionDescription(Contract):
    schema_version: Literal["vision.v1"] = "vision.v1"
    status: Literal["ok", "partial", "unreadable", "no_semantic_content"]
    image_type: str = Field(default="unknown", max_length=100)
    entities: list[VisionEntity] = Field(default_factory=list, max_length=300)
    relations: list[VisionRelation] = Field(default_factory=list, max_length=600)
    observations: list[str] = Field(default_factory=list, max_length=300)
    interpretations: list[str] = Field(default_factory=list, max_length=100)
    uncertainties: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [entity.id for entity in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate vision entity IDs")
        if any(edge.source not in ids or edge.target not in ids for edge in self.relations):
            raise ValueError("vision relation references an unknown entity")
        if self.status == "ok" and not (self.observations or self.entities):
            raise ValueError("ok vision output needs visible observations or entities")
        return self


class ImageAsset(Contract):
    asset_id: str
    sha256: str
    relative_path: str
    media_type: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    derived_from: str | None = None


class FigureOccurrence(Contract):
    figure_id: str
    asset_id: str
    locator: SourceLocator
    caption: str = ""
    original_asset_id: str | None = None
    full_page: bool = False
    analysis_required: bool = True
    ocr_text: str = ""
    vision: VisionDescription | None = None
    semantic_state: Literal["pending", "disabled", "described", "ocr_only", "failed"] = "pending"
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Block(Contract):
    block_id: str
    type: Literal["heading", "paragraph", "list", "table", "figure", "note", "formula"]
    text: str = ""
    order: int = Field(ge=0)
    locators: list[SourceLocator] = Field(min_length=1)
    origin: Literal["native", "ocr", "vl_generated"] = "native"
    heading_level: int | None = Field(default=None, ge=1, le=6)
    table_rows: list[list[str]] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    excluded_from_index: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ParsedContent(Contract):
    blocks: list[Block] = Field(default_factory=list)
    assets: list[ImageAsset] = Field(default_factory=list)
    figures: list[FigureOccurrence] = Field(default_factory=list)
    warnings: list[ParseWarning] = Field(default_factory=list)
    page_count: int | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class DocumentIR(ParsedContent):
    schema_version: Literal["docqa.ir.v1"] = "docqa.ir.v1"
    doc_id: str
    revision_id: str
    content_sha256: str
    parse_profile_id: str
    artifact_id: str | None = None
    source_name: str
    source_format: str
    category: str = "default"


class ParseResult(Contract):
    schema_version: Literal["docqa.parse.v1"] = "docqa.parse.v1"
    status: Literal["complete", "review_required", "partial", "failed"]
    doc_id: str
    revision_id: str
    artifact_id: str
    artifact_dir: str
    markdown_path: str
    eligible_for_indexing: bool
    reused: bool = False
    warnings: list[ParseWarning] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
