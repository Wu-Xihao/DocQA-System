from __future__ import annotations

from hashlib import sha256

import pytest

from docqa.doc2md.markdown import render_markdown
from docqa.docbase.chunker import chunk_artifact, chunk_profile, figure_vl_text, resolve_span_text
from docqa.domain.documents import (
    Block,
    DocumentIR,
    FigureOccurrence,
    SourceLocator,
    VisionDescription,
    VisionEntity,
    VisionRelation,
)
from docqa.domain.errors import DocQAError
from docqa.domain.rag import ChunkOptions, chunk_identity


def locator(page=1):
    return SourceLocator(
        locator_type="pdf_page",
        page=page,
        precision="page_only",
        display_label=f"第 {page} 页",
        doc_id="doc-one",
        revision_id="revision-one",
        artifact_id="artifact-one",
    )


def block(text, *, block_id="b1", kind="paragraph", order=0, **values):
    return Block(
        block_id=block_id, type=kind, text=text, order=order, locators=[locator()], **values
    )


def ir_for(blocks, **values):
    return DocumentIR(
        doc_id="doc-one",
        revision_id="revision-one",
        artifact_id="artifact-one",
        content_sha256="1" * 64,
        parse_profile_id="profile-one",
        source_name="中文样例.pdf",
        source_format="pdf",
        category="技术",
        blocks=blocks,
        **values,
    )


def run(ir, options=None):
    _, source_map = render_markdown(ir, snapshot_link="input.pdf", status="complete")
    return chunk_artifact(ir, source_map, options or ChunkOptions())


def assert_resolvable(ir, chunks):
    for chunk in chunks:
        assert chunk.chunk_id == chunk_identity(chunk)
        for span in chunk.source_spans:
            assert (
                sha256(resolve_span_text(ir, span).encode("utf-8")).hexdigest()
                == span.source_text_sha256
            )


def test_short_paragraphs_merge_with_heading_without_losing_source():
    ir = ir_for(
        [
            block("标题", kind="heading", heading_level=1),
            block("段落一", block_id="b2", order=1),
            block("段落二", block_id="b3", order=2),
        ]
    )
    chunks = run(ir)
    assert len(chunks) == 1
    assert chunks[0].section == ["标题"]
    assert [span.block_id for span in chunks[0].source_spans] == ["b1", "b2", "b3"]
    assert chunks[0].generated_context[0].kind == "section"
    assert chunks[0].display_text.index("段落一") < chunks[0].display_text.index("段落二")
    assert_resolvable(ir, chunks)


def test_utf8_budget_overlap_and_full_coverage_are_exact_codepoints():
    text = ("中文😀 内容甲。 FE_HEART_Start 不得截断。\n" * 30) + "结束"
    ir = ir_for([block(text)])
    options = ChunkOptions(target_utf8_bytes=120, max_utf8_bytes=160, overlap_utf8_bytes=25)
    chunks = run(ir, options)
    coverage = set()
    for chunk in chunks:
        assert chunk.index_utf8_bytes == len(chunk.index_text.encode("utf-8")) <= 160
        for span in chunk.source_spans:
            coverage.update(range(span.text_start_codepoint, span.text_end_codepoint))
            assert not (
                "FE_" in resolve_span_text(ir, span)
                and "FE_HEART_Start" not in resolve_span_text(ir, span)
            )
    assert coverage == set(range(len(text)))
    assert any(
        left.source_spans[-1].text_end_codepoint > right.source_spans[0].text_start_codepoint
        for left, right in zip(chunks, chunks[1:])
    )
    assert_resolvable(ir, chunks)


def test_oversized_identifier_is_explicit_error_instead_of_truncation():
    with pytest.raises(DocQAError) as error:
        run(
            ir_for([block("FE_" + "A" * 200)]),
            ChunkOptions(target_utf8_bytes=64, max_utf8_bytes=80, overlap_utf8_bytes=0),
        )
    assert error.value.code == "CHUNK_IDENTIFIER_TOO_LONG"


def test_generated_section_context_counts_towards_hard_byte_limit():
    ir = ir_for([block("章节标题太长" * 20, kind="heading", heading_level=1)])
    with pytest.raises(DocQAError) as error:
        run(ir, ChunkOptions(target_utf8_bytes=80, max_utf8_bytes=100, overlap_utf8_bytes=0))
    assert error.value.code == "CHUNK_CONTEXT_TOO_LONG"


def test_new_section_does_not_merge_with_previous_section():
    ir = ir_for(
        [
            block("第一章", kind="heading", heading_level=1),
            block("A", block_id="b2", order=1),
            block("第二章", block_id="b3", kind="heading", heading_level=1, order=2),
            block("B", block_id="b4", order=3),
        ]
    )
    chunks = run(ir)
    assert len(chunks) == 2
    assert [chunk.section for chunk in chunks] == [["第一章"], ["第二章"]]
    assert chunks[0].source_spans[-1].block_id == "b2"


def test_small_table_remains_one_group_with_exact_row_ranges():
    ir = ir_for([block("", kind="table", table_rows=[["字段", "说明"], ["SD5993", "型号"]])])
    chunks = run(ir)
    assert len(chunks) == 1
    assert [resolve_span_text(ir, span) for span in chunks[0].source_spans] == [
        "字段 | 说明",
        "SD5993 | 型号",
    ]
    assert [(span.table_row_start, span.table_row_end) for span in chunks[0].source_spans] == [
        (0, 1),
        (1, 2),
    ]
    assert_resolvable(ir, chunks)


def test_long_table_repeats_unverified_header_only_as_generated_context():
    rows = [["编号", "内容"]] + [[f"R{row}", "数据说明" * 10] for row in range(12)]
    ir = ir_for([block("", kind="table", table_rows=rows)])
    chunks = run(ir, ChunkOptions(target_utf8_bytes=220, max_utf8_bytes=320, overlap_utf8_bytes=20))
    assert len(chunks) > 2
    assert any(
        context.kind == "table_header"
        for chunk in chunks[1:]
        for context in chunk.generated_context
    )
    assert all(chunk.index_utf8_bytes <= 320 for chunk in chunks)
    assert {span.table_row_start for chunk in chunks for span in chunk.source_spans} == set(
        range(len(rows))
    )
    assert sum(span.table_row_start == 0 for chunk in chunks for span in chunk.source_spans) == 1
    assert_resolvable(ir, chunks)


def test_very_long_cell_is_split_without_losing_original_row_text():
    text = "超长单元格，" * 100
    ir = ir_for([block("", kind="table", table_rows=[["字段", "内容"], ["A", text]])])
    chunks = run(ir, ChunkOptions(target_utf8_bytes=180, max_utf8_bytes=280, overlap_utf8_bytes=20))
    covered = set()
    for chunk in chunks:
        assert chunk.index_utf8_bytes <= 280
        for span in chunk.source_spans:
            if span.table_row_start == 1:
                covered.update(range(span.text_start_codepoint, span.text_end_codepoint))
    assert covered == set(range(len("A | " + text)))
    assert_resolvable(ir, chunks)


def test_figure_ocr_and_vl_remain_distinguishable_and_source_bound():
    vision = VisionDescription(
        status="partial",
        image_type="流程图",
        observations=["开始节点连接处理节点"],
        entities=[VisionEntity(id="A", label="开始"), VisionEntity(id="B", label="处理")],
        relations=[VisionRelation(source="A", target="B", direction="directed", label="下一步")],
        interpretations=["可能表示流水线"],
        uncertainties=["末端模糊"],
    )
    figure = FigureOccurrence(
        figure_id="f1",
        asset_id="asset-image",
        locator=locator(),
        caption="原生图注",
        ocr_text="START_ID_001",
        vision=vision,
        semantic_state="described",
    )
    ir = ir_for([block("", kind="figure", figure_ids=["f1"])], figures=[figure])
    # The chunker consumes verified IR and does not require reading image bytes.
    source_map = {
        "artifact_id": ir.artifact_id,
        "entries": [{"block_id": "b1", "start_byte": 10, "end_byte": 1000}],
    }
    chunks = chunk_artifact(ir, source_map, ChunkOptions())
    assert len(chunks) == 1 and chunks[0].figure_ids == ["f1"]
    assert {span.origin for span in chunks[0].source_spans} == {"ocr", "vl_generated"}
    assert "[OCR 派生]" in chunks[0].display_text and "[VL 派生]" in chunks[0].display_text
    assert "开始 → 处理" in figure_vl_text(figure) and "不确定项：末端模糊" in chunks[0].index_text
    assert "解释（非逐字原文）" in chunks[0].index_text
    assert chunks[0].generated_context[0].kind == "figure_caption"
    assert_resolvable(ir, chunks)


def test_no_semantic_figure_does_not_create_fake_status_knowledge():
    figure = FigureOccurrence(
        figure_id="f1",
        asset_id="image",
        locator=locator(),
        caption="只有图注",
        vision=VisionDescription(status="no_semantic_content"),
    )
    ir = ir_for([block("", kind="figure", figure_ids=["f1"])], figures=[figure])
    source_map = {
        "artifact_id": ir.artifact_id,
        "entries": [{"block_id": "b1", "start_byte": 1, "end_byte": 100}],
    }
    assert figure_vl_text(figure) == ""
    assert chunk_artifact(ir, source_map, ChunkOptions()) == []


def test_undirected_vl_relation_is_not_misrepresented_as_bidirectional():
    figure = FigureOccurrence(
        figure_id="f1",
        asset_id="image",
        locator=locator(),
        vision=VisionDescription(
            status="ok",
            entities=[VisionEntity(id="a", label="A"), VisionEntity(id="b", label="B")],
            relations=[VisionRelation(source="a", target="b", direction="undirected")],
        ),
    )
    assert "A —（无向） B" in figure_vl_text(figure)
    assert "↔" not in figure_vl_text(figure)


def test_identity_binds_document_revision_and_strategy():
    ir = ir_for([block("same content")])
    first = run(ir)[0]
    assert run(ir)[0].chunk_id == first.chunk_id
    assert run(ir.model_copy(update={"doc_id": "another-document"}))[0].chunk_id != first.chunk_id
    assert chunk_profile(ChunkOptions(overlap_utf8_bytes=0)) != chunk_profile(ChunkOptions())


def test_excluded_blocks_do_not_enter_index():
    ir = ir_for(
        [block("excluded", excluded_from_index=True), block("allowed", block_id="b2", order=1)]
    )
    chunks = run(ir)
    assert "excluded" not in chunks[0].index_text
    assert [span.block_id for span in chunks[0].source_spans] == ["b2"]


@pytest.mark.parametrize(
    "entries",
    [
        None,
        [{"block_id": "b1", "start_byte": 4, "end_byte": 4}],
        [{"block_id": "b2", "start_byte": 1, "end_byte": 2}],
        [{"block_id": "b1", "start_byte": 1, "end_byte": 2}] * 2,
    ],
)
def test_invalid_source_maps_fail_closed(entries):
    with pytest.raises(DocQAError) as error:
        chunk_artifact(
            ir_for([block("abc")]),
            {"artifact_id": "artifact-one", "entries": entries},
            ChunkOptions(),
        )
    assert error.value.code == "CHUNK_INVALID_SOURCE_MAP"


def test_span_resolution_rejects_component_range_forgery():
    ir = ir_for([block("truth")])
    chunk = run(ir)[0]
    with pytest.raises(DocQAError):
        resolve_span_text(ir, chunk.source_spans[0].model_copy(update={"text_end_codepoint": 100}))
    with pytest.raises(DocQAError):
        resolve_span_text(ir, chunk.source_spans[0].model_copy(update={"figure_id": "unrelated"}))
