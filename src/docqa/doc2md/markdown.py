"""Deterministic UTF-8/LF Markdown projection and byte-bound source mapping."""

from __future__ import annotations

import html
import re
from hashlib import sha256
from urllib.parse import quote

from docqa.domain.documents import Block, DocumentIR


def escape_text(text: str) -> str:
    text = html.escape(text.replace("\r\n", "\n").replace("\r", "\n"), quote=False)
    text = re.sub(r"([\\`*_{}\[\]#!|~+\-])", r"\\\1", text)
    return re.sub(r"(?m)^(\s*\d+)([.)])", r"\1\\\2", text)


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(escape_text(cell).replace("\n", "<br>") for cell in row) + " |"
        for row in normalized
    ]
    lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _body(block: Block, ir: DocumentIR) -> str:
    if block.type == "heading":
        return "#" * (block.heading_level or 2) + " " + escape_text(block.text)
    if block.type == "table":
        prefix = ""
        if block.metadata.get("continuation_previous"):
            previous = block.metadata["continuation_previous"]
            prefix = (
                f"> 可能续接前页表格（版式规则关联，需核对）：[上一部分](#block-{previous})\n\n"
            )
        body = _table(block.table_rows) or escape_text(block.text)
        note = "表格首行用于 Markdown 展示，不代表已确认它是原文表头。"
        if block.metadata.get("cell_layout"):
            note += " 原表含单元格布局信息，完整合并关系请核对 IR 与原文。"
        return prefix + body + "\n\n> " + note
    if block.type == "figure":
        figures = {item.figure_id: item for item in ir.figures}
        assets = {item.asset_id: item for item in ir.assets}
        parts = []
        for figure_id in block.figure_ids:
            figure = figures[figure_id]
            if figure.metadata.get("role") == "page_ocr":
                parts.append(
                    f"正文 OCR 位置：{escape_text(figure.locator.display_label)}（整页扫描，仅作正文转录）"
                )
                # The OCR paragraph is rendered separately with its own source map.
                continue
            asset = assets[figure.asset_id]
            label = " ".join((figure.caption or figure.locator.display_label).splitlines())
            label = str(figure.metadata.get("trace_name", label))
            image_path = str(figure.metadata.get("trace_image_path", asset.relative_path))
            parts.append(f"![{escape_text(label)}]({quote(image_path, safe='/')})")
            parts.append(f"图位置：{escape_text(figure.locator.display_label)}")
            if not figure.analysis_required:
                parts.append("此图仅存档；可见区域以 Office 渲染视图为准。")
            if figure.ocr_text:
                parts.append("**OCR 转录（派生文字）**\n\n" + escape_text(figure.ocr_text))
            if figure.vision:
                vision = figure.vision
                parts.append("**VL 图片释义（模型派生，需与原图核对）**")
                parts.extend("- " + escape_text(item) for item in vision.observations)
                if vision.entities:
                    parts.append(
                        "可见节点：" + "；".join(escape_text(x.label) for x in vision.entities)
                    )
                labels = {node.id: node.label for node in vision.entities}
                for edge in vision.relations:
                    symbol = {"directed": "→", "undirected": "—", "unknown": "方向未知"}[
                        edge.direction
                    ]
                    parts.append(
                        "- "
                        + escape_text(
                            f"{labels[edge.source]} {symbol} {labels[edge.target]} {edge.label}"
                        )
                    )
                if vision.interpretations:
                    parts.append("**解释（不是逐字原文）**")
                    parts.extend("- " + escape_text(item) for item in vision.interpretations)
                if vision.uncertainties:
                    parts.append("**不确定项**")
                    parts.extend("- " + escape_text(item) for item in vision.uncertainties)
            elif figure.analysis_required:
                parts.append("图片尚无可用 VL 释义，不应视为完整图语义。")
        return "\n\n".join(parts)
    prefix = "- " if block.type == "list" else ""
    if block.origin == "ocr":
        prefix += "**OCR 转录：** "
    return prefix + escape_text(block.text)


def render_markdown(ir: DocumentIR, *, snapshot_link: str, status: str) -> tuple[bytes, dict]:
    chunks = [
        f"# {escape_text(ir.source_name)}\n\n",
        f"- 文档 ID：`{ir.doc_id}`\n- 内容版本：`{ir.revision_id}`\n",
        f"- 解析资产：`{ir.artifact_id}`\n- 解析状态：`{status}`\n",
        f"- 原文快照：[打开原文]({quote(snapshot_link.replace(chr(92), '/'), safe='/.:')})\n\n",
    ]
    if ir.warnings:
        chunks.append("## 解析提示\n\n")
        chunks.extend(f"- `{item.code}`：{escape_text(item.message)}\n" for item in ir.warnings)
        chunks.append("\n")
    current = bytearray("".join(chunks).encode("utf-8"))
    line_count = current.count(b"\n")
    entries = []
    for block in ir.blocks:
        anchor = f"block-{block.block_id}"
        content = f'<a id="{anchor}"></a>\n\n' + _body(block, ir) + "\n"
        payload = content.encode("utf-8")
        start = len(current)
        entries.append(
            {
                "block_id": block.block_id,
                "anchor": anchor,
                "start_byte": start,
                "end_byte": start + len(payload),
                "line_start": line_count + 1,
                "line_end": line_count + payload.count(b"\n"),
                "origin": block.origin,
                "text_start_codepoint": 0,
                "text_end_codepoint": len(block.text),
                "source_text_sha256": sha256(block.text.encode("utf-8")).hexdigest(),
                "locators": [locator.model_dump(mode="json") for locator in block.locators],
                "figure_ids": block.figure_ids,
            }
        )
        sources = "；".join(escape_text(x.display_label) for x in block.locators)
        suffix = f"\n> 来源：{sources}\n\n".encode("utf-8")
        current.extend(payload)
        current.extend(suffix)
        line_count += payload.count(b"\n") + suffix.count(b"\n")
    source_map = {
        "schema_version": "docqa.source-map.v1",
        "artifact_id": ir.artifact_id,
        "markdown_sha256": sha256(current).hexdigest(),
        "encoding": "UTF-8",
        "newline": "LF",
        "interval": "half-open UTF-8 byte offsets",
        "entries": entries,
    }
    return bytes(current), source_map
