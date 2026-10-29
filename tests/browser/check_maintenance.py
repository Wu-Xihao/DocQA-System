"""Isolated browser: deletion, checked answer SSE and automatic folder monitoring."""

import json
import re
import time
from pathlib import Path

from docx import Document
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[2]
base = "http://127.0.0.1:8790"
output = root / "runtime/maintenance-browser"
output.mkdir(exist_ok=True)
checks, errors, streams = [], [], []

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True
    )
    context = browser.new_context(viewport={"width": 1540, "height": 1000})
    context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.on(
        "response",
        lambda response: (
            streams.append(response.url) if "/api/v1/events?" in response.url else None
        ),
    )
    page.goto(base)
    boot = context.request.get(base + "/api/v1/bootstrap").json()
    assert boot["settings"]["synthetic"]
    headers = {"X-DocQA-Token": boot["csrf_token"]}
    expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
    expect(page.locator(".document-name").first).to_be_visible()
    page.locator("#question-input").fill("DOCQA_IMAGE")
    page.locator("#question-input").press("Enter")
    expect(page.locator(".message-actions")).to_have_count(1, timeout=30000)
    assert streams
    assert page.locator(".answer-body .claim").count() >= 1
    conversation = context.request.get(base + "/api/v1/conversations", headers=headers).json()[
        "items"
    ][0]
    messages = context.request.get(
        base + f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
    ).json()["items"]
    job = messages[-1]["job"]
    checks.append("question completes over SSE and checked citations remain clickable")
    page.locator(".message-actions").get_by_role("button", name="生成成果").click()
    page.locator(".studio-action").filter(has_text="研究文档").click()
    expect(page.locator("#preview-dialog")).to_be_visible(timeout=30000)
    page.locator("#close-preview").click()
    expect(page.get_by_role("button", name=re.compile("^删除成果"))).to_be_visible()
    artifacts = context.request.get(
        base + f"/api/v1/conversations/{conversation['id']}/artifacts", headers=headers
    ).json()["items"]
    aid = artifacts[0]["id"]
    page.get_by_role("button", name=re.compile("^删除成果")).click()
    expect(page.locator(".artifact-card")).to_have_count(0)
    assert context.request.get(base + "/api/v1/artifacts/" + aid, headers=headers).status == 404
    assert not (root / "runtime/maintenance-demo/web/studio" / aid).exists()
    assert (
        context.request.get(base + f"/api/v1/queries/{job['answer_id']}", headers=headers).status
        == 200
    )
    checks.append("artifact delete removes backend file and metadata while preserving the answer")
    page.screenshot(path=str(output / "delete-controls.png"))
    page.get_by_role("button", name=re.compile("^删除聊天")).click()
    expect(page.locator(".history-row")).to_have_count(0)
    expect(page.locator(".message-actions")).to_have_count(0)
    until = time.monotonic() + 15
    state = root / "runtime/maintenance-demo/web"
    while (state / "agent" / job["job_id"]).exists() and time.monotonic() < until:
        page.wait_for_timeout(150)
    assert not (state / "agent" / job["job_id"]).exists()
    assert not (state / "runs" / job["job_id"]).exists()
    assert (
        context.request.get(base + "/api/v1/jobs/" + job["job_id"], headers=headers).status == 404
    )
    checks.append("conversation delete clears transcript, backend job/session and runner files")

    watch = root / "runtime/maintenance-demo/uploads" / ("watch-" + str(time.time_ns()))
    (watch / "sub").mkdir(parents=True)
    source = watch / "sub/watcher.docx"

    def write(text):
        doc = Document()
        doc.add_paragraph(text)
        doc.save(source)

    write("Protocol: WATCH_BROWSER first revision.")
    page.locator("#maintenance-section > summary").click()
    expect(page.locator("#watch-allowed")).to_contain_text("允许目录")
    page.locator("#watch-path").fill(str(watch))
    page.locator("#watch-interval").fill("15")
    page.locator('[data-watch-type="pdf"]').uncheck()
    page.locator('[data-watch-type="pptx"]').uncheck()
    page.locator("#watch-add").click()
    expect(page.locator(".watch-item")).to_have_count(1)

    def find_document():
        value = context.request.get(base + "/api/v1/documents", headers=headers).json()
        return next((d for d in value["documents"] if d["source_name"] == "watcher.docx"), None)

    until = time.monotonic() + 45
    first = None
    while time.monotonic() < until:
        first = find_document()
        if first:
            break
        page.wait_for_timeout(300)
    assert first and first["category"] == "技术规范"
    write("Protocol: WATCH_BROWSER changed second revision.")
    until = time.monotonic() + 50
    changed = None
    while time.monotonic() < until:
        changed = find_document()
        if changed and changed["artifact_id"] != first["artifact_id"]:
            break
        page.wait_for_timeout(400)
    assert changed and changed["artifact_id"] != first["artifact_id"], (
        "Automatic poll did not update the modified document"
    )
    page.locator("#index-status").click()
    expect(page.locator("#index-status-text")).to_contain_text("SQLite-vec", timeout=30000)
    page.screenshot(path=str(output / "watch-manager.png"))
    checks.append(
        "recursive DOCX watch auto-ingests and later updates a changed file; vector status is SQLite-vec"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#toggle-left").click()
    expect(page.locator("#watch-add")).to_be_visible()
    assert not errors, errors
    browser.close()
report = {"status": "passed", "checks": checks, "browser_errors": errors, "real_model_calls": False}
(output / "report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
)
print(json.dumps(report, ensure_ascii=True))
