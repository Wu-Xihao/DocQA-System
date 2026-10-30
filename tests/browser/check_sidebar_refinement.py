"""Isolated real browser: compact maintenance, stable DOM, keyboard, mobile and dark mode."""

import json
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import uvicorn
import yaml
from docx import Document
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from docqa.agent.config import AgentOptions
    from docqa.doc2md.config import ParseOptions
    from docqa.doc2md.pipeline import parse_document
    from docqa.docbase.config import DocBaseOptions
    from docqa.docbase.pipeline import build_index
    from docqa.service.config import ServiceOptions
    from docqa.web.app import create_app
    from docqa.web.config import WebOptions

    output = ROOT / "runtime/sidebar-accordion-browser"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sidebar-ui-", dir=ROOT.parent) as temporary:
        root = Path(temporary)
        inputs = root / "项目资料"
        inputs.mkdir()
        (root / "uploads").mkdir()
        retrieval = DocBaseOptions(mode="demo", vector_backend="sqlite_vec")
        artifacts = []
        for name in ("系统架构设计", "实验记录"):
            source = inputs / (name + ".docx")
            document = Document()
            document.add_paragraph("SIDEBAR_SYNTHETIC " + name + "：本资料仅用于界面与交互验收。")
            document.save(source)
            artifacts.append(
                Path(
                    parse_document(
                        source,
                        root / "parsed",
                        options=ParseOptions(office_render="never", ocr_mode="disabled"),
                    ).artifact_dir
                )
            )
        build_index(artifacts, root / "service/index", options=retrieval)
        service_config = root / "service.yaml"
        service_config.write_text(
            yaml.safe_dump(
                ServiceOptions(
                    allowed_source_roots=[inputs, root / "uploads"],
                    settle_seconds=0,
                    parse=ParseOptions(office_render="never", ocr_mode="disabled"),
                    retrieval=retrieval,
                ).model_dump(mode="json")
            ),
            encoding="utf-8",
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        app = create_app(
            WebOptions(
                port=port,
                index=root / "service/index",
                state=root / "web",
                service_state=root / "service",
                service_config=service_config,
                upload_root=root / "uploads",
                service_mode="external",
                agent=AgentOptions(mode="demo", retrieval=retrieval),
            )
        )
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            until = time.monotonic() + 10
            while not server.started and time.monotonic() < until:
                time.sleep(0.02)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    headless=True,
                )
                context = browser.new_context(viewport={"width": 1540, "height": 1050})
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.url.startswith(base + "/")
                        else route.abort()
                    ),
                )
                page = context.new_page()
                # Migrate old preferences that allowed all four sections open.
                page.add_init_script("""if (!localStorage.getItem('docqa-ui-v1')) {
                    localStorage.setItem('docqa-ui-v1', JSON.stringify({
                        documentsOpen:true, scopeOpen:true, historyOpen:true, maintenanceOpen:true
                    }));
                }""")
                errors = []
                requests = Counter()
                failures = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "request",
                    lambda request: requests.update(
                        [(request.method, request.url.split("?", 1)[0])]
                    ),
                )
                page.on(
                    "response",
                    lambda response: (
                        failures.append(response.url) if response.status >= 400 else None
                    ),
                )
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(base)
                expect(page.locator("#document-count")).to_have_text("2")
                expect(page.locator("#watch-new")).to_be_enabled()
                assert page.locator("#left-panel > details").evaluate_all(
                    "nodes=>nodes.map(n=>n.id)"
                ) == [
                    "documents-section",
                    "scope-section",
                    "history-section",
                    "maintenance-section",
                ]
                expect(page.locator("#maintenance-section .section-icon svg")).to_have_count(1)
                expect(page.locator("#history-section .section-icon svg")).to_have_count(1)
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                expect(page.locator("#documents-section")).to_have_attribute("open", "")
                # A collapsed History must not stretch and push Maintenance down.
                assert page.evaluate("""() => {
                    const history=document.getElementById('history-section').getBoundingClientRect();
                    const maintenance=document.getElementById('maintenance-section').getBoundingClientRect();
                    return Math.abs(maintenance.top-history.bottom) <= 1;
                }""")
                page.locator("#scope-section > summary").focus()
                page.locator("#scope-section > summary").press("Enter")
                expect(page.locator("#scope-section")).to_have_attribute("open", "")
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                page.locator("#history-section > summary").press("Space")
                expect(page.locator("#history-section")).to_have_attribute("open", "")
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                page.locator("#documents-section > summary").click()
                assert requests[("GET", base + "/api/v1/maintenance/sources")] == 0
                # Seed titles via the same signed browser; all material is synthetic.
                boot = context.request.get(base + "/api/v1/bootstrap").json()
                headers = {"X-DocQA-Token": boot["csrf_token"]}
                for title in ("梳理系统架构", "整理实验结果"):
                    assert (
                        context.request.post(
                            base + "/api/v1/conversations", headers=headers, data={"title": title}
                        ).status
                        == 201
                    )
                page.locator("#refresh-history").click()
                expect(page.locator(".history-row")).to_have_count(2)
                page.screenshot(path=str(output / "overview.png"))

                # Repeated reads of an unchanged catalog keep row/menu and focus identity.
                page.evaluate(
                    "window.keptRow=document.querySelector('.document-row');window.keptSelect=document.getElementById('category-select')"
                )
                page.locator(".document-row .row-menu summary").first.click()
                with page.expect_response(lambda r: "/api/v1/documents" in r.url):
                    page.locator("#refresh-documents").click()
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(150)
                assert page.evaluate(
                    "document.contains(window.keptRow) && window.keptRow.querySelector('.row-menu').open"
                )
                page.locator("#scope-section > summary").click()
                page.locator("#category-select").focus()
                with page.expect_response(lambda r: "/api/v1/documents" in r.url):
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                assert page.evaluate("document.activeElement===window.keptSelect")
                page.locator("#documents-section > summary").click()
                page.locator("#document-search").fill("架构")
                expect(page.locator(".document-row")).to_have_count(1)
                page.locator("#document-search").fill("")
                expect(page.locator(".document-row")).to_have_count(2)

                # Lazy maintenance loading and a collapsed add form/help.
                page.locator("#documents-section > summary").click()
                page.locator("#scope-section > summary").click()
                page.locator("#maintenance-section > summary").click()
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                expect(page.locator(".watch-empty")).to_be_visible()
                expect(page.locator("#watch-form")).to_be_hidden()
                assert not page.locator(".maintenance-help").evaluate("n=>n.open")
                before = requests[("GET", base + "/api/v1/maintenance/sources")]
                page.locator("#maintenance-section > summary").click()
                page.locator("#maintenance-section > summary").click()
                page.wait_for_timeout(100)
                assert requests[("GET", base + "/api/v1/maintenance/sources")] == before
                page.locator("#watch-new").click()
                expect(page.locator("#watch-path")).to_be_focused()
                page.locator("#watch-path").fill(str(inputs))
                page.locator("#watch-interval").fill("60")
                page.locator('[data-watch-type="pdf"]').uncheck()
                page.locator('[data-watch-type="pptx"]').uncheck()
                page.screenshot(path=str(output / "add-directory.png"))
                assert page.locator("#watch-add").evaluate(
                    "b=>{b.click();b.click();return b.disabled;}"
                )
                try:
                    expect(page.locator(".watch-item")).to_have_count(1, timeout=10000)
                except AssertionError as error:
                    page.screenshot(path=str(output / "failure.png"))
                    raise AssertionError(
                        {
                            "feedback": page.locator("#watch-feedback").inner_text(),
                            "errors": errors,
                            "http_failures": failures,
                            "post_count": requests[("POST", base + "/api/v1/maintenance/sources")],
                        }
                    ) from error
                assert requests[("POST", base + "/api/v1/maintenance/sources")] == 1
                expect(page.locator("#watch-form")).to_be_hidden()
                expect(page.locator(".watch-name")).to_have_text("项目资料")
                expect(page.locator(".watch-meta")).to_contain_text("每 1 分钟")
                page.locator(".watch-menu summary").click()
                page.get_by_role("button", name="暂停同步", exact=True).click()
                expect(page.locator(".watch-state")).to_have_text("已暂停")
                page.locator(".watch-menu summary").click()
                page.get_by_role("button", name="恢复同步", exact=True).click()
                expect(page.locator(".watch-state")).to_have_text("已启用")
                page.get_by_role("button", name="立即扫描", exact=True).click()
                expect(page.locator("#watch-feedback")).to_contain_text("扫描已排队")
                page.locator(".watch-menu summary").click()
                page.get_by_role("button", name="查看文件", exact=True).click()
                expect(page.locator(".watch-files")).to_contain_text("已发现 2 份文件")
                page.locator(".watch-files > summary").click()
                page.locator("#index-status").click()
                expect(page.locator("#index-status-text")).to_contain_text("2 份资料")
                page.screenshot(path=str(output / "maintenance-light.png"))
                page.locator("#theme-button").click()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert (
                    page.locator(".new-chat").evaluate("n=>getComputedStyle(n).backgroundColor")
                    != "rgb(255, 255, 255)"
                )
                assert (
                    page.locator(".history-open").first.evaluate(
                        "n=>Number(getComputedStyle(n).color.match(/\\d+/)[0])"
                    )
                    > 150
                )
                page.screenshot(path=str(output / "maintenance-dark.png"))
                page.reload()
                expect(page.locator("#maintenance-section")).to_have_attribute("open", "")
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                expect(page.locator(".watch-item")).to_have_count(1)
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#toggle-left").click()
                page.locator("#watch-new").click()
                page.locator("#watch-form").scroll_into_view_if_needed()
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                assert page.locator("#left-panel").evaluate("n=>n.scrollWidth<=n.clientWidth+1")
                page.screenshot(path=str(output / "maintenance-mobile.png"))
                page.locator("#watch-cancel").click()
                expect(page.locator("#watch-new")).to_be_focused()
                page.locator(".watch-menu summary").click()
                page.locator(".watch-menu summary").press("Escape")
                assert not page.locator(".watch-menu").evaluate("n=>n.open")
                expect(page.locator("#left-panel")).to_be_visible()
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                # All-closed is intentional and survives reload; reopening one
                # header still restores the one-at-a-time interaction on mobile.
                page.locator("#maintenance-section > summary").click()
                expect(page.locator("#left-panel > details[open]")).to_have_count(0)
                page.wait_for_function(
                    "() => JSON.parse(localStorage.getItem('docqa-ui-v1')).sidebarSection===null"
                )
                page.reload()
                expect(page.locator("#left-panel > details[open]")).to_have_count(0)
                page.locator("#toggle-left").click()
                page.locator("#history-section > summary").click()
                expect(page.locator("#history-section")).to_have_attribute("open", "")
                expect(page.locator("#left-panel > details[open]")).to_have_count(1)
                page.screenshot(path=str(output / "accordion-mobile.png"))
                assert not errors, errors
                assert not failures, failures
                recovery = context.new_page()
                recovery.route(
                    "**/api/v1/documents**",
                    lambda route: route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps({"error": {"code": "PROFILE_MISMATCH"}}),
                    ),
                )
                recovery.goto(base)
                expect(recovery.locator("#status-banner")).to_contain_text("索引与当前模型")
                expect(recovery.locator("#watch-new")).to_be_enabled()
                if not recovery.locator("#left-panel").is_visible():
                    recovery.locator("#toggle-left").click()
                recovery.locator("#maintenance-section > summary").click()
                expect(recovery.locator(".watch-item")).to_have_count(1)
                recovery.close()
                browser.close()
                (output / "report.json").write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "synthetic": True,
                            "external_requests": 0,
                            "checks": [
                                "sidebar order and SVG icons",
                                "contiguous headers without flex spacer",
                                "exclusive accordion, legacy migration, keyboard and saved active/all-closed state",
                                "lazy/coalesced maintenance loading",
                                "stable document DOM and focus",
                                "debounced search",
                                "add/pause/resume/scan/files/index",
                                "duplicate submit guard",
                                "saved expand/theme preferences",
                                "390px drawer/keyboard/no overflow",
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            print(
                "PASS: sidebar layout, compact maintenance, stable DOM/focus, live operations, keyboard, dark/mobile; no external model calls."
            )
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()


if __name__ == "__main__":
    main()
