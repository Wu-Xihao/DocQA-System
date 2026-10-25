"""Source-bound evidence group evaluation; retrieval never claims to answer questions."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, model_validator

from docqa.docbase.config import DocBaseOptions
from docqa.docbase.retrieval import MODES, generation_directory, query
from docqa.docbase.storage import load_generation
from docqa.domain.documents import Contract
from docqa.domain.errors import DocQAError


class EvidenceTarget(Contract):
    doc_id: str
    revision_id: str
    block_id: str
    component: Literal["block_text", "table_row", "figure_ocr", "figure_vl"] = "block_text"
    figure_id: str | None = None
    table_row_start: int | None = Field(default=None, ge=0)
    table_row_end: int | None = Field(default=None, ge=1)
    text_start_codepoint: int = Field(ge=0)
    text_end_codepoint: int = Field(ge=1)

    @model_validator(mode="after")
    def check_interval(self):
        if self.text_start_codepoint >= self.text_end_codepoint:
            raise ValueError("Evidence range must be nonempty")
        if self.component == "table_row":
            if (
                self.table_row_start is None
                or self.table_row_end != self.table_row_start + 1
                or self.figure_id is not None
            ):
                raise ValueError("Table target requires one original row")
        elif self.table_row_start is not None or self.table_row_end is not None:
            raise ValueError("Non-table target cannot name a row")
        if self.component in {"figure_ocr", "figure_vl"}:
            if not self.figure_id:
                raise ValueError("Figure target requires figure_id")
        elif self.figure_id is not None:
            raise ValueError("Non-figure target cannot name a figure")
        return self


class EvaluationCase(Contract):
    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answerable: bool
    categories: list[str] | None = None
    doc_ids: list[str] | None = None
    # Each inner list holds alternative sources for ONE required fact.
    evidence_groups: list[list[EvidenceTarget]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_groups(self):
        if self.answerable != bool(self.evidence_groups) or any(
            not group for group in self.evidence_groups
        ):
            raise ValueError(
                "Answerable cases require nonempty evidence groups; unanswerable cases require none"
            )
        return self


def _matching_ranks(hits, group):
    for hit in hits:
        for span in hit.source_spans:
            for target in group:
                if (hit.doc_id, span.locators[0].revision_id, span.block_id, span.component) != (
                    target.doc_id,
                    target.revision_id,
                    target.block_id,
                    target.component,
                ):
                    continue
                if (span.figure_id, span.table_row_start, span.table_row_end) != (
                    target.figure_id,
                    target.table_row_start,
                    target.table_row_end,
                ):
                    continue
                # A tiny overlap does not prove the annotated evidence was retrieved.
                if (
                    span.text_start_codepoint <= target.text_start_codepoint
                    and span.text_end_codepoint >= target.text_end_codepoint
                ):
                    yield hit.final_rank


def metrics_for_hits(hits, evidence_groups) -> dict:
    if not evidence_groups:
        raise ValueError("Recall metrics require nonempty evidence groups")
    ranks = [min(_matching_ranks(hits, group), default=None) for group in evidence_groups]
    found = [value for value in ranks if value is not None]
    return {
        "recall_at_k": len(found) / len(ranks),
        "hit_rate_at_k": float(bool(found)),
        "mrr_at_k": 1 / min(found) if found else 0.0,
        "complete_coverage_at_k": float(len(found) == len(ranks)),
    }


def eval_retrieval(
    index: str | Path,
    test_set: str | Path,
    *,
    options: DocBaseOptions | None = None,
    modes: tuple[str, ...] = ("dense", "bm25", "sparse", "rrf", "hybrid"),
    top_k: int | None = None,
    generation_id: str | None = None,
    providers=None,
) -> dict:
    """Pin one generation for every case/mode; report annotated coverage, not answer accuracy."""
    try:
        path = Path(test_set)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Evaluation set too large")
        dataset = json.loads(path.read_bytes())
        if dataset["schema_version"] != "docqa.docbase-eval.v1":
            raise ValueError("Unsupported evaluation set")
        cases = [EvaluationCase.model_validate(item) for item in dataset["cases"]]
        if not cases or len({case.question_id for case in cases}) != len(cases):
            raise ValueError("Empty or duplicate evaluation cases")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DocQAError("INVALID_EVALUATION_SET", "Invalid source-bound evaluation JSON") from exc
    options = options or DocBaseOptions()
    if not modes or len(set(modes)) != len(modes) or not set(modes).issubset(MODES):
        raise DocQAError("INVALID_REQUEST", "Evaluation modes must be unique known modes")
    directory = generation_directory(index, generation_id)
    with load_generation(directory) as generation:
        pinned_id = generation.manifest["generation_id"]
        # Reject stale ground truth rather than report a misleading zero recall.
        for case in cases:
            for group in case.evidence_groups:
                for target in group:
                    found = any(
                        chunk.doc_id == target.doc_id
                        and chunk.revision_id == target.revision_id
                        and any(
                            span.block_id == target.block_id
                            and span.component == target.component
                            and (span.figure_id, span.table_row_start, span.table_row_end)
                            == (target.figure_id, target.table_row_start, target.table_row_end)
                            and span.text_start_codepoint <= target.text_start_codepoint
                            and span.text_end_codepoint >= target.text_end_codepoint
                            for span in chunk.source_spans
                        )
                        for chunk in generation.chunks
                    )
                    if not found:
                        raise DocQAError(
                            "EVALUATION_SOURCE_MISMATCH",
                            "Evidence target is stale, absent or exceeds a source component window",
                        )
                # Alternatives can be filtered out individually; at least one
                # complete alternative must remain reachable for every group.
                if not any(
                    (case.categories is None or chunk.category in case.categories)
                    and (case.doc_ids is None or chunk.doc_id in case.doc_ids)
                    and bool(
                        list(
                            _matching_ranks(
                                [
                                    SimpleNamespace(
                                        doc_id=chunk.doc_id,
                                        source_spans=chunk.source_spans,
                                        final_rank=1,
                                    )
                                ],
                                group,
                            )
                        )
                    )
                    for chunk in generation.chunks
                ):
                    raise DocQAError(
                        "INVALID_EVALUATION_SET",
                        "Every alternative for a required evidence group is excluded by the hard filter",
                    )
        synthetic = generation.manifest["synthetic"]
    summaries, details = {}, []
    for mode in modes:
        measurements, negative_hits, failures = [], [], []
        for case in cases:
            try:
                result = query(
                    directory,
                    case.question,
                    options=options,
                    categories=case.categories,
                    doc_ids=case.doc_ids,
                    mode=mode,
                    top_k=top_k,
                    providers=providers,
                )
                metrics = (
                    metrics_for_hits(result.hits, case.evidence_groups) if case.answerable else None
                )
                if case.answerable:
                    measurements.append(metrics)
                else:
                    negative_hits.append(bool(result.hits))
                details.append(
                    {
                        "mode": mode,
                        "question_id": case.question_id,
                        "status": result.status,
                        "metrics": metrics,
                        "returned_hits": len(result.hits),
                        "warnings": result.warnings,
                    }
                )
            except DocQAError as exc:
                failures.append({"question_id": case.question_id, "code": exc.code})
                # Failures count as zero for answerable recall; they never disappear from denominators.
                if case.answerable:
                    measurements.append(
                        dict.fromkeys(
                            ("recall_at_k", "hit_rate_at_k", "mrr_at_k", "complete_coverage_at_k"),
                            0.0,
                        )
                    )
                details.append(
                    {"mode": mode, "question_id": case.question_id, "error": exc.as_dict()}
                )
        summaries[mode] = {
            key: math.fsum(value[key] for value in measurements) / len(measurements)
            if measurements
            else None
            for key in ("recall_at_k", "hit_rate_at_k", "mrr_at_k", "complete_coverage_at_k")
        }
        summaries[mode].update(
            {
                "answerable_cases": len(measurements),
                "unanswerable_completed": len(negative_hits),
                "unanswerable_cases": sum(not case.answerable for case in cases),
                "unanswerable_failures": sum(
                    not next(
                        case.answerable
                        for case in cases
                        if case.question_id == failure["question_id"]
                    )
                    for failure in failures
                ),
                "unanswerable_with_hits_rate": sum(negative_hits) / len(negative_hits)
                if negative_hits
                else None,
                "failures": failures,
            }
        )
    return {
        "schema_version": "docqa.docbase-eval-report.v1",
        "generation_id": pinned_id,
        "synthetic": synthetic,
        "quality_claim": "annotated evidence coverage only; synthetic results are mechanics tests",
        "cases": len(cases),
        "top_k": top_k or options.top_k,
        "summaries": summaries,
        "details": details,
        "failed_calls": sum(len(value["failures"]) for value in summaries.values()),
    }
