"""Assert actual panel geometry; display visibility alone misses grid regressions."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
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
OUTPUT = Path(__file__).resolve().parents[2] / "runtime/web-layout-check"
OUTPUT.mkdir(parents=True, exist_ok=True)
checks, errors = [], []


def geometry(page):
    return page.evaluate("""() => Object.fromEntries(['workspace','left-panel','main-content','right-panel','question-input'].map(id=>{
      const el=document.getElementById(id),r=el.getBoundingClientRect();
      return [id,{x:r.x,y:r.y,w:r.width,h:r.height,right:r.right,bottom:r.bottom,visible:getComputedStyle(el).display!=='none'}];
    }))""")


def assert_docked(page, left, right):
    g = geometry(page)
    chat, workspace, composer = g["main-content"], g["workspace"], g["question-input"]
    assert chat["w"] >= 480, g
    assert chat["x"] >= workspace["x"] - 1 and chat["right"] <= workspace["right"] + 1, g
    for name, visible in [("left", left), ("right", right)]:
        panel = g[name + "-panel"]
        assert panel["visible"] == visible, g
        expect(page.locator("#toggle-" + name)).to_have_attribute(
            "aria-expanded", str(visible).lower()
        )
        if visible:
            assert 260 <= panel["w"] <= 560, g
            assert panel["bottom"] <= workspace["bottom"] + 1, g
    if left:
        assert g["left-panel"]["right"] <= chat["x"] + 1, g
    if right:
        assert chat["right"] <= g["right-panel"]["x"] + 1, g
    assert (
        composer["w"] > 300 and composer["x"] >= chat["x"] and composer["right"] <= chat["right"]
    ), g
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert (
        float(
            page.locator("#question-input")
            .evaluate("el=>getComputedStyle(el).fontSize")
            .removesuffix("px")
        )
        >= 16
    )
    return g


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=args.browser, headless=True)
    context = browser.new_context(viewport={"width": 2505, "height": 1233})
    context.route(
        "**/*",
        lambda route: (
            route.continue_() if route.request.url.startswith(BASE + "/") else route.abort()
        ),
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE)
    assert context.request.get(BASE + "/api/v1/bootstrap").json()["settings"]["synthetic"]
    expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
    expect(page.locator("#document-count")).not_to_have_text("0")
    for width, height in [(2505, 1233), (1920, 1080), (1440, 900), (1280, 720)]:
        page.set_viewport_size({"width": width, "height": height})
        for name in ["left", "right"]:
            if not page.locator("#" + name + "-panel").is_visible():
                page.locator("#toggle-" + name).click()
        page.locator("#question-input").fill("保留草稿：切换侧栏时聊天仍可用")
        assert_docked(page, True, True)
        page.locator("#toggle-left").click()
        assert_docked(page, False, True)
        page.screenshot(path=str(OUTPUT / f"{width}-left-hidden.png"))
        page.locator("#toggle-right").click()
        assert_docked(page, False, False)
        page.locator("#toggle-left").click()
        assert_docked(page, True, False)
        page.locator("#toggle-right").click()
        assert_docked(page, True, True)
        expect(page.locator("#question-input")).to_have_value("保留草稿：切换侧栏时聊天仍可用")
        checks.append(f"{width}x{height}: four docked panel combinations and draft")
    page.set_viewport_size({"width": 2505, "height": 1233})
    for left, right in [(False, True), (True, False), (False, False), (True, True)]:
        for name, desired in [("left", left), ("right", right)]:
            if page.locator("#" + name + "-panel").is_visible() != desired:
                page.locator("#toggle-" + name).click()
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
        expect(page.locator("#document-count")).not_to_have_text("0")
        assert_docked(page, left, right)
    checks.append("reload preserves all four combinations without auto-placement")
    for width in [1100, 820, 390, 1920, 1280, 959, 1440]:
        page.set_viewport_size({"width": width, "height": 900})
        if width < 1280:
            if not page.locator("#left-panel").is_visible():
                page.locator("#toggle-left").click()
            expect(page.locator("#left-panel")).to_be_visible()
            page.locator("#toggle-right").click()
            expect(page.locator("#right-panel")).to_be_visible()
            if width < 960:
                expect(page.locator("#left-panel")).to_be_hidden()
            g = geometry(page)
            assert g["right-panel"]["right"] <= width + 1
            page.locator("#toggle-right").click()
            expect(page.locator("#right-panel")).to_be_hidden()
            if width < 960:
                page.locator("#toggle-left").click()
                page.screenshot(path=str(OUTPUT / f"{width}-left-drawer.png"))
                page.locator("#toggle-left").click()
        else:
            g = geometry(page)
            assert_docked(page, g["left-panel"]["visible"], g["right-panel"]["visible"])
    checks.append("resizing across docked/drawer breakpoints")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate(
        "document.documentElement.style.setProperty('--left','400px'); document.documentElement.style.setProperty('--right','560px')"
    )
    for name in ["left", "right"]:
        if not page.locator("#" + name + "-panel").is_visible():
            page.locator("#toggle-" + name).click()
    assert_docked(page, True, True)
    page.locator("#reset-layout").click()
    assert_docked(page, True, True)
    checks.append("oversized saved widths leave a readable chat column and reset")
    page.set_viewport_size({"width": 1920, "height": 1080})
    before = geometry(page)["left-panel"]["w"]
    handle = page.locator("#resize-left").bounding_box()
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 30, y, steps=5)
    page.mouse.up()
    assert geometry(page)["left-panel"]["w"] >= before + 25
    page.locator("#resize-right").focus()
    page.keyboard.press("ArrowLeft")
    assert_docked(page, True, True)
    page.locator("#reset-layout").click()
    checks.append("pointer and keyboard resizing")
    page.locator("#question-input").fill("DOCQA_IMAGE")
    page.locator("#ask-button").click()
    expect(page.locator(".message-actions")).to_have_count(1, timeout=30000)
    assert (
        float(
            page.locator(".answer-body")
            .evaluate("el=>getComputedStyle(el).fontSize")
            .removesuffix("px")
        )
        >= 17
    )
    assert (
        float(
            page.locator(".document-name")
            .first.evaluate("el=>getComputedStyle(el).fontSize")
            .removesuffix("px")
        )
        >= 14
    )
    page.locator(".message-actions").get_by_role("button", name="查看来源").click()
    expect(page.locator(".citation-card").first).to_be_visible()
    assert (
        float(
            page.locator(".citation-preview")
            .first.evaluate("el=>getComputedStyle(el).fontSize")
            .removesuffix("px")
        )
        >= 14
    )
    page.screenshot(path=str(OUTPUT / "desktop-light.png"))
    page.locator("#toggle-left").click()
    assert_docked(page, False, True)
    page.screenshot(path=str(OUTPUT / "desktop-left-hidden-with-answer.png"))
    page.locator("#theme-button").click()
    page.mouse.move(900, 35)
    # Citation rows may be transparent: inspect their effective paper, not an old palette literal.
    color = page.locator(".citation-card").first.evaluate("""el=>{
      for(let p=el;p;p=p.parentElement){
        const color=getComputedStyle(p).backgroundColor;
        if(color!=='rgba(0, 0, 0, 0)')return color;
      }
    }""")
    assert all(int(part) < 100 for part in color.removeprefix("rgb(").removesuffix(")").split(",")), color
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUTPUT / "desktop-dark.png"))
    page.locator("#theme-button").click()
    checks.append("larger text with real demo answer and source cards, light/dark screenshots")
    assert not errors, errors
    browser.close()

report = {"status": "passed", "checks": checks, "browser_errors": errors}
(OUTPUT / "report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False))
