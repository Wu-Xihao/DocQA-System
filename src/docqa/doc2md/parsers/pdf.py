"""PDF native extraction and visual preservation.

The adapter deliberately exposes a conservative line-based reading order.  It
does not claim to solve arbitrary multicolumn layouts, merge tables across pages,
or perform OCR.  Raster and vector content is retained in a rendered page, while
the common pipeline owns OCR and vision enrichment.
"""

from __future__ import annotations

import math
import re
from contextlib import closing
from io import BytesIO
from threading import RLock

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

from docqa.doc2md.common import ParseContext
from docqa.domain.documents import (
    Block,
    FigureOccurrence,
    ImageAsset,
    ParsedContent,
    ParseWarning,
    SourceLocator,
)
from docqa.domain.errors import DocQAError

# PDFium is process-global and not thread-safe, even for different documents.
# Keep construction, child operations, bitmap encoding and explicit close inside
# the same lock. Other modules needing PDFium must reuse this lock.
PDFIUM_LOCK = RLock()


def _validate_document(ctx: ParseContext, rendered):
    if pdfium_raw.FPDF_GetSecurityHandlerRevision(rendered) >= 0:
        raise DocQAError(
            "PASSWORD_REQUIRED", "Encrypted PDF sources are not accepted by the current adapter"
        )
    if len(rendered) > ctx.max_pages:
        raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "PDF exceeds configured page limit")
    if not len(rendered):
        raise DocQAError("PARSE_CORRUPT", "PDF contains no pages")


def _bbox(page, bounds):
    """Normalize pdfplumber's already-rotated, top-left page coordinates."""
    x0, top, x1, bottom = (float(v) for v in bounds)
    origin_x, origin_y = page.bbox[:2]
    return (
        min(1.0, max(0.0, (x0 - origin_x) / page.width)),
        min(1.0, max(0.0, (top - origin_y) / page.height)),
        min(1.0, max(0.0, (x1 - origin_x) / page.width)),
        min(1.0, max(0.0, (bottom - origin_y) / page.height)),
    )


def _raw_page_boxes(page):
    """Keep PDF user-space rectangles separate from display coordinates."""
    media = tuple(float(v) for v in page.page_obj.mediabox)
    crop = tuple(float(v) for v in page.page_obj.cropbox)
    mx0, mx1 = sorted((media[0], media[2]))
    my0, my1 = sorted((media[1], media[3]))
    cx0, cx1 = sorted((crop[0], crop[2]))
    cy0, cy1 = sorted((crop[1], crop[3]))
    visible = (max(mx0, cx0), max(my0, cy0), min(mx1, cx1), min(my1, cy1))
    return media, crop, visible


def _visible_page(page, render_page, content):
    """Clip native objects to the same rotated CropBox that PDFium displays.

    pdfplumber's ``Page.bbox`` defaults to MediaBox, and its ``cropbox`` helper
    does not reflect asymmetric rectangles for rotations 180/270. Transform the
    *raw* rectangle through the PDF display rotation instead. Character objects
    crossing the clip boundary are omitted, rather than exposing an entire
    partly invisible glyph as if it were visible source text.
    """
    media, _crop, visible = _raw_page_boxes(page)
    if not all(math.isfinite(v) for v in (*media, *visible)):
        raise DocQAError("PARSE_CORRUPT", "PDF page has invalid media/crop coordinates")
    mx0, mx1 = sorted((media[0], media[2]))
    my0, my1 = sorted((media[1], media[3]))
    x0, y0, x1, y1 = visible
    rotation = int(page.rotation)
    if rotation not in {0, 90, 180, 270} or rotation != render_page.get_rotation():
        raise DocQAError("PDF_GEOMETRY_UNSUPPORTED", "PDF page rotation cannot be mapped reliably")
    if x0 >= x1 or y0 >= y1:
        raise DocQAError("PARSE_CORRUPT", "PDF CropBox has no visible intersection with MediaBox")
    if any(abs(a - b) > 0.01 for a, b in zip(visible, render_page.get_bbox(), strict=True)):
        raise DocQAError("PDF_GEOMETRY_UNSUPPORTED", "PDF parsers disagree on the visible page box")

    def to_display(x, y):
        # Equivalent to pdfminer's per-page CTM, followed by pdfplumber's
        # conversion from rotated bottom-left to rotated top-left coordinates.
        if rotation == 0:
            dx, dy = x - mx0, y - my0
        elif rotation == 90:
            dx, dy = y - my0, mx1 - x
        elif rotation == 180:
            dx, dy = mx1 - x, my1 - y
        else:
            dx, dy = my1 - y, x - mx0
        return dx + page.mediabox[0], page.height - dy + page.mediabox[1]

    corners = [to_display(x, y) for x in (x0, x1) for y in (y0, y1)]
    bounds = (
        min(x for x, _ in corners),
        min(y for _, y in corners),
        max(x for x, _ in corners),
        max(y for _, y in corners),
    )
    displayed_width, displayed_height = render_page.get_size()
    if (
        abs((bounds[2] - bounds[0]) - displayed_width) > 0.01
        or abs((bounds[3] - bounds[1]) - displayed_height) > 0.01
    ):
        raise DocQAError(
            "PDF_GEOMETRY_UNSUPPORTED", "PDF native and rendered coordinate scales differ"
        )

    boundary_characters = 0

    def visible_object(obj):
        nonlocal boundary_characters
        if obj.get("object_type") != "char":
            return True
        left, top, right, bottom = obj["x0"], obj["top"], obj["x1"], obj["bottom"]
        within = (
            left >= bounds[0] - 1e-6
            and top >= bounds[1] - 1e-6
            and right <= bounds[2] + 1e-6
            and bottom <= bounds[3] + 1e-6
        )
        if (
            not within
            and right > bounds[0]
            and bottom > bounds[1]
            and left < bounds[2]
            and top < bounds[3]
        ):
            boundary_characters += 1
        return within

    # Evaluate the filter before checking the count; FilteredPage is lazy.
    filtered = page.filter(visible_object)
    _ = filtered.objects
    if boundary_characters:
        content.warnings.append(
            ParseWarning(
                code="PDF_CROP_BOUNDARY_TEXT",
                message=f"PDF page {page.page_number}: {boundary_characters} glyphs cross the visible crop boundary and were excluded from native text; check the page image",
            )
        )
    return filtered.crop(bounds)


def _locator(ctx: ParseContext, page, bounds=None, *, extra=None):
    media, crop, visible = _raw_page_boxes(page)
    native_ref = {
        "physical_page": page.page_number,
        "coordinate_system": "pdfplumber_rotated_top_left_points",
        "page_bbox_points": [float(v) for v in page.bbox],
        "media_box_points": [float(v) for v in page.mediabox],
        "media_box_pdf_user_space_points": list(media),
        "crop_box_pdf_user_space_points": list(crop),
        "visible_box_pdf_user_space_points": list(visible),
        "normalized_coordinate_basis": "visible_rotated_cropbox",
        "rotation_degrees": int(page.rotation),
    }
    if bounds is not None:
        native_ref["bbox_points"] = [float(v) for v in bounds]
    if extra:
        native_ref.update(extra)
    return SourceLocator(
        doc_id=ctx.doc_id,
        revision_id=ctx.revision_id,
        locator_type="pdf_page",
        native_ref=native_ref,
        page=page.page_number,
        bbox=_bbox(page, bounds) if bounds is not None else None,
        precision="exact_region" if bounds is not None else "page_only",
        display_label=f"{ctx.source_name} · PDF 第 {page.page_number} 页",
    )


def _inside_table(obj, table_bounds):
    if obj.get("object_type") != "char":
        return False
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in table_bounds)


def _text_lines(page):
    """Read cardinal text baselines without reversing upside-down glyphs.

    Page rotation alone is insufficient: one page can also contain individually
    counter-rotated labels. Group by each glyph's baseline matrix and normalize
    only the text projection; all returned bounds remain display coordinates.
    This is a geometric heuristic, not a linguistic/bidirectional layout engine.
    """

    def direction(char):
        a, b, *_ = char["matrix"]
        if abs(a) >= abs(b):
            return "ltr" if a >= 0 else "rtl"
        return "ttb" if b < 0 else "btt"

    for char_direction in ("ltr", "rtl", "ttb", "btt"):
        subset = page.filter(
            lambda obj: obj.get("object_type") != "char" or direction(obj) == char_direction
        )
        line_direction = "ttb" if char_direction in {"ltr", "rtl"} else "ltr"
        yield from subset.extract_text_lines(
            return_chars=False,
            line_dir=line_direction,
            char_dir=char_direction,
            line_dir_rotated=line_direction,
            char_dir_rotated=char_direction,
            # Output the logical strings, not a re-rotated text canvas.
            line_dir_render="ttb",
            char_dir_render="ltr",
        )


def _extract_native(ctx, page, content):
    """Return sorted native blocks and whether meaningful text was extracted."""
    candidates = []
    table_bounds = []
    for table_index, table in enumerate(page.find_tables()):
        rows = []
        for row in table.rows:
            values = []
            for cell in row.cells:
                if cell is None:
                    values.append("")
                    continue
                # Match table membership by glyph centre while retaining the
                # same baseline-aware reading used by ordinary paragraphs.
                subset = page.filter(
                    lambda obj, cell=cell: (
                        obj.get("object_type") == "char"
                        and cell[0] <= (obj["x0"] + obj["x1"]) / 2 < cell[2]
                        and cell[1] <= (obj["top"] + obj["bottom"]) / 2 < cell[3]
                    )
                )
                lines = sorted(_text_lines(subset), key=lambda line: (line["top"], line["x0"]))
                values.append("\n".join(line["text"].strip() for line in lines))
            rows.append(values)
        # A graphic composed of empty boxes must remain a visual, not an empty
        # table that falsely claims successful semantic extraction.
        if not any(any(cell for cell in row) for row in rows):
            continue
        bounds = tuple(float(v) for v in table.bbox)
        table_bounds.append(bounds)
        block = Block(
            block_id=ctx.identity("pdf", page.page_number, "table", table_index),
            type="table",
            text="\n".join(" | ".join(row) for row in rows),
            order=0,
            locators=[_locator(ctx, page, bounds)],
            table_rows=rows,
            metadata={"extraction": "pdfplumber_ruled_table", "cross_page_merged": False},
        )
        candidates.append((bounds[1], bounds[0], 0, block))

    text_page = page.filter(lambda obj: not _inside_table(obj, table_bounds))
    for line_index, line in enumerate(_text_lines(text_page)):
        text = line["text"].strip()
        if not text:
            continue
        bounds = (line["x0"], line["top"], line["x1"], line["bottom"])
        block = Block(
            block_id=ctx.identity("pdf", page.page_number, "line", line_index),
            type="paragraph",
            text=text,
            order=0,
            locators=[_locator(ctx, page, bounds)],
            metadata={"extraction": "pdfplumber_geometric_line"},
        )
        candidates.append((bounds[1], bounds[0], 1, block))

    for _, _, _, block in sorted(candidates, key=lambda item: item[:3]):
        block.order = len(content.blocks)
        content.blocks.append(block)
    all_text = "".join(block.text for _, _, _, block in candidates)
    # An alphanumeric character is enough to avoid rerunning OCR on a valid short
    # title. This only detects absent/garbled text, not OCR layer correctness.
    meaningful_text = any(c.isalnum() for c in all_text)
    if "\ufffd" in all_text or re.search(r"\(cid:\d+\)", all_text):
        meaningful_text = False
        content.warnings.append(
            ParseWarning(
                code="PDF_TEXT_ENCODING_SUSPECT",
                message=f"PDF page {page.page_number}: replacement characters or unresolved CID glyphs were found; OCR review is required",
            )
        )
    return meaningful_text


def _embedded_images(ctx, render_page, page, content):
    """Preserve decoded image streams; the page render remains visual authority.

    PDFium extraction can omit masks and page transforms. Therefore these assets
    supplement, rather than replace, the composed page image.
    """
    images = []
    for image_index, image in enumerate(
        render_page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE])
    ):
        try:
            width, height = image.get_px_size()
            if width * height > ctx.max_pixels:
                raise DocQAError(
                    "RESOURCE_LIMIT_EXCEEDED", "PDF embedded image exceeds pixel limit"
                )
            buffer = BytesIO()
            image.extract(buffer)
            asset = ctx.save_image(
                buffer.getvalue(), f"Page{page.page_number}_Image{image_index + 1}"
            )
            if asset.asset_id not in {item.asset_id for item in content.assets}:
                content.assets.append(asset)
            images.append((asset, image_index, [float(v) for v in image.get_bounds()]))
        except DocQAError as exc:
            if exc.code == "RESOURCE_LIMIT_EXCEEDED":
                raise
            content.warnings.append(
                ParseWarning(
                    code="PDF_IMAGE_EXTRACTION_FAILED",
                    message=f"PDF page {page.page_number}, image {image_index + 1}: {exc.message}; page rendering is the fallback",
                )
            )
        except (pdfium.PdfiumError, ValueError, OSError, RuntimeError) as exc:
            content.warnings.append(
                ParseWarning(
                    code="PDF_IMAGE_EXTRACTION_FAILED",
                    message=f"PDF page {page.page_number}, image {image_index + 1} could not be extracted ({type(exc).__name__}); page rendering is the fallback",
                )
            )
    return images


def _render_asset(ctx, render_page, page_number):
    scale = ctx.render_dpi / 72
    width, height = render_page.get_size()
    pixel_count = math.ceil(width * scale) * math.ceil(height * scale)
    if pixel_count > ctx.max_pixels:
        raise DocQAError(
            "RESOURCE_LIMIT_EXCEEDED", "PDF rendered page exceeds configured pixel limit"
        )
    bitmap = render_page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        try:
            buffer = BytesIO()
            image.save(buffer, format="PNG")
        finally:
            image.close()
    finally:
        bitmap.close()
    return ctx.save_image(buffer.getvalue(), f"Page{page_number}_FullPage")


def _page_figure(ctx, render_page, page, content, embedded, needs_ocr):
    asset = _render_asset(ctx, render_page, page.page_number)
    if asset.asset_id not in {item.asset_id for item in content.assets}:
        content.assets.append(asset)
    figure_id = ctx.identity("pdf", page.page_number, "full_page")
    locator = _locator(ctx, page, page.bbox, extra={"render_dpi": ctx.render_dpi})
    content.figures.append(
        FigureOccurrence(
            figure_id=figure_id,
            asset_id=asset.asset_id,
            locator=locator,
            caption=f"PDF 第 {page.page_number} 页（整页视觉上下文）",
            original_asset_id=(embedded[0][0].derived_from or embedded[0][0].asset_id)
            if len(embedded) == 1
            else None,
            full_page=True,
            metadata={
                "needs_ocr": needs_ocr,
                "extraction": "pdfium_page_render",
                "render_dpi": ctx.render_dpi,
            },
        )
    )
    content.blocks.append(
        Block(
            block_id=ctx.identity("pdf", page.page_number, "visual_block"),
            type="figure",
            order=len(content.blocks),
            locators=[locator],
            figure_ids=[figure_id],
            metadata={"native_text_already_extracted": not needs_ocr},
        )
    )
    return figure_id


def _region_figures(ctx, render_page, page, content, needs_ocr):
    from docqa.doc2md.regions import graphic_regions

    regions, scan, limited = graphic_regions(page)
    if limited:
        content.warnings.append(
            ParseWarning(
                code="VISUAL_REGION_LIMIT",
                message=f"Page {page.page_number}: graphic detection exceeded its resource limit; check the original",
            )
        )
    ids = []
    # A full-page scan is a text/OCR source, never automatically a diagram for VL.
    if scan and (needs_ocr or len(page.chars) < 80):
        embedded = _embedded_images(ctx, render_page, page, content)
        identity = _page_figure(ctx, render_page, page, content, embedded, True)
        figure = content.figures[-1]
        figure.caption = f"扫描第 {page.page_number} 页正文 OCR（非独立图表）"
        figure.metadata.update(role="page_ocr", vision_required=False, exclude_gallery=True)
        content.warnings.append(
            ParseWarning(
                code="SCANNED_FIGURES_NEED_REVIEW",
                message=f"Page {page.page_number}: OCR text preserved; figures inside the scan require region review",
            )
        )
        return [identity]
    if scan:
        content.warnings.append(
            ParseWarning(
                code="SCANNED_FIGURES_NEED_REVIEW",
                message=f"Page {page.page_number}: native text layer retained; diagrams inside the full-page scan need region review",
            )
        )
    if not regions:
        return ids
    scale = ctx.render_dpi / 72
    w, h = render_page.get_size()
    if math.ceil(w * scale) * math.ceil(h * scale) > ctx.max_pixels:
        raise DocQAError("RESOURCE_LIMIT_EXCEEDED", "PDF page exceeds region-render pixel limit")
    bitmap = render_page.render(scale=scale)
    try:
        image = bitmap.to_pil()
        try:
            for number, region in enumerate(regions, 1):
                b = _bbox(page, region["box"])
                crop = image.crop(
                    (
                        max(0, math.floor(b[0] * image.width)),
                        max(0, math.floor(b[1] * image.height)),
                        min(image.width, math.ceil(b[2] * image.width)),
                        min(image.height, math.ceil(b[3] * image.height)),
                    )
                )
                try:
                    buffer = BytesIO()
                    crop.save(buffer, format="PNG")
                finally:
                    crop.close()
                asset = ctx.save_image(buffer.getvalue(), f"Page{page.page_number}_Region{number}")
                if asset.asset_id not in {a.asset_id for a in content.assets}:
                    content.assets.append(asset)
                locator = _locator(
                    ctx,
                    page,
                    region["box"],
                    extra={"render_dpi": ctx.render_dpi, "region_detection": "graphic_geometry.v1"},
                )
                figure_id = ctx.identity("pdf-region", page.page_number, number, list(b))
                content.figures.append(
                    FigureOccurrence(
                        figure_id=figure_id,
                        asset_id=asset.asset_id,
                        locator=locator,
                        caption=region["caption"] or f"第 {page.page_number} 页图表 {number}",
                        metadata={
                            "extraction": "pdfium_region_render",
                            "needs_ocr": region["raster"] or not region["native_text"].strip(),
                            "native_text_in_region": region["native_text"],
                            "primitive_count": region["primitive_count"],
                            "region_detection": "heuristic",
                        },
                    )
                )
                content.blocks.append(
                    Block(
                        block_id=ctx.identity("pdf-region-block", figure_id),
                        type="figure",
                        order=len(content.blocks),
                        locators=[locator],
                        figure_ids=[figure_id],
                    )
                )
                ids.append(figure_id)
        finally:
            image.close()
    finally:
        bitmap.close()
    return ids


def _fallback_figures(ctx, page, content, embedded, needs_ocr):
    """Preserve available bitmaps if the user explicitly disables rendering."""
    figure_ids = []
    for asset, image_index, raw_bounds in embedded:
        figure_id = ctx.identity("pdf", page.page_number, "embedded_image", image_index)
        locator = _locator(
            ctx,
            page,
            extra={
                "image_object_index": image_index,
                "image_pdfium_bounds_bottom_left_points": raw_bounds,
            },
        )
        content.figures.append(
            FigureOccurrence(
                figure_id=figure_id,
                asset_id=asset.asset_id,
                locator=locator,
                caption=f"PDF 第 {page.page_number} 页嵌入图像 {image_index + 1}",
                original_asset_id=asset.derived_from or asset.asset_id,
                metadata={
                    "needs_ocr": needs_ocr,
                    "extraction": "pdfium_image_stream",
                    "composed_page_preserved": False,
                },
            )
        )
        content.blocks.append(
            Block(
                block_id=ctx.identity("pdf", page.page_number, "image_block", image_index),
                type="figure",
                order=len(content.blocks),
                locators=[locator],
                figure_ids=[figure_id],
            )
        )
        figure_ids.append(figure_id)
    return figure_ids


def parse_pdf(ctx: ParseContext) -> ParsedContent:
    """Parse a PDF with physical 1-based page locators and bounded rendering."""
    if ctx.render_dpi <= 0 or ctx.max_pages <= 0 or ctx.max_pixels <= 0:
        raise DocQAError("INVALID_REQUEST", "PDF resource limits and render DPI must be positive")
    try:
        # Serialize the complete PDFium handle lifetime, including exception
        # cleanup. Never rely on finalizers that might execute in other threads.
        with (
            PDFIUM_LOCK,
            pdfium.PdfDocument(ctx.source) as rendered,
            pdfplumber.open(ctx.source) as pdf,
        ):
            _validate_document(ctx, rendered)
            if len(pdf.pages) != len(rendered):
                raise DocQAError("PARSE_CORRUPT", "PDF parsers disagree on physical page count")
            content = ParsedContent(
                page_count=len(rendered),
                metadata={
                    "parser": "pdfplumber+pdfium",
                    "reading_order": "geometric_lines_top_to_bottom_then_left_to_right",
                    "coordinate_origin": "rotated_page_top_left",
                    "pages": [],
                },
            )
            content.warnings.append(
                ParseWarning(
                    code="PDF_LAYOUT_HEURISTIC",
                    severity="info",
                    message="PDF uses geometric line order and ruled-table detection; complex multicolumn order and cross-page tables require review",
                )
            )
            for page_index, page in enumerate(pdf.pages):
                if not (
                    page.width > 0
                    and page.height > 0
                    and math.isfinite(page.width)
                    and math.isfinite(page.height)
                ):
                    raise DocQAError("PARSE_CORRUPT", "PDF page has invalid dimensions")
                with closing(rendered[page_index]) as geometry_page:
                    page = _visible_page(page, geometry_page, content)
                meaningful_text = _extract_native(ctx, page, content)
                image_count = len(page.images)
                vector_count = len(page.curves) + len(page.rects) + len(page.lines)
                needs_ocr = not meaningful_text
                # A blank native page contains nothing to OCR. Native punctuation
                # without letters/numbers remains a candidate for OCR review.
                has_visual_content = bool(image_count or vector_count)
                should_preserve_visual = has_visual_content or (needs_ocr and bool(page.chars))
                page_info = {
                    "page": page.page_number,
                    "width_points": float(page.width),
                    "height_points": float(page.height),
                    "rotation_degrees": int(page.rotation),
                    "has_native_text": meaningful_text,
                    "needs_ocr": needs_ocr and should_preserve_visual,
                    "image_count": image_count,
                    "vector_object_count": vector_count,
                    "full_page": False,
                    "figure_ids": [],
                    "original_images": [],
                }
                if should_preserve_visual:
                    with closing(rendered[page_index]) as render_page:
                        embedded = (
                            []
                            if ctx.render_pdf
                            else _embedded_images(ctx, render_page, page, content)
                        )
                        page_info["original_images"] = [
                            {
                                "asset_id": asset.asset_id,
                                "original_asset_id": asset.derived_from or asset.asset_id,
                                "image_index": index,
                                "bounds_bottom_left_points": bounds,
                            }
                            for asset, index, bounds in embedded
                        ]
                        if ctx.render_pdf:
                            page_info["figure_ids"] = _region_figures(
                                ctx, render_page, page, content, needs_ocr
                            )
                            page_info["full_page"] = any(
                                f.full_page
                                for f in content.figures
                                if f.figure_id in page_info["figure_ids"]
                            )
                            page_info["original_images"] = [
                                {"original_asset_id": f.original_asset_id, "figure_id": f.figure_id}
                                for f in content.figures
                                if f.figure_id in page_info["figure_ids"] and f.original_asset_id
                            ]
                        else:
                            page_info["figure_ids"] = _fallback_figures(
                                ctx, page, content, embedded, needs_ocr
                            )
                            content.warnings.append(
                                ParseWarning(
                                    code="PDF_RENDER_DISABLED",
                                    message=f"PDF page {page.page_number}: rendering was disabled; vector graphics, masks and composed visual context are not preserved",
                                )
                            )
                content.metadata["pages"].append(page_info)
                page.close()
            from docqa.doc2md.table_links import link_pdf_tables

            return link_pdf_tables(content)
    except DocQAError:
        raise
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise DocQAError(
            "PASSWORD_REQUIRED", "PDF is password protected or uses unsupported encryption"
        ) from exc
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
            raise DocQAError("PASSWORD_REQUIRED", "PDF requires a password") from exc
        raise DocQAError(
            "PARSE_CORRUPT", f"PDF could not be parsed ({type(exc).__name__})"
        ) from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise DocQAError("SOURCE_ACCESS_DENIED", "PDF source is missing or unreadable") from exc
    except Exception as exc:
        raise DocQAError("PARSE_CORRUPT", f"PDF extraction failed ({type(exc).__name__})") from exc


def render_pdf_pages(ctx: ParseContext) -> list[ImageAsset]:
    """Render every physical page, including native text pages, for Office previews.

    Returned list order is physical 1-based page order. The caller must mark the
    resulting positions as *derived* if this PDF was converted from an Office
    source; this helper does not invent Office-native page numbers.
    """
    if ctx.render_dpi <= 0 or ctx.max_pages <= 0 or ctx.max_pixels <= 0:
        raise DocQAError("INVALID_REQUEST", "PDF resource limits and render DPI must be positive")
    try:
        with PDFIUM_LOCK, pdfium.PdfDocument(ctx.source) as rendered:
            _validate_document(ctx, rendered)
            assets = []
            for page_index in range(len(rendered)):
                with closing(rendered[page_index]) as page:
                    assets.append(_render_asset(ctx, page, page_index + 1))
            return assets
    except DocQAError:
        raise
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
            raise DocQAError("PASSWORD_REQUIRED", "PDF requires a password") from exc
        raise DocQAError("PARSE_CORRUPT", f"PDF rendering failed ({type(exc).__name__})") from exc
    except (FileNotFoundError, PermissionError) as exc:
        raise DocQAError("SOURCE_ACCESS_DENIED", "PDF source is missing or unreadable") from exc
    except Exception as exc:
        raise DocQAError("PARSE_CORRUPT", f"PDF rendering failed ({type(exc).__name__})") from exc
