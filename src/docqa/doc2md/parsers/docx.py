"""Native DOCX body extraction with OOXML anchors and explicit visual coverage gaps.

DOCX does not provide a dependable physical-page map.  Every native location here
therefore identifies a part and an XML object; a later rendering pass may add its
own *derived* page locations without changing these anchors.
"""

from __future__ import annotations

from hashlib import sha256
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError

from docqa.doc2md.common import ParseContext
from docqa.domain.documents import (
    Block,
    FigureOccurrence,
    ParsedContent,
    ParseWarning,
    SourceLocator,
)
from docqa.domain.errors import DocQAError

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"


class _DocxParser:
    def __init__(self, ctx: ParseContext, document):
        self.ctx = ctx
        self.document = document
        self.result = ParsedContent(
            metadata={
                "format": "docx",
                "native_coverage": "body paragraphs, tables, embedded raster images",
                "needs_office_render": False,
                "physical_page_mapping": "unavailable",
            }
        )
        self._assets = {}
        self._warnings: set[tuple[str, str]] = set()
        self._styles = {style.style_id: style for style in document.styles}

    def warn(self, code: str, message: str, *, render: bool = False):
        key = code, message
        if key not in self._warnings:
            self.result.warnings.append(ParseWarning(code=code, message=message))
            self._warnings.add(key)
        if render:
            self.result.metadata["needs_office_render"] = True
            key = (
                "VISUAL_RENDER_REQUIRED",
                "DOCX has content requiring an Office rendering baseline",
            )
            if key not in self._warnings:
                self.result.warnings.append(ParseWarning(code=key[0], message=key[1]))
                self._warnings.add(key)

    def locator(self, element) -> SourceLocator:
        path = element.getroottree().getpath(element)
        return SourceLocator(
            locator_type="docx_object",
            native_ref={"part": str(self.document.part.partname), "object_path": path},
            precision="exact_object",
            display_label=f"DOCX body object {path}",
            doc_id=self.ctx.doc_id,
            revision_id=self.ctx.revision_id,
        )

    def append_block(self, kind: str, element, text: str = "", **kwargs):
        self.result.blocks.append(
            Block(
                block_id=self.ctx.identity(
                    "block", "docx", self.locator(element).native_ref, len(self.result.blocks)
                ),
                type=kind,
                text=text,
                order=len(self.result.blocks),
                locators=[self.locator(element)],
                **kwargs,
            )
        )

    def events(self, element):
        """Yield visible-native text and drawing events in their XML order.

        Deleted/moved-from revisions are not blended with their replacement.
        Revision handling remains marked unresolved; this is no substitute for
        accepting or rejecting revisions in Word/LibreOffice.
        """
        if element.tag in {_W + "del", _W + "moveFrom"}:
            return
        if element.tag in {_W + "drawing", _W + "pict"}:
            yield "drawing", element
            return
        if element.tag in {_W + "txbxContent", _W + "instrText"}:
            return
        if element.tag == _W + "t":
            yield "text", element.text or ""
        elif element.tag == _W + "tab":
            yield "text", "\t"
        elif element.tag in {_W + "br", _W + "cr"}:
            yield "text", "\n"
        else:
            for child in element:
                yield from self.events(child)

    def paragraph_info(self, element):
        metadata = {}
        level = None
        ppr = element.find(_W + "pPr")
        if ppr is not None:
            style_node = ppr.find(_W + "pStyle")
            style_id = style_node.get(_W + "val") if style_node is not None else None
            style = self._styles.get(style_id)
            if style is not None:
                metadata["style_id"] = style.style_id
                metadata["style_name"] = style.name
                # Built-in heading names are English even in localized Word.
                if style.name.lower().startswith("heading "):
                    suffix = style.name.rsplit(" ", 1)[-1]
                    if suffix.isdigit():
                        level = min(6, max(1, int(suffix)))
                visited = set()
                ancestor = style
                while level is None and ancestor is not None and ancestor.style_id not in visited:
                    visited.add(ancestor.style_id)
                    props = ancestor.element.find(_W + "pPr")
                    outline = props.find(_W + "outlineLvl") if props is not None else None
                    value = outline.get(_W + "val", "9") if outline is not None else "9"
                    if value.isdigit() and int(value) <= 5:
                        level = int(value) + 1
                    ancestor = ancestor.base_style
            outline = ppr.find(_W + "outlineLvl")
            if outline is not None:
                value = outline.get(_W + "val", "9")
                if value.isdigit() and int(value) <= 5:
                    level = int(value) + 1
            numbering = ppr.find(_W + "numPr")
            if numbering is not None:
                metadata["numbering"] = {
                    child.tag.rsplit("}", 1)[-1]: child.get(_W + "val", "") for child in numbering
                }
        hyperlinks = []
        for link in element.iter(_W + "hyperlink"):
            rel_id = link.get(_R + "id")
            target = link.get(_W + "anchor", "")
            if rel_id and rel_id in self.document.part.rels:
                target = str(self.document.part.rels[rel_id].target_ref)
            hyperlinks.append({"relationship_id": rel_id or "", "target": target})
        if hyperlinks:
            metadata["hyperlinks"] = hyperlinks
        kind = (
            "heading"
            if level
            else "list"
            if (
                "numbering" in metadata
                or str(metadata.get("style_name", "")).lower().startswith("list")
            )
            else "paragraph"
        )
        return kind, level, metadata

    def paragraph(self, element):
        kind, level, metadata = self.paragraph_info(element)
        text: list[str] = []

        def flush():
            value = "".join(text).strip()
            if value:
                self.append_block(kind, element, value, heading_level=level, metadata=metadata)
            text.clear()

        for event, value in self.events(element):
            if event == "text":
                text.append(value)
            else:
                flush()
                self.drawing(value)
        flush()

    def drawing(self, element):
        picture_uri = "http://schemas.openxmlformats.org/drawingml/2006/picture"
        if element.tag == _W + "pict" or any(
            graphic.get("uri") != picture_uri for graphic in element.iter(_A + "graphicData")
        ):
            self.warn(
                "COMPLEX_DRAWING_LAYOUT",
                "DOCX legacy/group/chart drawing requires rendering",
                render=True,
            )
        nodes = list(element.iter(_A + "blip")) + list(element.iter(_V + "imagedata"))
        if not nodes:
            self.warn(
                "DRAWING_NOT_EXTRACTED", "A native DOCX drawing needs visual rendering", render=True
            )
            return
        anchor = next(element.iter(_WP + "anchor"), None)
        src_rects = [dict(node.attrib) for node in element.iter(_A + "srcRect")]
        transforms = [dict(node.attrib) for node in element.iter(_A + "xfrm")]
        is_transformed = any(
            any(value not in {"0", "0.0"} for value in crop.values()) for crop in src_rects
        ) or any(
            any(value not in {"0", "0.0", "false"} for value in transform.values())
            for transform in transforms
        )
        floating = anchor is not None
        if floating:
            self.warn(
                "FLOATING_IMAGE_LAYOUT",
                "Floating DOCX image layout requires rendering",
                render=True,
            )
        if is_transformed:
            self.warn(
                "IMAGE_DISPLAY_TRANSFORM",
                "DOCX image crop/rotation requires rendering",
                render=True,
            )
        for occurrence_index, node in enumerate(nodes):
            relationship_id = node.get(_R + "embed") or node.get(_R + "id")
            if not relationship_id:
                self.warn(
                    "EXTERNAL_IMAGE_NOT_FETCHED",
                    "Linked DOCX images are never fetched",
                    render=True,
                )
                continue
            relationship = self.document.part.rels.get(relationship_id)
            if relationship is None:
                self.warn(
                    "IMAGE_REFERENCE_BROKEN",
                    "DOCX drawing references a missing relationship",
                    render=True,
                )
                continue
            if relationship.is_external:
                self.warn(
                    "EXTERNAL_IMAGE_NOT_FETCHED",
                    "Linked DOCX images are never fetched",
                    render=True,
                )
                continue
            part = relationship.target_part
            raw_data = part.blob
            try:
                asset = self.ctx.save_image(raw_data, f"block-{len(self.result.blocks):04d}_figure")
            except DocQAError as exc:
                if exc.code == "RESOURCE_LIMIT_EXCEEDED":
                    raise
                self.warn(
                    "EMBEDDED_IMAGE_UNSUPPORTED",
                    "A DOCX image cannot be decoded as raster data",
                    render=True,
                )
                continue
            self._assets.setdefault(asset.asset_id, asset)
            locator = self.locator(node)
            figure_id = self.ctx.identity("figure", "docx", locator.native_ref, occurrence_index)
            title = ""
            doc_pr = next(element.iter(_WP + "docPr"), None)
            if doc_pr is not None:
                title = doc_pr.get("descr") or doc_pr.get("title") or doc_pr.get("name", "")
            figure = FigureOccurrence(
                figure_id=figure_id,
                asset_id=asset.asset_id,
                original_asset_id=asset.derived_from,
                locator=locator,
                caption=title,
                # Only the displayed visual baseline may be interpreted when
                # native extraction exposes pixels hidden by an Office crop.
                analysis_required=not (is_transformed or floating),
                semantic_state="disabled" if is_transformed or floating else "pending",
                metadata={
                    "relationship_id": relationship_id,
                    "image_part": str(part.partname),
                    "raw_sha256": sha256(raw_data).hexdigest(),
                    "floating": floating,
                    "crop": src_rects,
                    "transforms": transforms,
                    "visible_region_policy": "office_render"
                    if is_transformed or floating
                    else "native_image",
                },
            )
            self.result.figures.append(figure)
            self.append_block("figure", node, figure_ids=[figure_id])

    def table(self, element):
        rows = []
        merge_info = []
        row_layout = []
        for row_number, row in enumerate(element.findall(_W + "tr")):
            cells = []
            row_properties = row.find(_W + "trPr")
            omissions = {}
            for side in ("gridBefore", "gridAfter"):
                node = row_properties.find(_W + side) if row_properties is not None else None
                value = node.get(_W + "val", "0") if node is not None else "0"
                omissions[side] = int(value) if value.isdigit() else 0
            row_layout.append({"row": row_number, **omissions})
            column = omissions["gridBefore"]
            for cell in row.findall(_W + "tc"):
                texts = []
                for paragraph in cell.iter(_W + "p"):
                    texts.append(
                        "".join(value for event, value in self.events(paragraph) if event == "text")
                    )
                cells.append("\n".join(texts).strip())
                properties = cell.find(_W + "tcPr")
                span = 1
                vertical = ""
                if properties is not None:
                    grid_span = properties.find(_W + "gridSpan")
                    if grid_span is not None:
                        raw_span = grid_span.get(_W + "val", "1")
                        span = int(raw_span) if raw_span.isdigit() else 1
                    vmerge = properties.find(_W + "vMerge")
                    if vmerge is not None:
                        vertical = vmerge.get(_W + "val", "continue")
                merge_info.append(
                    {
                        "row": row_number,
                        "column": column,
                        "grid_span": span,
                        "vertical_merge": vertical,
                    }
                )
                column += span
            rows.append(cells)
        self.append_block(
            "table",
            element,
            "\n".join("\t".join(row) for row in rows),
            table_rows=rows,
            metadata={
                "cell_layout": merge_info,
                "row_layout": row_layout,
                "row_representation": "native_xml_cells",
            },
        )
        if any(True for table in element.iter(_W + "tbl") if table is not element):
            self.warn(
                "COMPLEX_TABLE_STRUCTURE",
                "Nested DOCX table layout requires rendering",
                render=True,
            )
        # Figures keep their native cell anchors and are placed immediately after
        # the containing table's structured block, never detached into a final gallery.
        for event, value in self.events(element):
            if event == "drawing":
                self.drawing(value)

    def run(self) -> ParsedContent:
        body = self.document.element.body
        for node in body.iter():
            if node.tag in {_W + "ins", _W + "del", _W + "moveFrom", _W + "moveTo"}:
                self.warn(
                    "REVISIONS_UNRESOLVED",
                    "DOCX tracked revisions require explicit review",
                    render=True,
                )
            elif node.tag in {_W + "txbxContent", _W + "object", _W + "altChunk"}:
                self.warn(
                    "COMPLEX_OBJECT_NOT_PARSED",
                    "DOCX text box/embedded object/altChunk needs rendering",
                    render=True,
                )
            elif "officeDocument/2006/math" in node.tag:
                self.warn(
                    "FORMULA_NOT_PARSED",
                    "DOCX equation requires a visual rendering baseline",
                    render=True,
                )
        for relationship in self.document.part.rels.values():
            suffix = relationship.reltype.rsplit("/", 1)[-1]
            if suffix in {"header", "footer", "footnotes", "endnotes", "comments"}:
                self.warn(
                    "NON_BODY_CONTENT_NOT_PARSED",
                    f"DOCX {suffix} is outside native body extraction",
                    render=True,
                )
        self.body_children(body)
        self.result.assets = list(self._assets.values())
        return self.result

    def body_children(self, parent):
        for child in parent:
            if child.tag == _W + "p":
                self.paragraph(child)
            elif child.tag == _W + "tbl":
                self.table(child)
            elif child.tag in {_W + "sdt", _W + "sdtContent", _W + "ins", _W + "moveTo"}:
                self.body_children(child)


def parse_docx(ctx: ParseContext) -> ParsedContent:
    """Parse a validated DOCX snapshot; external relationships are never resolved."""
    try:
        document = Document(str(ctx.source))
        return _DocxParser(ctx, document).run()
    except DocQAError:
        raise
    except (
        PackageNotFoundError,
        BadZipFile,
        XMLSyntaxError,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OSError,
    ) as exc:
        raise DocQAError(
            "DOCUMENT_PARSE_FAILED", "DOCX package is damaged, encrypted, or unreadable"
        ) from exc
