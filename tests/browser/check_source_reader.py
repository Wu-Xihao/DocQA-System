"""Isolated Chromium check for clickable source documents and the three-tab reader."""

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8787")
args = parser.parse_args()
url = urlsplit(args.url)
if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path not in {"", "/"}:
    parser.error("Use an isolated loopback Demo")
base = args.url.rstrip("/")
output = Path("runtime/source-reader-browser")
output.mkdir(parents=True, exist_ok=True)
errors, checks, bootstrap_times = [], [], []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True
    )
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    context.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(base) else route.abort(),
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    boot = page.request.get(base + "/api/v1/bootstrap").json()
    assert boot["settings"]["synthetic"]
    for _ in range(3):
        started = time.perf_counter()
        response = page.request.get(base + "/api/v1/bootstrap")
        assert response.ok and response.json()["knowledge_base"]["evidence_verified"] is False
        bootstrap_times.append(round(time.perf_counter() - started, 4))
    expect(page.locator(".document-name").first).to_be_visible()
    page.locator(".document-name").first.click()
    expect(page.locator("#preview-dialog")).to_be_visible()
    expect(page.locator(".reader-block").first).to_be_visible(timeout=30000)
    page.get_by_role("tab", name="Markdown", exact=True).click()
    expect(page.locator(".reader-content .reader-markdown")).to_be_visible(timeout=30000)
    page.get_by_role("tab", name="MD 源码与定位").click()
    expect(page.locator(".reader-line").first).to_be_visible(timeout=30000)
    expect(page.get_by_role("link", name="打开原文件", exact=True)).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role("link", name="下载原文件", exact=True).click()
    assert download.value.suggested_filename.endswith((".docx", ".pdf", ".pptx"))
    checks.append("document name opens original text, Markdown, raw MD and original download")
    page.locator("#close-preview").click()
    page.locator("#new-chat").click()
    page.locator("#question-input").fill("DOCQA_IMAGE")
    page.locator("#question-input").press("Enter")
    expect(page.locator(".answer-document").first).to_be_visible(timeout=30000)
    citation = page.locator(".answer-body .citation-chip").first
    assert citation.inner_text().startswith("[")
    citation.click()
    expect(page.locator("#preview-dialog")).to_be_visible(timeout=30000)
    expect(page.locator(".reader-content mark").first).to_be_visible(timeout=30000)
    page.get_by_role("tab", name="MD 源码与定位").click()
    expect(page.locator(".cited-line").first).to_be_visible(timeout=30000)
    page.screenshot(path=str(output / "citation-reader.png"), full_page=True)
    checks.append(
        "inline numbered citation opens the same-version document and highlights MD block"
    )
    page.locator("#close-preview").click()
    page.locator(".answer-document").first.click()
    expect(page.locator("#preview-dialog")).to_be_visible(timeout=30000)
    checks.append("answer reference document chip opens original-content reader")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("tab", name="Markdown", exact=True).click()
    expect(page.locator(".reader-content .reader-markdown")).to_be_visible(timeout=30000)
    page.screenshot(path=str(output / "mobile-reader.png"), full_page=True)
    checks.append("mobile reader tabs remain usable")
    assert not errors, errors
    browser.close()
report = {
    "status": "passed",
    "checks": checks,
    "browser_errors": errors,
    "synthetic_bootstrap_http_seconds": bootstrap_times,
}
(output / "report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=True))
