"""Conservative graphic-object regions, shared by PDF and Office-rendered pages.

Geometry is a detection hint, never a claim that all figures were recovered.
Coordinates remain in the visible, rotated pdfplumber page basis.
"""

import math
import re


def union(boxes):
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def graphic_regions(page, *, max_regions=40):
    """Group raster/vector primitives; ignore rules, page backgrounds and tiny icons."""
    bounds = page.bbox
    page_area = page.width * page.height
    seeds = []
    scan = False
    for kind, objects in (
        ("raster", page.images),
        ("vector", [*page.curves, *page.rects, *page.lines]),
    ):
        for obj in objects:
            b = (
                max(bounds[0], float(obj["x0"])),
                max(bounds[1], float(obj["top"])),
                min(bounds[2], float(obj["x1"])),
                min(bounds[3], float(obj["bottom"])),
            )
            if not all(math.isfinite(v) for v in b):
                continue
            coverage = area(b) / page_area
            if kind == "raster" and coverage > 0.85:
                scan = True
                continue
            if kind == "vector" and coverage > 0.85:
                continue
            # Running rules and glyph-sized outlines are not standalone figures.
            if max(b[2] - b[0], b[3] - b[1]) < 6:
                continue
            seeds.append({"box": b, "raster": kind == "raster", "count": 1})
    # Bound pathological vector files; warn at the caller rather than dropping silently.
    overflow = len(seeds) > 8000
    seeds = seeds[:8000]
    groups = []
    for seed in sorted(seeds, key=lambda s: (s["box"][1], s["box"][0])):
        while True:
            touched = []
            for index, g in enumerate(groups):
                a, b = g["box"], seed["box"]
                if a[0] <= b[2] + 9 and a[2] >= b[0] - 9 and a[1] <= b[3] + 9 and a[3] >= b[1] - 9:
                    touched.append(index)
            if not touched:
                break
            for index in reversed(touched):
                other = groups.pop(index)
                seed = {
                    "box": union([seed["box"], other["box"]]),
                    "raster": seed["raster"] or other["raster"],
                    "count": seed["count"] + other["count"],
                }
        groups.append(seed)
    selected = []
    chars = page.chars
    captions = []
    for line in page.extract_text_lines(return_chars=False):
        if re.match(r"^\s*(?:fig(?:ure)?\.?\s*\d|table\s*\d|图\s*\d|表\s*\d)", line["text"], re.I):
            captions.append(line)
    for group in groups:
        box = group["box"]
        w, h = box[2] - box[0], box[3] - box[1]
        if w < 28 or h < 20 or area(box) / page_area < 0.003:
            continue
        if area(box) / page_area > 0.90:
            continue
        related = [
            c
            for c in captions
            if c["x0"] < box[2] + 20
            and c["x1"] > box[0] - 20
            and ((0 <= c["top"] - box[3] < 40) or (0 <= box[1] - c["bottom"] < 25))
        ]
        caption = (
            min(related, key=lambda c: min(abs(c["top"] - box[3]), abs(c["bottom"] - box[1])))
            if related
            else None
        )
        # Include labels whose glyphs touch the object boundary; do not absorb prose lines.
        labels = [
            c
            for c in chars
            if c["x0"] >= box[0] - 12
            and c["x1"] <= box[2] + 12
            and c["top"] >= box[1] - 12
            and c["bottom"] <= box[3] + 12
        ]
        boxes = [box, *[(c["x0"], c["top"], c["x1"], c["bottom"]) for c in labels]]
        if caption:
            boxes.append((caption["x0"], caption["top"], caption["x1"], caption["bottom"]))
        b = union(boxes)
        b = (
            max(bounds[0], b[0] - 5),
            max(bounds[1], b[1] - 5),
            min(bounds[2], b[2] + 5),
            min(bounds[3], b[3] + 5),
        )
        selected.append(
            {
                "box": b,
                "caption": caption["text"] if caption else "",
                "raster": group["raster"],
                "native_text": "".join(c["text"] for c in labels),
                "primitive_count": group["count"],
            }
        )
    selected.sort(key=lambda r: (r["box"][1], r["box"][0]))
    return selected[:max_regions], scan, overflow or len(selected) > max_regions
