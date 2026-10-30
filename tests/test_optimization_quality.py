"""Offline quality gates and source-preserving complex-table/visual checks."""

import json
import time
from copy import deepcopy

import httpx
import pytest
import test_agent_integration as shared
from PIL import Image

from docqa.agent.config import AgentOptions
from docqa.doc2md.config import VisionConfig
from docqa.doc2md.table_links import link_pdf_tables
from docqa.domain.documents import Block, ParsedContent, SourceLocator
from docqa.domain.errors import DocQAError
from docqa.quality import diagnostics, evaluate, percentile
from docqa.web.tasks import TaskStore
from docqa.web.visual import check_image


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return shared.corpus.__wrapped__(tmp_path_factory)


def test_quality_gate_records_real_failures_and_no_private_excerpt_in_report(corpus, tmp_path):
    index, options, _ = corpus
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "positive",
                    "question": "FE_HEART_Start 心跳",
                    "expected_status": ["answered"],
                    "must_include": ["FE_HEART_Start"],
                    "must_not_include": ["FAKE_FALSE_VALUE"],
                },
                {
                    "id": "negative",
                    "question": "TLS 证书链",
                    "expected_status": ["answered"],
                    "must_include": ["FAKE_FALSE_VALUE"],
                },
            ]
        ),
        encoding="utf-8",
    )
    report = evaluate(index, path, tmp_path / "reports", options)
    assert not report["passed"] and report["pass_rate"] == 0.5
    assert report["latency"]["p95"] >= report["latency"]["p50"] > 0
    saved = json.loads(open(report["report"], encoding="utf-8").read())
    assert "FE_HEART_Start" not in json.dumps(saved) and "证书链" not in json.dumps(
        saved, ensure_ascii=False
    )
    assert all(row["synthetic"] and not row["verified"] for row in report["results"])
    with pytest.raises(DocQAError, match="allow-http"):
        evaluate(index, path, tmp_path / "reports", AgentOptions(mode="http"))
    assert percentile([1, 2, 3], 0.5) == 2 and percentile([], 0.95) is None


def table(identity, page, box, rows):
    return Block(
        block_id=identity,
        type="table",
        order=page,
        table_rows=rows,
        locators=[
            SourceLocator(
                locator_type="pdf_page",
                page=page,
                bbox=box,
                precision="exact_region",
                display_label=f"PDF page {page}",
            )
        ],
    )


def test_table_continuation_is_only_association_not_fabricated_rows():
    a = table("a", 1, (0.1, 0.6, 0.9, 0.95), [["Name", "Value"], ["A", "1"]])
    b = table("b", 2, (0.1, 0.05, 0.9, 0.3), [["Name", "Value"], ["B", "2"]])
    original = deepcopy((a.table_rows, b.table_rows, a.locators, b.locators))
    linked = link_pdf_tables(ParsedContent(blocks=[a, b]))
    assert a.metadata["continuation_next"] == "b" and b.metadata["continuation_previous"] == "a"
    assert linked.metadata["table_continuations"][0]["verification"] == "heuristic_only"
    assert (a.table_rows, b.table_rows, a.locators, b.locators) == original
    mismatch = table("c", 2, (0.1, 0.05, 0.9, 0.3), [["Other", "Value"], ["C", "3"]])
    assert (
        "table_continuations" not in link_pdf_tables(ParsedContent(blocks=[a, mismatch])).metadata
    )


@pytest.mark.parametrize("ids", [["A"], ["A", "B"], ["WRONG"]])
def test_visual_check_sends_pixels_and_requires_exact_claim_identity(tmp_path, ids):
    path = tmp_path / "actual.png"
    Image.new("RGB", (50, 30), "blue").save(path)

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "separate-vl"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        content = {
            "checks": [
                {
                    "claim_id": identity,
                    "state": "uncertain",
                    "observations": ["blue pixels"],
                    "reason": "not enough visible text",
                }
                for identity in ids
            ]
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]
            },
        )

    config = VisionConfig(
        mode="http", endpoint="https://fake.invalid/chat", model="separate-vl", revision="1"
    )
    args = (path, "synthetic question", [{"claim_id": "A", "text": "diagram arrow A to B"}], config)
    if ids == ["A"]:
        report = check_image(*args, transport=httpx.MockTransport(handler))
        assert (
            report["checks"][0]["state"] == "uncertain"
            and not report["answer_verification_changed"]
        )
    else:
        with pytest.raises(DocQAError):
            check_image(*args, transport=httpx.MockTransport(handler))


def test_diagnostics_counts_rules_and_phases_without_question_or_quote(tmp_path):
    tasks = TaskStore(tmp_path)
    job = tasks.enqueue("owner", {"question": "private question never in report"})
    answer_id = "a" * 32
    tasks.claim()
    tasks.finish(
        job["job_id"],
        receipt={"answer_id": answer_id, "session_id": "b" * 32, "status": "failed"},
        error_code="CITATION_INVALID",
    )
    folder = tmp_path / "agent" / job["job_id"] / "answers" / answer_id
    folder.mkdir(parents=True)
    (folder / "answer.json").write_text(
        json.dumps(
            {
                "question": "private question never in report",
                "trace": [
                    {
                        "stage": "citation_validation_failed",
                        "validation_rule": "exact_quote_missing",
                    },
                    {"stage": "performance", "operations": {"evidence.load": 1.5}},
                ],
            }
        )
    )
    run = tmp_path / "runs" / job["job_id"]
    run.mkdir(parents=True)
    (run / "timing.json").write_text(
        json.dumps({"started_at": time.time(), "elapsed_seconds": 2.5})
    )
    report = diagnostics(tmp_path)
    assert report["validation_rules"] == {"exact_quote_missing": 1}
    assert report["phases"]["evidence.load"]["p95"] == 1.5
    assert report["runner_p50"] == 2.5 and "private question" not in json.dumps(report)
