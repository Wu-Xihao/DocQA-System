"""Isolated real browser + synthetic API acceptance for chat/retrieval switching."""

import json
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from docx import Document
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def main():
    sys.path.insert(0, str(ROOT / "src"))
    from docqa.agent.config import AgentOptions, ChatServiceOptions
    from docqa.agent.providers import DemoChatClient
    from docqa.doc2md.config import ParseOptions
    from docqa.doc2md.pipeline import parse_document
    from docqa.docbase.config import DocBaseOptions
    from docqa.docbase.pipeline import build_index
    from docqa.web.app import create_app
    from docqa.web.config import WebOptions

    calls = []
    demo = DemoChatClient()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            if "response_format" in payload:
                task = json.loads(payload["messages"][1]["content"])
                data = task["data"]
                if task["task"] == "assess":
                    result = demo.assess(data["question"], data["topics"], data["evidence"])
                elif task["task"] == "compose":
                    result = demo.compose(
                        data["question"],
                        data["topics"],
                        data["evidence"],
                        data["assessment"],
                        data["output_format"],
                    )
                elif task["task"] == "verify":
                    result = demo.verify(data["question"], data["draft"], data["evidence"])
                else:
                    raise AssertionError(task["task"])
                content = result.model_dump_json()
            else:
                question = payload["messages"][-1]["content"]
                content = "Synthetic chat reply: " + question
                if question == "what color?":
                    assert any("blue" in m["content"] for m in payload["messages"][:-1])
                    content = "Your synthetic color is blue."
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for part in (content[:10], content[10:]):
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps(
                                {
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": part},
                                            "finish_reason": None,
                                        }
                                    ]
                                }
                            )
                            + "\n\n"
                        ).encode()
                    )
                    self.wfile.flush()
                    time.sleep(0.4)
                self.wfile.write(
                    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                )
                self.wfile.flush()
                return
            raw = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()
    output = ROOT / "runtime/wordcloud-browser"
    output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="chat-ui-", dir=ROOT.parent) as temporary:
            folder = Path(temporary)
            retrieval = DocBaseOptions(mode="demo")
            chat = ChatServiceOptions(
                endpoint=f"http://127.0.0.1:{api.server_port}/chat",
                model="synthetic-browser",
                revision="1",
                retries=0,
                timeout=4,
                stream=True,
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            agent = AgentOptions(mode="http", generator=chat, verifier=chat, retrieval=retrieval)
            app = create_app(
                WebOptions(port=port, index=folder / "index", state=folder / "web", agent=agent)
            )
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert server.started
                base = f"http://127.0.0.1:{port}"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        headless=True,
                    )
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if route.request.url.startswith(base + "/")
                            else route.abort()
                        ),
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base)
                    expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
                    expect(page.locator("#mode-select")).to_have_value("retrieval")
                    expect(page.locator("#ask-button")).to_be_disabled()
                    page.locator("#mode-select").select_option("chat")
                    expect(page.locator("#ask-button")).to_be_enabled()
                    expect(page.locator("#format-select")).to_be_hidden()
                    page.locator("#question-input").fill("remember synthetic blue")
                    page.locator("#question-input").press("Enter")
                    expect(page.locator(".direct-chat")).to_have_count(1, timeout=30000)
                    expect(page.locator(".stream-label")).to_contain_text("实时回复")
                    expect(page.locator("#mode-select")).to_be_enabled()
                    page.locator("#question-input").fill("what color?")
                    page.locator("#question-input").press("Enter")
                    expect(page.locator(".direct-chat-body").last).to_contain_text(
                        "Your synthetic color is blue.", timeout=30000
                    )
                    assert len(calls) == 2 and all(
                        "response_format" not in value for value in calls
                    )
                    assert [m["role"] for m in calls[-1]["messages"]] == [
                        "system",
                        "user",
                        "assistant",
                        "user",
                    ]
                    assert page.locator(".citation-chip").count() == 0
                    page.reload()
                    expect(page.locator(".direct-chat")).to_have_count(2)
                    expect(page.locator("#mode-select")).to_have_value("chat")
                    page.screenshot(path=str(output / "chat-desktop.png"))
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    expect(page.locator("#mode-select")).to_be_visible()
                    page.screenshot(path=str(output / "chat-mobile.png"))

                    # Publish an isolated KB after chatting without one.
                    document = Document()
                    document.add_paragraph(
                        "MODEDOC_SYNTH: graph memory connects graph nodes and graph edges. "
                        "Graph memory supports evidence retrieval and memory ranking. "
                        "Evidence retrieval preserves citations. 图记忆通过知识图谱组织记忆，图记忆支持证据检索。"
                    )
                    source = folder / "sample.docx"
                    document.save(source)
                    parsed = parse_document(
                        source,
                        folder / "parsed",
                        options=ParseOptions(office_render="never", ocr_mode="disabled"),
                    )
                    build_index([Path(parsed.artifact_dir)], folder / "index", options=retrieval)
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.reload()
                    expect(page.locator(".document-name")).to_have_count(1)
                    page.locator("#new-chat").click()
                    expect(page.locator("#mode-select")).to_have_value("retrieval")
                    page.locator("#question-input").fill("MODEDOC_SYNTH")
                    page.locator("#question-input").press("Enter")
                    expect(page.locator(".answer-documents")).to_have_count(1, timeout=30000)
                    page.get_by_role("button", name="生成成果", exact=True).first.click()
                    page.locator("#studio-actions button").filter(has_text="思维导图").click()
                    expect(page.locator(".artifact-card")).to_have_count(1, timeout=30000)
                    expect(page.locator("#preview-dialog")).to_be_visible()
                    page.get_by_role("button", name="编辑编排", exact=True).click()
                    page.get_by_label("成果标题", exact=True).fill("人工编辑验收")
                    page.screenshot(path=str(output / "editor-desktop.png"))
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.locator(".editor-dialog").evaluate(
                        "(d)=>d.scrollWidth <= d.clientWidth + 1"
                    )
                    page.screenshot(path=str(output / "editor-mobile.png"))
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("button", name="保存为新版本", exact=True).click()
                    expect(page.locator(".editor-dialog")).to_have_count(0, timeout=10000)
                    expect(page.locator("#preview-title")).to_have_text("人工编辑验收")
                    page.locator("#preview-dialog").press("Escape")
                    assert page.locator(".citation-chip").count() > 0
                    expect(page.locator("#studio-actions button")).to_have_count(6)
                    assert page.locator("#studio-actions button").evaluate_all("""nodes => {
                        const rows = new Map();
                        for (const node of nodes) {
                            const rect=node.getBoundingClientRect(), y=Math.round(rect.y);
                            rows.set(y,(rows.get(y)||0)+1);
                        }
                        return rows.size===3 && [...rows.values()].every(n=>n===2);
                    }""")
                    page.locator('[data-artifact-kind="wordcloud"]').click()
                    expect(page.locator("#preview-dialog .wordcloud-view")).to_be_visible(
                        timeout=30000
                    )
                    assert page.locator(".wordcloud-word").count() > 5
                    page.locator(".wordcloud-word").first.focus()
                    page.locator(".wordcloud-word").first.press("Enter")
                    expect(page.locator(".wordcloud-selection")).to_contain_text("出现")
                    expect(page.locator("#preview-sources .source-block").first).to_be_visible()
                    page.get_by_role("button", name="放大词云", exact=True).click()
                    expect(page.locator(".wordcloud-tools")).to_contain_text("125%")
                    page.get_by_role("button", name="适应", exact=True).click()
                    page.screenshot(path=str(output / "wordcloud-desktop.png"))
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    assert (
                        page.locator('.wordcloud-word[data-color="2"]').first.evaluate(
                            "node=>getComputedStyle(node).fill"
                        )
                        == "rgb(190, 161, 238)"
                    )
                    page.screenshot(path=str(output / "wordcloud-dark.png"))
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.locator(".wordcloud-list > summary").click()
                    expect(page.locator(".wordcloud-term").first).to_be_visible()
                    page.screenshot(path=str(output / "wordcloud-mobile.png"))
                    page.locator("#preview-dialog").press("Escape")
                    if not page.locator("#right-panel").is_visible():
                        page.locator("#toggle-right").click()
                    page.screenshot(path=str(output / "six-actions-mobile.png"))
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.locator("#mode-select").select_option("chat")
                    page.locator("#question-input").fill("new ordinary turn")
                    page.locator("#question-input").press("Enter")
                    expect(page.locator(".direct-chat")).to_have_count(1, timeout=30000)
                    assert [m["role"] for m in calls[-1]["messages"]] == ["system", "user"]
                    expect(page.locator("#mode-select")).to_be_enabled()
                    expect(page.locator("#sources-count")).to_have_text("0")
                    assert all(
                        page.locator("#studio-actions button").nth(i).is_disabled()
                        for i in range(page.locator("#studio-actions button").count())
                    )
                    page.locator(".assistant-message").filter(
                        has=page.locator(".answer-documents")
                    ).get_by_role("button", name="重新生成", exact=True).click()
                    expect(page.locator(".answer-documents")).to_have_count(2, timeout=30000)
                    expect(page.locator("#mode-select")).to_have_value("retrieval")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.screenshot(path=str(output / "mixed-mobile.png"))
                    assert not errors, errors
                    browser.close()
                print(
                    "PASS: live provider SSE; mode isolation; RAG references; artifact editor; six actions in 3x2; source-bound wordcloud/zoom/keyboard/dark/390px; no JS errors or external model calls."
                )
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                listener.close()
                assert not thread.is_alive()
    finally:
        api.shutdown()
        api.server_close()
        api_thread.join(timeout=3)


if __name__ == "__main__":
    main()
