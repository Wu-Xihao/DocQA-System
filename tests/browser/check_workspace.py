"""Isolated real Chromium tests; no user browser profile or real model requests."""

import argparse
import io
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from docx import Document
from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description="Browser checks against an isolated demo Web server")
parser.add_argument("--url", default="http://127.0.0.1:8787")
parser.add_argument("--browser", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
args = parser.parse_args()

selected = urlsplit(args.url)
if (
    selected.scheme != "http"
    or selected.hostname not in {"127.0.0.1", "localhost"}
    or selected.path not in {"", "/"}
    or selected.query
    or selected.fragment
):
    parser.error("Use a loopback demo HTTP origin")
BASE = args.url.rstrip("/")
OUTPUT = Path(__file__).resolve().parents[2] / "runtime/web-browser-check"
OUTPUT.mkdir(parents=True, exist_ok=True)
checks = []
errors = []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path=args.browser,
        headless=True,
    )
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    context.route(
        "**/*",
        lambda route: route.continue_() if route.request.url.startswith(BASE) else route.abort(),
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE)
    initial = context.request.get(BASE + "/api/v1/bootstrap").json()
    assert initial["settings"]["synthetic"], (
        "Run against the isolated demo configuration, not the real-model service"
    )
    expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
    expect(page.locator("#document-count")).not_to_have_text("0", timeout=20000)
    assert not page.locator("#connection-status").inner_text().endswith("异常")
    checks.append("real HTTP module loading")
    for path in [
        "/static/app.js",
        "/static/js/main.js",
        "/static/js/dom.js",
        "/static/js/api.js",
        "/static/js/renderers.js",
    ]:
        response = context.request.get(BASE + path)
        assert response.headers["content-type"].startswith("text/javascript"), path
    checks.append("browser HTTP MIME")
    page.locator("#theme-button").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.locator("#theme-button").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    checks.append("theme switch")
    for width in [1440, 1100, 820, 390]:
        page.set_viewport_size({"width": width, "height": 1000})
        page.evaluate(
            "document.getElementById('workspace').classList.remove('left-open','right-open','left-hidden','right-hidden')"
        )
        # Desktop/medium left sidebar is docked. Mobile left starts closed.
        if width >= 960:
            expect(page.locator("#left-panel")).to_be_visible()
            page.locator("#toggle-left").click()
            expect(page.locator("#left-panel")).to_be_hidden()
        page.locator("#toggle-left").click()
        expect(page.locator("#left-panel")).to_be_visible()
        if width == 390:
            page.screenshot(path=str(OUTPUT / "mobile-left.png"))
        # File chooser must retain trusted click activation on every viewport.
        with page.expect_file_chooser() as chooser:
            page.locator("#dropzone").click()
        assert chooser.value.is_multiple()
        if width >= 1280:
            page.locator("#toggle-right").click()
            expect(page.locator("#right-panel")).to_be_hidden()
        page.locator("#toggle-right").click()
        expect(page.locator("#right-panel")).to_be_visible()
        if width == 390:
            page.screenshot(path=str(OUTPUT / "mobile-right.png"))
        if width < 960:
            expect(page.locator("#left-panel")).to_be_hidden()
        page.locator("#toggle-right").click()
        expect(page.locator("#right-panel")).to_be_hidden()
        page.screenshot(path=str(OUTPUT / f"workspace-{width}.png"))
        checks.append(f"{width}px drawers and file chooser")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#new-chat").click()
    page.locator("#select-none").click()
    expect(page.locator("#ask-button")).to_be_disabled()
    page.locator("#select-all").click()
    expect(page.locator("#ask-button")).to_be_enabled()
    page.locator("#question-input").fill("DOCQA_IMAGE")
    page.locator("#question-input").press("Enter")
    expect(page.locator(".user-message")).to_have_count(1)
    expect(page.locator(".message-actions")).to_have_count(1, timeout=30000)
    assert page.url.rstrip("/") == BASE
    checks.append("Enter submits without native navigation")
    page.locator("#question-input").fill("它包括哪些阶段？")
    page.locator("#ask-button").click()
    expect(page.locator(".message-actions")).to_have_count(2, timeout=30000)
    checks.append("button submits follow-up")
    timing = page.locator(".answer-diagnostics").filter(has_text="耗时明细").first
    expect(timing).to_be_visible()
    timing.locator("summary").click()
    expect(timing).to_contain_text("回答支持校验")
    expect(timing).to_contain_text("重排")
    timing.locator("summary").click()
    checks.append("phase timing panel with verification and reranking")
    page.locator("#question-input").fill("第一行")
    page.locator("#question-input").press("Shift+Enter")
    expect(page.locator("#question-input")).to_have_value("第一行\n")
    expect(page.locator(".user-message")).to_have_count(2)
    checks.append("Shift+Enter retains draft")
    page.locator(".message-actions").first.get_by_role("button", name="相关图片").click()
    expect(page.locator("#images-list > *").first).to_be_visible()
    if page.locator(".image-card").count():
        expect(page.locator("#images-list")).to_contain_text("未参与回答证据")
        page.locator(".image-card button").first.click()
        expect(page.locator("#image-dialog")).to_be_visible()
        expect(page.locator("#lightbox-image")).to_be_visible()
        expect(page.locator("#lightbox-image")).to_have_js_property("complete", True)
        assert page.locator("#lightbox-image").evaluate("img=>img.naturalWidth > 0")
        page.locator("#close-image").click()
        checks.append("related reference image lightbox")
    else:
        # A native Word source without page mapping must not guess an image association.
        expect(page.locator("#images-list")).to_contain_text("不会猜测图片所在页")
        checks.append("unmapped Word source reports absent association without guessing")
    page.locator('[data-tab="sources"]').click()
    page.locator(".citation-card").first.click()
    expect(page.locator(".source-detail blockquote")).to_be_visible()
    expect(page.locator("#preview-dialog")).to_be_visible()
    page.locator("#close-preview").click()
    checks.append("source citation opens original reader")
    page.locator(".message-actions").first.get_by_role("button", name="生成成果").click()
    page.locator(".studio-action").filter(has_text="思维导图").click()
    expect(page.locator("#preview-dialog")).to_be_visible(timeout=25000)
    expect(page.locator(".mindmap svg")).to_be_visible()
    page.locator(".map-outline > summary").click()
    expect(page.locator(".map-branch").first).to_be_visible()
    page.get_by_role("button", name="折叠全部", exact=True).click()
    assert not page.locator(".map-branch").first.evaluate("el=>el.open")
    page.get_by_role("button", name="展开全部", exact=True).click()
    assert page.locator(".map-branch").first.evaluate("el=>el.open")
    page.screenshot(path=str(OUTPUT / "mindmap.png"))
    page.locator("#preview-actions select").select_option("json")
    with page.expect_download() as exported:
        page.locator("#preview-actions").get_by_role("button", name="导出").click()
    exported.value.save_as(str(OUTPUT / "mindmap.json"))
    assert json.loads((OUTPUT / "mindmap.json").read_text(encoding="utf-8"))["kind"] == "mindmap"
    page.locator("#close-preview").click()
    checks.append("mindmap preview, branch folding and downloaded export")
    page.locator("#question-input").fill("")
    # Genuine browser XHR and one-shot Service ingestion, using synthetic DOCX.
    token = context.request.get(BASE + "/api/v1/bootstrap").json()["csrf_token"]
    for mode in ["selection", "drop"]:
        document = Document()
        marker = "UPLOAD_BROWSER_" + mode.upper() + "_" + str(time.time_ns())
        document.add_paragraph(marker + " browser upload document")
        output = io.BytesIO()
        document.save(output)
        name = "browser-" + mode + "-" + str(time.time_ns()) + ".docx"
        raw = output.getvalue()
        if mode == "selection":
            page.locator("#document-search").fill("filter-not-matching-new-file")
        if not page.locator("#left-panel").is_visible():
            page.locator("#toggle-left").click()
        with page.expect_response(
            lambda response: response.url.endswith("/commit") and response.request.method == "POST"
        ) as committed:
            if mode == "selection":
                with page.expect_file_chooser() as selected:
                    page.locator("#dropzone").click()
                selected.value.set_files(
                    {
                        "name": name,
                        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "buffer": raw,
                    }
                )
            else:
                page.evaluate(
                    """({name, bytes}) => {
                    const dt = new DataTransfer();
                    dt.items.add(new File([new Uint8Array(bytes)], name, {type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}));
                    const dz = document.getElementById('dropzone');
                    for (const kind of ['dragenter','dragover','drop']) dz.dispatchEvent(new DragEvent(kind,{bubbles:true,cancelable:true,dataTransfer:dt}));
                }""",
                    {"name": name, "bytes": list(raw)},
                )
        response = committed.value
        assert response.status == 202, response.text()
        record = response.json()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            result = context.request.get(
                BASE + "/api/v1/uploads/" + record["id"], headers={"X-DocQA-Token": token}
            ).json()
            if result["state"] in ["ready", "ready_with_gaps"]:
                break
            assert result["state"] not in ["failed", "review_required"], result
            page.wait_for_timeout(400)
        else:
            raise AssertionError("Browser upload did not publish")
        # Stay inside the pinned chat: library updates without switching history.
        if mode == "selection":
            filtered = page.locator(f'.upload-row[data-upload-id="{record["id"]}"]')
            expect(filtered).to_contain_text("被当前筛选隐藏", timeout=20000)
            filtered.get_by_role("button", name="显示此资料").click()
        doc_row = page.locator(f'.document-row[data-doc-id="{record["doc_id"]}"]')
        expect(doc_row).to_contain_text(name, timeout=20000)
        expect(doc_row.locator('input[type="checkbox"]')).to_be_disabled()
        expect(page.locator("#library-version-note")).to_be_visible()
        page.locator("#use-latest-library").click()
        expect(doc_row.locator('input[type="checkbox"]')).to_be_enabled()
        expect(page.locator("#library-version-note")).to_be_hidden()
        assert page.locator(".user-message").count() == 2
        checks.append(mode + " upload auto-refreshes library in old chat and upgrades explicitly")
    page.screenshot(path=str(OUTPUT / "chat.png"), full_page=True)
    assert not errors, errors

    broken = browser.new_context()
    broken.route(
        "**/static/js/main.js",
        lambda route: route.fulfill(
            status=200, content_type="text/plain", body="/* simulate invalid MIME */"
        ),
    )
    failed = broken.new_page()
    failed.goto(BASE)
    expect(failed.locator("html")).to_have_attribute("data-app-state", "failed")
    expect(failed.locator("#status-banner")).to_contain_text("交互组件加载失败")
    expect(failed.locator("#ask-button")).to_be_disabled()
    failed.locator("#question-input").fill("keep this draft")
    failed.evaluate("document.getElementById('question-form').requestSubmit()")
    expect(failed.locator("#question-input")).to_have_value("keep this draft")
    assert failed.url.rstrip("/") == BASE
    checks.append("failed module reports error and prevents destructive native submit")
    broken.close()

    corrupt = browser.new_context()
    corrupt.add_init_script("localStorage.setItem('docqa-ui-v1','null')")
    recovered = corrupt.new_page()
    recovered.goto(BASE)
    expect(recovered.locator("html")).to_have_attribute("data-app-state", "bound")
    recovered.locator("#theme-button").click()
    expect(recovered.locator("html")).to_have_attribute("data-theme", "dark")
    checks.append("invalid stored preferences cannot prevent event binding")
    corrupt.close()
    browser.close()

report = {"status": "passed", "checks": checks, "browser_errors": errors}
(OUTPUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
