"""Parse shipped ES modules explicitly; Node's .js auto-detection can miss errors."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docqa.web.app import create_app
from docqa.web.config import WebOptions

STATIC = Path(__file__).resolve().parents[1] / "src/docqa/web/static"


@pytest.mark.parametrize("source", sorted(STATIC.rglob("*.js")), ids=lambda p: p.name)
def test_frontend_module_syntax(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional and is only needed for frontend development checks")
    checked = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=source.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    )
    assert checked.returncode == 0, f"{source.name}: {checked.stderr}"


def test_static_assets_keep_executable_types_with_bad_windows_mime_map(tmp_path, monkeypatch):
    import mimetypes

    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    monkeypatch.setitem(mimetypes.types_map, ".css", "application/octet-stream")
    app = create_app(WebOptions(state=tmp_path / "web", index=tmp_path / "index"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for source in STATIC.rglob("*"):
            if source.suffix not in {".js", ".css"}:
                continue
            url = "/static/" + source.relative_to(STATIC).as_posix()
            response = client.get(url)
            expected = "text/javascript" if source.suffix == ".js" else "text/css"
            assert response.status_code == 200, url
            assert response.headers["content-type"].split(";")[0] == expected, url
            assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/static/absent.js").status_code == 404


def test_initial_page_does_not_allow_native_question_submission(tmp_path):
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def handle_starttag(self, tag, attrs):
            value = dict(attrs)
            if tag == "button" and value.get("id") == "ask-button":
                assert "disabled" in value
                self.found = True

    parsed = Page()
    parsed.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert parsed.found
