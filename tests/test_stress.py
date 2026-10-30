"""Offline stress harness: malformed inputs are rejected explicitly, never crash."""

import json
from pathlib import Path

from docqa.stress import stress


def test_small_stress_run_rejects_malformed_inputs_and_reports_latency(tmp_path):
    report = stress(tmp_path, documents=6, rows=30, queries=6, workers=3)
    assert report["passed"], report["unexpected"]
    assert report["external_requests"] == 0 and report["synthetic"] is True
    for name in ("sequential", "concurrent", "filtered"):
        latency = report["retrieval"][name]
        assert latency["requests"] == 6 and latency["p95_ms"] >= latency["p50_ms"] > 0
    assert report["retrieval"]["concurrent"]["empty_results"] == 0
    rejected = {r["input"]: r for r in report["robustness"] if r["rejected"]}
    assert rejected["traversal.docx"]["error_code"] == "UNSAFE_ARCHIVE"
    assert rejected["legacy-ole.xlsx"]["error_code"] == "PROTECTED_OR_BINARY_OFFICE"
    assert not any(str(r.get("error_code", "")).startswith("CRASH") for r in report["robustness"])
    saved = json.loads(Path(report["report"]).read_text(encoding="utf-8"))
    assert saved["documents"] == 6
