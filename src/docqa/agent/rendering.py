"""Render system-owned citations and checked claims, with no raw model Markdown."""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import quote

from docqa.doc2md.markdown import escape_text
from docqa.domain.answers import AnswerResult
from docqa.domain.errors import DocQAError


def _cell(value):
    return escape_text(value).replace("\n", "<br>")


def _label(value):
    # Keep a small fixed Mermaid language: labels cannot introduce syntax/HTML.
    return (
        html.escape(value, quote=True)
        .replace("\n", " ")
        .replace("`", "&#96;")
        .replace("\\", "&#92;")
    )


def _plain_path(value):
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith("\\\\?\\") else value


def render_answer(result: AnswerResult, *, answer_dir: Path | None = None) -> str:
    by_claim = {}
    for citation in result.citations:
        expected = f"docqa://answers/{result.answer_id}/citations/{citation.citation_id}"
        if citation.source_ref != expected or not re.fullmatch(r"C[0-9]{4}", citation.citation_id):
            raise DocQAError("CITATION_INVALID", "Citation URI is not system-owned")
        for claim_id in citation.claim_ids:
            by_claim.setdefault(claim_id, []).append(
                f"[{citation.citation_id}](#{citation.citation_id.lower()})"
            )
    for claim in result.claims:
        if claim.claim_id not in by_claim:
            raise DocQAError("CITATION_INVALID", "Every displayed claim must have a citation")
    lines = [f"# 文档问答\n\n状态：`{result.status}`", f"问题：{escape_text(result.question)}"]
    if result.synthetic:
        lines.append("> 演示摘录：使用合成检索或检查机制，未经真实模型语义验收。")
    if result.degraded:
        lines.append("> 本次检索存在降级，缺失能力见诊断；不能据此推定未检索内容。")
    if result.requested_format == "table" and result.claims:
        lines.extend(["| 有据内容 | 类型 | 来源 |", "| --- | --- | --- |"])
        lines.extend(
            f"| {_cell(claim.text)} | {claim.kind} | {' '.join(by_claim[claim.claim_id])} |"
            for claim in result.claims
        )
    else:
        for claim in result.claims:
            prefix = "基于证据的归纳：" if claim.kind == "inference" else ""
            lines.append(
                prefix + escape_text(claim.text) + " " + " ".join(by_claim[claim.claim_id])
            )
    if result.requested_format == "mermaid":
        if result.diagram_edges:
            claim_ids = {claim.claim_id for claim in result.claims}
            checks = (
                {check.item_id: check for check in result.support_report.relation_checks}
                if result.support_report
                else {}
            )
            nodes, graph, notes = {}, ["flowchart TD"], []
            for edge in result.diagram_edges:
                if (
                    not set(edge.claim_ids).issubset(claim_ids)
                    or edge.edge_id not in checks
                    or checks[edge.edge_id].state != "supported"
                ):
                    raise DocQAError(
                        "DIAGRAM_INVALID", "Diagram relationship was not support-checked"
                    )
                if edge.direction == "unknown":
                    notes.append(
                        f"{escape_text(' '.join(edge.source_label.splitlines()))} 与 {escape_text(' '.join(edge.target_label.splitlines()))}：方向未知，未生成确定箭头。"
                    )
                    continue
                for label in (edge.source_label, edge.target_label):
                    if label not in nodes:
                        nodes[label] = f"N{len(nodes) + 1}"
                        graph.append(f'  {nodes[label]}["{_label(label)}"]')
                arrow = "-->" if edge.direction == "directed" else "---"
                graph.append(f"  {nodes[edge.source_label]} {arrow} {nodes[edge.target_label]}")
                notes.append(
                    "图关系依据："
                    + " ".join(ref for cid in edge.claim_ids for ref in by_claim[cid])
                )
            if len(graph) > 1:
                lines.extend(
                    ["依据已核对的证据整理：", "```mermaid\n" + "\n".join(graph) + "\n```"]
                )
            lines.extend(notes)
        else:
            lines.append("未获得可验证的图关系，保留上述文字内容与来源。")
    for conflict in result.conflicts:
        refs = by_claim.get(conflict.conflict_id)
        if not refs:
            raise DocQAError("CITATION_INVALID", "Conflict explanation requires citations")
        lines.append("来源冲突：" + escape_text(conflict.description) + " " + " ".join(refs))
    if result.unanswered_topics:
        descriptions = {
            item["topic_id"]: item["text"]
            for event in result.trace
            if event.get("stage") == "plan"
            for item in event.get("topics", [])
        }
        lines.append(
            "尚未解决："
            + "；".join(
                escape_text(descriptions.get(topic, topic)) for topic in result.unanswered_topics
            )
        )
    if result.status == "insufficient_evidence":
        lines.append("当前指定范围内没有足够的可验证证据，无法给出确定结论。")
    if result.status == "failed":
        lines.append(
            "本次处理发生故障，未发布未经校验的回答。原因：" + escape_text(result.stop_reason)
        )
    lines.append("## 来源")
    for citation in result.citations:
        origin = {"native": "原生摘录", "ocr": "OCR 转录", "vl_generated": "图片释义（模型派生）"}[
            citation.origin
        ]
        locations = "；".join(locator.display_label for locator in citation.locators)
        lines.append(
            f'<a id="{citation.citation_id.lower()}"></a>\n\n**{citation.citation_id}** {escape_text(citation.source_name)} · {origin} · {escape_text(locations)}\n\n> {escape_text(citation.quote[:200]).replace(chr(10), chr(10) + "> ")}'
        )
        if len(citation.quote) > 200:
            lines.append("引文预览仅展示前 200 字符；完整引文见 answer.json 或 source resolve。")
        lines.append(f"系统来源标识：`{citation.source_ref}`")
        if answer_dir is not None:
            links = []
            for label, path in [
                ("Markdown 原文", citation.markdown_path),
                ("原始文档快照", citation.snapshot_path),
                *[("原图 / 可见图", path) for path in citation.image_paths],
            ]:
                if path:
                    try:
                        relative = os.path.relpath(
                            _plain_path(path), _plain_path(str(answer_dir))
                        ).replace("\\", "/")
                        target = quote(relative, safe="/")
                    except ValueError:
                        # Windows cannot relativize paths across drive letters.
                        target = Path(_plain_path(path)).as_uri()
                    links.append(f"[{label}]({target})")
            lines.append(" · ".join(links))
    if not result.citations:
        lines.append("无已发布引用。")
    lines.append(f"回答 ID：`{result.answer_id}` · 索引代次：`{result.generation_id}`")
    lines.append(f"停止原因：`{result.stop_reason}` · 校验：`{result.verification}`")
    if result.warnings:
        lines.append("诊断：" + "；".join(escape_text(warning) for warning in result.warnings))
    return "\n\n".join(lines) + "\n"
