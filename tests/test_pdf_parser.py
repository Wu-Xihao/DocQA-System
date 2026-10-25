"""Real PDF fixtures generated without optional document creation packages."""

from hashlib import md5
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
import pytest
from PIL import Image, ImageDraw

from docqa.doc2md.common import ParseContext
from docqa.doc2md.parsers.pdf import parse_pdf, render_pdf_pages
from docqa.domain.errors import DocQAError


def _write_pdf(
    path: Path,
    streams: list[bytes],
    *,
    rotate=0,
    width=300,
    height=300,
    password=None,
    cropbox=None,
):
    """Build valid, uncompressed PDF pages with a standard Helvetica font."""
    count = len(streams)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Count {count} /Kids ["
            + " ".join(f"{4 + i * 2} 0 R" for i in range(count))
            + "] >>"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    file_id = b"docqa-pdf-test!!"
    encryption = None
    if password is not None:
        # Standard security handler revision 2. This fixture uses an actual PDF
        # password, without requiring pypdf or a system PDF encryption utility.
        padding = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")
        padded_user = (password.encode() + padding)[:32]
        padded_owner = (b"fixture-owner" + padding)[:32]
        owner = _rc4(md5(padded_owner).digest()[:5], padded_user)
        encryption_key = md5(
            padded_user + owner + (-4).to_bytes(4, "little", signed=True) + file_id
        ).digest()[:5]
        user = _rc4(encryption_key, padding)
        encryption = f"<< /Filter /Standard /V 1 /R 2 /Length 40 /O <{owner.hex()}> /U <{user.hex()}> /P -4 >>".encode()
    for i, stream in enumerate(streams):
        if encryption:
            object_key = encryption_key + (5 + i * 2).to_bytes(3, "little") + b"\0\0"
            stream = _rc4(md5(object_key).digest()[:10], stream)
        crop = " /CropBox [" + " ".join(str(v) for v in cropbox) + "]" if cropbox else ""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]{crop} /Rotate {rotate} /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + i * 2} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    if encryption:
        objects.append(encryption)
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    trailer_extra = (
        f" /Encrypt {len(objects)} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]"
        if encryption
        else ""
    )
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R{trailer_extra} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def _rc4(key, data):
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    result = bytearray()
    i = j = 0
    for value in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        result.append(value ^ state[(state[i] + state[j]) % 256])
    return bytes(result)


def _ctx(tmp_path, source, **kwargs):
    return ParseContext(
        source=source,
        work_dir=tmp_path / "artifact",
        doc_id="doc-test",
        revision_id="rev-test",
        source_name=source.name,
        **kwargs,
    )


def test_pdf_native_text_pages_and_normalized_coordinates(tmp_path):
    path = tmp_path / "native.pdf"
    _write_pdf(
        path,
        [
            b"BT /F1 12 Tf 20 250 Td (Native PDF first line) Tj 0 -30 Td (Second paragraph) Tj ET",
            b"BT /F1 12 Tf 20 250 Td (Physical page two) Tj ET",
        ],
    )
    result = parse_pdf(_ctx(tmp_path, path))
    assert result.page_count == 2
    assert [block.text for block in result.blocks] == [
        "Native PDF first line",
        "Second paragraph",
        "Physical page two",
    ]
    assert [block.order for block in result.blocks] == list(range(3))
    assert [block.locators[0].page for block in result.blocks] == [1, 1, 2]
    assert not result.figures
    assert not result.assets
    assert not any(page["needs_ocr"] for page in result.metadata["pages"])
    for block in result.blocks:
        locator = block.locators[0]
        assert locator.precision == "exact_region"
        assert all(0 <= value <= 1 for value in locator.bbox)
        assert locator.native_ref["bbox_points"]
        assert locator.doc_id == "doc-test"


def test_pdf_ruled_table_is_not_duplicated_in_paragraphs(tmp_path):
    path = tmp_path / "table.pdf"
    _write_pdf(
        path,
        [
            b"""
        1 w 20 100 m 220 100 l S 20 150 m 220 150 l S 20 200 m 220 200 l S
        20 100 m 20 200 l S 120 100 m 120 200 l S 220 100 m 220 200 l S
        BT /F1 12 Tf 30 175 Td (Name) Tj 100 0 Td (Value) Tj ET
        BT /F1 12 Tf 30 125 Td (Alpha) Tj 100 0 Td (42) Tj ET
        BT /F1 12 Tf 20 250 Td (Table introduction) Tj ET
    """
        ],
    )
    result = parse_pdf(_ctx(tmp_path, path))
    tables = [block for block in result.blocks if block.type == "table"]
    assert len(tables) == 1
    assert tables[0].table_rows == [["Name", "Value"], ["Alpha", "42"]]
    paragraphs = " ".join(block.text for block in result.blocks if block.type == "paragraph")
    assert paragraphs == "Table introduction"
    assert "Alpha" not in paragraphs
    assert result.figures[0].full_page
    assert not result.figures[0].metadata["needs_ocr"]
    assert [block.order for block in result.blocks] == list(range(len(result.blocks)))


def test_pdf_scanned_page_preserves_page_and_original_bitmap(tmp_path):
    path = tmp_path / "scan.pdf"
    image = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(image).text((20, 20), "Scanned bitmap text 123", fill="black")
    image.save(path, "PDF", resolution=72)
    result = parse_pdf(_ctx(tmp_path, path))
    assert result.page_count == 1
    assert len(result.figures) == 1
    figure = result.figures[0]
    assert figure.full_page
    assert figure.metadata["needs_ocr"]
    assert figure.original_asset_id
    assert figure.locator.page == 1
    assert figure.locator.bbox == (0, 0, 1, 1)
    assert figure.semantic_state == "pending"
    assert figure.ocr_text == ""
    assert result.metadata["pages"][0]["needs_ocr"]
    assert result.metadata["pages"][0]["original_images"]
    for asset in result.assets:
        assert (tmp_path / "artifact" / asset.relative_path).is_file()


def test_pdf_vectors_survive_even_without_embedded_images(tmp_path):
    path = tmp_path / "vector.pdf"
    _write_pdf(path, [b"20 150 80 40 re S 180 150 80 40 re S 100 170 m 180 170 l S"])
    result = parse_pdf(_ctx(tmp_path, path))
    assert not result.metadata["pages"][0]["image_count"]
    assert result.metadata["pages"][0]["vector_object_count"] > 0
    assert len(result.figures) == 1
    assert result.figures[0].full_page
    assert result.figures[0].metadata["needs_ocr"]
    assert result.figures[0].original_asset_id is None


def test_pdf_rotation_is_recorded_and_coordinates_remain_valid(tmp_path):
    path = tmp_path / "rotated.pdf"
    _write_pdf(
        path, [b"BT /F1 12 Tf 20 220 Td (Rotated text) Tj ET"], rotate=90, width=300, height=250
    )
    result = parse_pdf(_ctx(tmp_path, path))
    assert result.metadata["pages"][0]["rotation_degrees"] == 90
    assert result.metadata["pages"][0]["width_points"] == 250
    for block in result.blocks:
        assert block.locators[0].native_ref["rotation_degrees"] == 90
        assert all(0 <= value <= 1 for value in block.locators[0].bbox)


def test_pdf_mixed_text_directions_keep_logical_word_order(tmp_path):
    path = tmp_path / "mixed-directions.pdf"
    _write_pdf(
        path,
        [
            b"BT /F1 10 Tf 20 220 Td (FORWARD ONE) Tj ET "
            b"BT /F1 10 Tf -1 0 0 -1 250 180 Tm (UPRIGHT TWO) Tj ET"
        ],
        rotate=180,
    )
    result = parse_pdf(_ctx(tmp_path, path))
    assert {block.text for block in result.blocks} == {"FORWARD ONE", "UPRIGHT TWO"}


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_rotated_table_cells_preserve_logical_text(tmp_path, rotation):
    path = tmp_path / "table-directions.pdf"
    _write_pdf(
        path,
        [
            b"20 100 200 100 re S 120 100 m 120 200 l S "
            b"BT /F1 10 Tf 30 150 Td (CELL-ONE) Tj 100 0 Td (CELL-TWO) Tj ET"
        ],
        rotate=rotation,
    )
    result = parse_pdf(_ctx(tmp_path, path))
    cells = [
        cell
        for block in result.blocks
        if block.type == "table"
        for row in block.table_rows
        for cell in row
    ]
    assert sorted(cells) == ["CELL-ONE", "CELL-TWO"]


def test_pdf_render_disabled_preserves_bitmap_with_explicit_warning(tmp_path):
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (150, 100), "white").save(path, "PDF", resolution=72)
    result = parse_pdf(_ctx(tmp_path, path, render_pdf=False))
    assert result.figures
    assert not result.figures[0].full_page
    assert result.figures[0].locator.precision == "page_only"
    assert any(w.code == "PDF_RENDER_DISABLED" for w in result.warnings)


def test_pdf_page_limit_is_enforced(tmp_path):
    path = tmp_path / "pages.pdf"
    _write_pdf(path, [b"", b""])
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path, max_pages=1))
    assert caught.value.code == "RESOURCE_LIMIT_EXCEEDED"


def test_pdf_render_pixel_limit_is_checked_before_rendering(tmp_path):
    path = tmp_path / "large.pdf"
    _write_pdf(path, [b"20 20 80 40 re S"], width=1000, height=1000)
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path, max_pixels=1000))
    assert caught.value.code == "RESOURCE_LIMIT_EXCEEDED"


def test_pdf_embedded_pixel_limit_is_enforced(tmp_path):
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (150, 100), "white").save(path, "PDF", resolution=72)
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path, max_pixels=1000))
    assert caught.value.code == "RESOURCE_LIMIT_EXCEEDED"


def test_pdf_corruption_reports_structured_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a valid PDF\n%%EOF")
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path))
    assert caught.value.code == "PARSE_CORRUPT"


def test_pdf_password_error_is_not_reported_as_corruption(tmp_path, monkeypatch):
    def locked(*args, **kwargs):
        raise pdfium.PdfiumError("Incorrect password", err_code=pdfium_raw.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(pdfium, "PdfDocument", locked)
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, tmp_path / "protected.pdf"))
    assert caught.value.code == "PASSWORD_REQUIRED"


def test_pdf_actual_password_protected_file_is_rejected(tmp_path):
    path = tmp_path / "actual-password.pdf"
    _write_pdf(path, [b"BT /F1 12 Tf 20 250 Td (Protected data) Tj ET"], password="secret123")
    with pdfium.PdfDocument(path, password="secret123") as unlocked:
        assert len(unlocked) == 1
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path))
    assert caught.value.code == "PASSWORD_REQUIRED"


def test_pdf_empty_user_password_encryption_is_also_rejected(tmp_path):
    path = tmp_path / "owner-encrypted.pdf"
    _write_pdf(path, [b""], password="")
    with pytest.raises(DocQAError) as caught:
        parse_pdf(_ctx(tmp_path, path))
    assert caught.value.code == "PASSWORD_REQUIRED"


def test_pdf_empty_page_is_not_mislabeled_as_ocr_candidate(tmp_path):
    path = tmp_path / "blank.pdf"
    _write_pdf(path, [b""])
    result = parse_pdf(_ctx(tmp_path, path))
    assert result.page_count == 1
    assert result.blocks == []
    assert result.figures == []
    assert result.metadata["pages"][0]["needs_ocr"] is False


def test_pdf_ids_are_stable_across_runs(tmp_path):
    path = tmp_path / "stable.pdf"
    _write_pdf(path, [b"BT /F1 12 Tf 20 250 Td (Stable IDs) Tj ET 20 20 80 40 re S"])
    first = parse_pdf(_ctx(tmp_path, path))
    second = parse_pdf(_ctx(tmp_path, path))
    assert first.model_dump() == second.model_dump()


def test_pdf_office_helper_renders_all_native_pages_in_order(tmp_path):
    path = tmp_path / "office-render.pdf"
    _write_pdf(
        path,
        [
            b"BT /F1 12 Tf 20 250 Td (Converted Office page one) Tj ET",
            b"BT /F1 12 Tf 20 250 Td (Converted Office page two) Tj ET",
        ],
    )
    ctx = _ctx(tmp_path, path)
    assets = render_pdf_pages(ctx)
    assert len(assets) == 2
    assert assets[0].asset_id != assets[1].asset_id
    for page, asset in enumerate(assets, 1):
        assert f"Page{page}_FullPage" in asset.relative_path
        assert asset.width == asset.height == 600
        assert (ctx.work_dir / asset.relative_path).is_file()


def test_pdf_source_is_released_after_parse(tmp_path):
    path = tmp_path / "release.pdf"
    _write_pdf(path, [b"20 20 80 40 re S"])
    parse_pdf(_ctx(tmp_path, path))
    # Windows will reject this rename if an open parser handle retained a lock.
    renamed = path.with_name("renamed.pdf")
    path.rename(renamed)
    assert renamed.is_file()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_crop_excludes_hidden_text_and_uses_visible_coordinate_basis(tmp_path, rotation):
    path = tmp_path / "crop.pdf"
    _write_pdf(
        path,
        [
            b"BT /F1 8 Tf 110 150 Td (VISIBLE) Tj ET "
            b"BT /F1 8 Tf 10 280 Td (HIDDEN-OUTSIDE-CROP) Tj ET "
            b"110 120 50 30 re S"
        ],
        rotate=rotation,
        cropbox=(100, 100, 210, 200),
    )
    result = parse_pdf(_ctx(tmp_path, path, render_dpi=72))
    text = " ".join(block.text for block in result.blocks)
    assert "VISIBLE" in text and "HIDDEN" not in text
    figure = result.figures[0]
    asset = next(asset for asset in result.assets if asset.asset_id == figure.asset_id)
    expected = (110, 100) if rotation in {0, 180} else (100, 110)
    assert (asset.width, asset.height) == expected
    assert figure.locator.bbox == (0.0, 0.0, 1.0, 1.0)
    for block in result.blocks:
        assert (
            block.locators[0].native_ref["normalized_coordinate_basis"] == "visible_rotated_cropbox"
        )
