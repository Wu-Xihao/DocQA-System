"""Local, source-bound keyword frequencies and deterministic collision-free packing."""

import io
import re
from collections import Counter
from math import cos, sin, sqrt
from pathlib import Path
from xml.etree import ElementTree as ET

from docqa.domain.errors import DocQAError

STOP = set(
    """的 了 和 与 及 或 而 是 在 对 将 由 为 于 中 上 下 等 这 那 这个 那个
这些 那些 其中 一个 一种 一些 可以 可能 进行 通过 使用 采用 实现 提供 包括 以及
根据 需要 能够 具有 因此 同时 主要 相关 之间 作为 已经 没有 不是 不能 本文 本次
我们 你们 他们 它们 什么 如何 说明 内容 资料 原文 来源 引用 回答 要点 例如 此外
the a an and or but of to in on at by for with from as is are was were be been being
it its this that these those which what when where how why who whom whose
not no yes can could may might will would shall should must do does did done
have has had having into over under between within without through than then
their they them we our us you your he she his her also such only each other
all any some both more most many much very about there here following using used
use based via etc i e s t www http https com docx pdf pptx""".split()
)
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z0-9]+)*|[\u3400-\u9fff]+")
COLORS = ["#2864d8", "#187868", "#7655b5", "#ad552a", "#276a8c", "#9c4166"]


def keywords(facts, limit=40):
    import jieba

    if sum(len(f["text"]) for f in facts) > 200_000:
        raise DocQAError("ARTIFACT_TOO_LARGE", "Keyword source exceeds the text budget")
    terms = {}
    for fact in facts:
        if not fact["sources"]:
            continue
        counts = Counter()
        labels = {}
        for match in TOKEN.finditer(fact["text"]):
            raw = match.group()
            parts = jieba.cut(raw, HMM=False) if "\u3400" <= raw[0] <= "\u9fff" else [raw]
            for part in parts:
                key = part.casefold()
                if (
                    not 2 <= len(part) <= 32
                    or key in STOP
                    or re.fullmatch(r"[a-fA-F0-9]{16,}", part)
                    or re.fullmatch(r"[CEQ]\d{3,}", part)
                ):
                    continue
                counts[key] += 1
                labels.setdefault(key, part)
        for key, count in counts.items():
            item = terms.setdefault(
                key, {"text": labels[key], "weight": 0, "claim_ids": [], "sources": []}
            )
            item["weight"] += count
            item["claim_ids"].append(fact["id"])
            item["sources"] = list(dict.fromkeys([*item["sources"], *fact["sources"]]))
    selected = sorted(terms.items(), key=lambda pair: (-pair[1]["weight"], pair[0]))[:limit]
    if not selected:
        raise DocQAError("ARTIFACT_KEYWORDS_MISSING", "No source-bound keywords were found")
    return [{"id": f"W{i + 1:03d}", **item} for i, (_, item) in enumerate(selected)]


def layout(words, width=1100, height=620):
    low, high = min(w["weight"] for w in words), max(w["weight"] for w in words)
    best = []
    for scale in (1, 0.88, 0.76):
        placed = []
        for index, word in enumerate(words):
            size = (
                36 if high == low else 24 + 52 * sqrt((word["weight"] - low) / (high - low))
            ) * scale
            units = sum(1 if ord(c) > 255 else 0.63 for c in word["text"])
            size = min(size, (width - 80) / max(units, 1))
            text_width = units * size
            bw, bh = text_width + 16, size * 1.35 + 12
            for step in range(4200):
                radius, angle = 5.5 * sqrt(step), step * 0.37
                x = width / 2 + cos(angle) * radius * 1.5
                y = height / 2 + sin(angle) * radius * 0.88
                box = (x - bw / 2, y - bh / 2, x + bw / 2, y + bh / 2)
                if box[0] < 24 or box[1] < 24 or box[2] > width - 24 or box[3] > height - 24:
                    continue
                if any(
                    box[0] < p["box"][2]
                    and box[2] > p["box"][0]
                    and box[1] < p["box"][3]
                    and box[3] > p["box"][1]
                    for p in placed
                ):
                    continue
                placed.append(
                    {
                        "id": word["id"],
                        "x": round(x, 2),
                        "y": round(y, 2),
                        "font_size": round(size, 2),
                        "text_width": round(text_width, 2),
                        "box": box,
                        "color": index % len(COLORS),
                    }
                )
                break
        if len(placed) > len(best):
            best = placed
        if len(best) == len(words):
            break
    return {
        "width": width,
        "height": height,
        "positions": best,
        "method": "term_frequency_spiral.v1",
    }


def build_wordcloud(facts):
    words = keywords(facts)
    return {
        "words": words,
        "wordcloud_layout": layout(words),
        "keyword_method": "checked_claim_term_frequency.v1",
    }


def export_wordcloud(artifact, format_name):
    content = artifact["content"]
    cloud = content["wordcloud_layout"]
    words = {w["id"]: w for w in content["words"]}
    width, height = cloud["width"], cloud["height"] + 90
    title = "DocQA · 词云" + (" · 演示成果" if content["synthetic"] else "")
    if format_name == "svg":
        svg = ET.Element(
            "svg",
            {
                "xmlns": "http://www.w3.org/2000/svg",
                "viewBox": f"0 0 {width} {height}",
                "width": str(width),
                "height": str(height),
                "role": "img",
            },
        )
        ET.SubElement(svg, "title").text = title
        ET.SubElement(svg, "rect", {"width": "100%", "height": "100%", "fill": "#ffffff"})
        ET.SubElement(
            svg, "text", {"x": "28", "y": "34", "font-size": "22", "fill": "#182d43"}
        ).text = title
        ET.SubElement(
            svg, "text", {"x": "28", "y": "61", "font-size": "15", "fill": "#5c7085"}
        ).text = "字号表示相对词频；仅统计当前已核验回答。"
        for pos in cloud["positions"]:
            word = words[pos["id"]]
            node = ET.SubElement(
                svg,
                "text",
                {
                    "x": str(pos["x"]),
                    "y": str(pos["y"] + 90),
                    "text-anchor": "middle",
                    "dominant-baseline": "central",
                    "font-weight": "600",
                    "font-family": "Microsoft YaHei, sans-serif",
                    "font-size": str(pos["font_size"]),
                    "textLength": str(pos["text_width"]),
                    "lengthAdjust": "spacingAndGlyphs",
                    "fill": COLORS[pos["color"]],
                },
            )
            node.text = word["text"]
            ET.SubElement(node, "title").text = f"出现 {word['weight']} 次；" + "、".join(
                word["sources"]
            )
        return ET.tostring(svg, encoding="utf-8", xml_declaration=True), "image/svg+xml"
    if format_name == "png":
        from PIL import Image, ImageDraw, ImageFont

        font_path = next(
            (
                p
                for p in (
                    Path("C:/Windows/Fonts/msyh.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                )
                if p.is_file()
            ),
            None,
        )

        def font(size):
            return (
                ImageFont.truetype(str(font_path), size)
                if font_path
                else ImageFont.load_default(size=size)
            )

        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        draw.text((28, 15), title, font=font(22), fill="#182d43")
        draw.text(
            (28, 49), "字号表示相对词频；仅统计当前已核验回答。", font=font(15), fill="#5c7085"
        )
        for pos in cloud["positions"]:
            word = words[pos["id"]]
            face = font(max(12, round(pos["font_size"])))
            bbox = face.getbbox(word["text"])
            glyph = Image.new(
                "RGBA", (max(1, bbox[2] - bbox[0] + 4), max(1, bbox[3] - bbox[1] + 4))
            )
            ImageDraw.Draw(glyph).text(
                (2 - bbox[0], 2 - bbox[1]), word["text"], font=face, fill=COLORS[pos["color"]]
            )
            glyph = glyph.resize(
                (max(1, round(pos["text_width"])), glyph.height), Image.Resampling.LANCZOS
            )
            image.paste(
                glyph,
                (round(pos["x"] - glyph.width / 2), round(pos["y"] + 90 - glyph.height / 2)),
                glyph,
            )
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue(), "image/png"
    raise DocQAError("EXPORT_UNSUPPORTED", "Unsupported wordcloud image format")
