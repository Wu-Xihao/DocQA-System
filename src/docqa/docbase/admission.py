"""Explicit admission of intact, incomplete artifacts without changing their quality."""

from docqa.domain.documents import DocumentIR
from docqa.domain.errors import DocQAError

COVERAGE_WARNING = (
    "SOURCE_COVERAGE_PARTIAL: 部分来源仍有解析缺口；仅使用通过当前准入规则的内容，"
    "未解析或不确定的部分不作为回答证据。"
)


def source_admission(ir: DocumentIR, status: str, policy: str) -> dict | None:
    """Caller must verify the immutable artifact before applying this policy.

    Integrity errors never qualify. Native text with unresolved revisions or
    error-severity diagnostics is also excluded. Native-only mode blocks image-only
    sources; validated-parts requires at least one usable component.
    A native origin describes provenance, not guaranteed semantic correctness.
    """
    if policy not in {"reject", "native_only", "validated_parts"}:
        raise DocQAError("INVALID_CONFIG", "Unsupported incomplete-source policy")
    if status == "complete":
        return None
    if (
        policy == "reject"
        or status not in {"review_required", "partial"}
        or any(w.severity == "error" or w.code == "REVISIONS_UNRESOLVED" for w in ir.warnings)
    ):
        raise DocQAError("SOURCE_NOT_READY", "Source quality does not satisfy the admission policy")
    projection = validated_projection(ir) if policy == "validated_parts" else native_projection(ir)
    if not projection.blocks:
        raise DocQAError("NO_INDEXABLE_CONTENT", "Incomplete source has no admissible content")
    return {
        "status": status,
        "index_mode": policy,
        "warning_codes": sorted({w.code for w in ir.warnings}),
    }


def native_block(block) -> bool:
    return (
        not block.excluded_from_index
        and block.origin == "native"
        and block.type != "figure"
        and bool(
            block.text.strip() or any(any(cell.strip() for cell in row) for row in block.table_rows)
        )
    )


def has_coverage_gaps(member: dict) -> bool:
    return (member.get("source_quality") or {}).get("index_mode") in {
        "native_only",
        "validated_parts",
    }


def native_projection(ir: DocumentIR) -> DocumentIR:
    """Ephemeral indexing view; original IR, Markdown and identities stay intact."""
    return ir.model_copy(
        update={
            "blocks": [
                block.model_copy(update={"figure_ids": []})
                for block in ir.blocks
                if native_block(block)
            ],
            "figures": [],
        }
    )


def validated_projection(ir: DocumentIR) -> DocumentIR:
    """Admit successful figure components, never promote whole-document quality.

    All retained component strings are unchanged, preserving exact source spans.
    Archived/transformed originals and ambiguous/partial VL remain excluded.
    """
    figures = []
    for figure in ir.figures:
        if not figure.analysis_required:
            continue
        vision = figure.vision
        good_vl = (
            figure.semantic_state == "described"
            and vision is not None
            and vision.status in {"ok", "no_semantic_content"}
            and not vision.uncertainties
            and not any(r.direction == "unknown" for r in vision.relations)
        )
        good_ocr = bool(figure.ocr_text.strip()) and (
            good_vl or (figure.semantic_state == "ocr_only" and vision is None)
        )
        if good_vl or good_ocr:
            figures.append(
                figure.model_copy(
                    update={
                        "vision": vision if good_vl else None,
                        "ocr_text": figure.ocr_text if good_ocr else "",
                    }
                )
            )
    ids = {f.figure_id for f in figures}
    blocks = []
    for block in ir.blocks:
        kept = [f for f in block.figure_ids if f in ids]
        # Native figure placeholders carry components, not their unverified text.
        if native_block(block) or (
            block.type == "figure" and kept and not block.excluded_from_index
        ):
            blocks.append(block.model_copy(update={"figure_ids": kept}))
    return ir.model_copy(update={"blocks": blocks, "figures": figures})


def index_projection(ir: DocumentIR, member: dict) -> DocumentIR:
    mode = (member.get("source_quality") or {}).get("index_mode")
    if mode == "validated_parts":
        return validated_projection(ir)
    return native_projection(ir) if mode == "native_only" else ir
