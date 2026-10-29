"""Capture and exercise the workbench using only an isolated synthetic server."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8790")
args = parser.parse_args()
origin = urlsplit(args.url)
if origin.scheme != "http" or origin.hostname != "127.0.0.1" or origin.path not in {"", "/"}:
    parser.error("Use an isolated loopback synthetic server")
base = args.url.rstrip("/")
output = Path("runtime/visual-design-check")
output.mkdir(parents=True, exist_ok=True)
errors, checks = [], []


def screenshot(page, name):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=str(output / (name + ".png")), animations="disabled")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True
    )
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    context.route(
        "**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort()
    )
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    assert context.request.get(base + "/api/v1/bootstrap").json()["settings"]["synthetic"]
    expect(page.locator("html")).to_have_attribute("data-app-state", "bound")
    expect(page.locator(".document-name").first).to_be_visible()
    page.locator("#new-chat").click()
    expect(page.locator("#empty-state")).to_be_visible()
    screenshot(page, "desktop-empty")
    for width, height in [(1440, 900), (1280, 720), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        screenshot(page, f"empty-{width}")
        assert page.locator("#toggle-left").bounding_box()["width"] > 45
        assert page.locator("#toggle-right").bounding_box()["width"] > 45
    page.locator("#toggle-left").click()
    expect(page.locator("#dropzone")).to_be_visible()
    with page.expect_file_chooser():
        page.locator("#dropzone").press("Enter")
    screenshot(page, "mobile-library")
    page.locator("#toggle-right").click()
    expect(page.locator("#right-panel")).to_be_visible()
    expect(page.locator("#left-panel")).to_be_hidden()
    screenshot(page, "mobile-sources")
    page.locator("#close-right").click()
    expect(page.locator("#right-panel")).to_be_hidden()
    checks.append("390px named drawer controls and keyboard upload remain usable")

    page.set_viewport_size({"width": 1920, "height": 1080})
    for name in ["left", "right"]:
        if not page.locator(f"#{name}-panel").is_visible():
            page.locator(f"#toggle-{name}").click()
    page.locator("#theme-button").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    screenshot(page, "desktop-dark-empty")
    # Normal-size key reading text must meet WCAG AA contrast, in both themes.
    for theme in ["dark", "light"]:
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#theme-button").click()
        ratios = page.evaluate("""() => {
          const s=getComputedStyle(document.documentElement);
          const lum=hex=>{
            const rgb=hex.trim().replace('#','').match(/../g).map(x=>parseInt(x,16)/255)
              .map(c=>c<=.04045?c/12.92:((c+.055)/1.055)**2.4);
            return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
          };
          return ['--surface','--subtle','--bg'].flatMap(background=>
            ['--text','--muted','--accent'].map(key=>{
              const a=lum(s.getPropertyValue(key)), b=lum(s.getPropertyValue(background));
              return [key+' on '+background,(Math.max(a,b)+.05)/(Math.min(a,b)+.05)];
            }));
        }""")
        assert all(ratio >= 4.5 for _, ratio in ratios), (theme, ratios)
    checks.append("light/dark text and links meet AA contrast on paper, rails and workspace")

    page.locator("#question-input").fill("DOCQA_IMAGE")
    page.locator("#question-input").press("Enter")
    expect(page.locator(".message-actions")).to_have_count(1, timeout=30000)
    expect(page.locator(".citation-card").first).to_be_visible()
    assert page.locator(".answer-body").first.evaluate("e=>parseFloat(getComputedStyle(e).fontSize)") >= 19
    screenshot(page, "desktop-answer")
    page.locator(".answer-body .citation-chip").first.click()
    expect(page.locator("#preview-dialog")).to_be_visible()
    expect(page.locator(".reader-block").first).to_be_visible(timeout=30000)
    screenshot(page, "citation-reader")
    page.get_by_role("tab", name="Markdown", exact=True).click()
    expect(page.locator(".reader-markdown")).to_be_visible()
    screenshot(page, "markdown-reader")
    page.get_by_role("tab", name="全部图片", exact=True).click()
    expect(page.locator(".reader-image-card")).to_have_count(2)
    image = page.locator(".reader-image-card img").first
    expect(image).to_have_js_property("complete", True)
    assert image.evaluate("img=>img.naturalWidth>0")
    screenshot(page, "source-gallery")
    page.locator(".reader-image-card").first.get_by_role("button", name="放大查看").click()
    expect(page.locator(".reader-image-dialog")).to_be_visible()
    page.locator(".reader-image-dialog").get_by_role("button", name="关闭", exact=True).click()
    page.locator("#close-preview").click()
    checks.append("19px answer, citation, original/Markdown reader, image gallery and zoom work")

    page.locator('[data-tab="studio"]').click()
    screenshot(page, "desktop-studio")
    for label, selector, filename in [
        ("思维导图", ".mindmap svg", "mindmap"),
        ("演示文稿", ".slide-page", "slides"),
    ]:
        page.locator(".studio-action").filter(has_text=label).click()
        expect(page.locator("#preview-dialog")).to_be_visible(timeout=30000)
        expect(page.locator(selector).first).to_be_visible(timeout=30000)
        screenshot(page, filename)
        page.locator("#close-preview").click()
    page.locator("#theme-button").click()
    screenshot(page, "desktop-dark-answer")
    checks.append("mindmap and presentation previews keep existing functionality")

    page.set_viewport_size({"width": 390, "height": 844})
    if page.locator("#right-panel").is_visible():
        page.locator("#close-right").click()
    screenshot(page, "mobile-dark-answer")
    if page.locator("#toast").is_visible():
        toast_box = page.locator("#toast").bounding_box()
        composer_box = page.locator("#question-form").bounding_box()
        assert toast_box["y"] + toast_box["height"] < composer_box["y"]
    page.emulate_media(reduced_motion="reduce")
    assert page.locator("#theme-button").evaluate("e=>getComputedStyle(e).transitionDuration") == "0s"
    checks.append("reduced-motion preference respected, no page overflow")
    assert not errors, errors
    browser.close()

report = {"status": "passed", "checks": checks, "browser_errors": errors}
(output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=True))
