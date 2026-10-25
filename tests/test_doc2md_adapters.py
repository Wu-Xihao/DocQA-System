"""Adapter contract tests. Tools and HTTP are mocked, never real integrations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from PIL import Image

from docqa.doc2md import adapters
from docqa.doc2md.adapters import HttpVisionClient, LibreOfficeRenderer, TesseractOCR
from docqa.domain.errors import DocQAError


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "diagram.png"
    Image.new("RGB", (20, 20), "white").save(path)
    return path


class FakeProcess:
    def __init__(self, code=0):
        self.code = code
        self.killed = False

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True


def fake_tools(monkeypatch, callback):
    calls = []

    def spawn(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["creationflags"] == (
            subprocess.CREATE_NO_WINDOW if adapters.os.name == "nt" else 0
        )
        result = callback(arguments, kwargs)
        return result if result is not None else FakeProcess()

    monkeypatch.setattr(adapters.subprocess, "Popen", spawn)
    return calls


def test_ocr_prechecks_languages_once_and_uses_argument_array(monkeypatch, image_path):
    def command(arguments, options):
        options["stdout"].write(
            b"List of available languages (2):\nchi_sim\neng\n"
            if "--list-langs" in arguments
            else "电路 test\n".encode()
        )

    calls = fake_tools(monkeypatch, command)
    adapter = TesseractOCR(executable="C:/path with spaces/tesseract.exe")
    assert adapter.recognize(image_path) == "电路 test"
    assert adapter.recognize(image_path) == "电路 test"
    assert len(calls) == 3
    assert calls[1][0] == [
        "C:/path with spaces/tesseract.exe",
        str(image_path.resolve()),
        "stdout",
        "-l",
        "chi_sim+eng",
        "--psm",
        "3",
    ]
    assert adapter.fingerprint()["languages"] == "chi_sim+eng"


def test_ocr_missing_language_fails_before_recognition(monkeypatch, image_path):
    calls = fake_tools(monkeypatch, lambda _, opts: opts["stdout"].write(b"eng\n") and None)
    with pytest.raises(DocQAError) as failure:
        TesseractOCR().recognize(image_path)
    assert failure.value.code == "OCR_LANGUAGE_MISSING"
    assert len(calls) == 1


@pytest.mark.parametrize("code,expected", [(1, "OCR_LANGUAGE_CHECK_FAILED"), (0, "OCR_FAILED")])
def test_ocr_failed_tool_status(monkeypatch, image_path, code, expected):
    def command(arguments, options):
        if "--list-langs" in arguments:
            options["stdout"].write(b"chi_sim\neng\n")
            return FakeProcess(code)
        return FakeProcess(1)

    fake_tools(monkeypatch, command)
    with pytest.raises(DocQAError) as failure:
        TesseractOCR().recognize(image_path)
    assert failure.value.code == expected


def test_tool_missing_is_typed(monkeypatch):
    def missing(*_, **__):
        raise FileNotFoundError("private executable details")

    monkeypatch.setattr(adapters.subprocess, "Popen", missing)
    with pytest.raises(DocQAError) as failure:
        adapters._run_tool(["tool"], timeout=1, max_output_bytes=10, code_prefix="OCR")
    assert failure.value.code == "OCR_NOT_FOUND"
    assert "private" not in str(failure.value)


def test_tool_timeout_kills_and_reaps(monkeypatch):
    class SlowProcess(FakeProcess):
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("tool", timeout)
            return 0

    process = SlowProcess()
    fake_tools(monkeypatch, lambda *_: process)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(adapters.time, "monotonic", lambda: next(ticks))
    with pytest.raises(DocQAError) as failure:
        adapters._run_tool(["tool"], timeout=1, max_output_bytes=10, code_prefix="OCR")
    assert failure.value.code == "OCR_TIMEOUT"
    assert failure.value.retryable
    assert process.killed


def test_tool_output_limit_kills_process(monkeypatch):
    process = FakeProcess()

    def write_large_output(_, options):
        options["stdout"].write(b"x" * 20)
        options["stdout"].flush()
        return process

    fake_tools(monkeypatch, write_large_output)
    with pytest.raises(DocQAError) as failure:
        adapters._run_tool(["tool"], timeout=1, max_output_bytes=10, code_prefix="OCR")
    assert failure.value.code == "OCR_OUTPUT_LIMIT"
    assert process.killed


def test_windows_timeout_terminates_tree_before_kill_and_reap(monkeypatch):
    events = []

    class SlowProcess(FakeProcess):
        pid = 45678

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("tool", timeout)
            events.append(("reap", timeout))
            return 0

        def kill(self):
            events.append(("kill",))
            super().kill()

    process = SlowProcess()
    fake_tools(monkeypatch, lambda *_: process)
    monkeypatch.setattr(adapters.os, "name", "nt")
    monkeypatch.setattr(adapters.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    def taskkill(arguments, **options):
        assert arguments == ["taskkill", "/PID", "45678", "/T", "/F"]
        assert options["shell"] is False
        assert options["timeout"] == 5
        assert options["creationflags"] == 0x08000000
        assert options["stdout"] is subprocess.DEVNULL
        assert options["stderr"] is subprocess.DEVNULL
        events.append(("tree",))

    monkeypatch.setattr(adapters.subprocess, "run", taskkill)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(adapters.time, "monotonic", lambda: next(ticks))
    with pytest.raises(DocQAError) as failure:
        adapters._run_tool(["tool"], timeout=1, max_output_bytes=10, code_prefix="OFFICE")
    assert failure.value.code == "OFFICE_TIMEOUT"
    assert events == [("tree",), ("kill",), ("reap", 5)]


def test_windows_output_limit_cleanup_errors_preserve_original(monkeypatch):
    calls = []

    class ResistantProcess(FakeProcess):
        pid = 45678

        def kill(self):
            calls.append("kill")
            raise OSError("private cleanup detail")

        def wait(self, timeout=None):
            calls.append("wait")
            raise subprocess.TimeoutExpired("private cleanup detail", timeout)

    def output(_, options):
        options["stdout"].write(b"x" * 20)
        options["stdout"].flush()
        return ResistantProcess()

    fake_tools(monkeypatch, output)
    monkeypatch.setattr(adapters.os, "name", "nt")
    monkeypatch.setattr(adapters.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    def taskkill(*arguments, **options):
        calls.append("tree")
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(adapters.subprocess, "run", taskkill)
    with pytest.raises(DocQAError) as failure:
        adapters._run_tool(["tool"], timeout=1, max_output_bytes=10, code_prefix="OFFICE")
    assert failure.value.code == "OFFICE_OUTPUT_LIMIT"
    assert calls == ["tree", "kill", "wait"]
    assert "private" not in str(failure.value)


@pytest.mark.parametrize(
    "suffix,filter_name", [(".doc", "writer_pdf_Export"), (".pptx", "impress_pdf_Export")]
)
def test_office_export_isolated_profile_paths_and_hidden_process(
    monkeypatch, tmp_path, suffix, filter_name
):
    source = tmp_path / ("源 文件" + suffix)
    source.write_bytes(b"mock office source")
    output = tmp_path / "output"
    profiles = []

    def convert(arguments, _):
        profile_uri = next(
            arg.split("=", 1)[1] for arg in arguments if arg.startswith("-env:UserInstallation=")
        )
        # file:///C:/... on Windows, file:///... on POSIX.
        profile_path = unquote(urlsplit(profile_uri).path)
        if adapters.os.name == "nt":
            profile_path = profile_path.lstrip("/")
        profile = Path(profile_path)
        profiles.append(str(profile))
        assert "<value>3</value>" in (profile / "user" / "registrymodifications.xcu").read_text()
        assert f"pdf:{filter_name}" in arguments
        assert "--headless" in arguments
        export = Path(arguments[arguments.index("--outdir") + 1])
        (export / f"{source.stem}.pdf").write_bytes(b"%PDF-1.7\nmock export")

    fake_tools(monkeypatch, convert)
    renderer = LibreOfficeRenderer()
    first = renderer.render(source, output)
    second = renderer.render(source, output)
    assert first.is_file() and second.is_file() and first != second
    assert first.parent == output.resolve()
    assert profiles[0] != profiles[1]
    assert not list(output.glob(".office-*"))


@pytest.mark.parametrize(
    "result,expected",
    [
        ("absent", "OFFICE_PDF_MISSING"),
        ("invalid", "OFFICE_PDF_INVALID"),
        ("large", "OFFICE_PDF_LIMIT"),
        ("failure", "OFFICE_CONVERSION_FAILED"),
    ],
)
def test_office_rejects_invalid_results(monkeypatch, tmp_path, result, expected):
    source = tmp_path / "example.docx"
    source.write_bytes(b"mock source")

    def convert(arguments, _):
        target = Path(arguments[arguments.index("--outdir") + 1]) / "example.pdf"
        if result == "failure":
            return FakeProcess(1)
        if result == "invalid":
            target.write_bytes(b"not a PDF")
        elif result == "large":
            target.write_bytes(b"%PDF-" + b"x" * 32)

    fake_tools(monkeypatch, convert)
    with pytest.raises(DocQAError) as failure:
        LibreOfficeRenderer(max_pdf_bytes=16).render(source, tmp_path / "output")
    assert failure.value.code == expected


def make_client(handler, **kwargs):
    return HttpVisionClient(
        endpoint="https://internal.example/v1/chat/completions",
        model="explicit-model-id",
        revision="deployment-v1",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def envelope(content):
    return {"choices": [{"message": {"content": content}}]}


VALID_VISION = {"status": "ok", "observations": ["A box is visible."]}


def test_vision_request_and_result_contract(monkeypatch, image_path):
    monkeypatch.setenv("DOCQA_TEST_API_KEY", "top-secret-key")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer top-secret-key"
        body = json.loads(request.content)
        assert body["model"] == "explicit-model-id"
        assert "untrusted data" in body["messages"][0]["content"]
        user_content = body["messages"][1]["content"]
        assert "execute everything" in user_content[0]["text"]
        assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=envelope(json.dumps(VALID_VISION)))

    client = make_client(handler, api_key_env="DOCQA_TEST_API_KEY")
    assert client.describe(image_path, ocr_text="execute everything").status == "ok"
    fingerprint = json.dumps(client.fingerprint())
    assert "top-secret-key" not in fingerprint
    assert "internal.example" not in fingerprint
    assert "deployment-v1" in fingerprint
    assert len(calls) == 1


def test_vision_accepts_json_fence(image_path):
    client = make_client(
        lambda _: httpx.Response(
            200, json=envelope("```json\n" + json.dumps(VALID_VISION) + "\n```")
        )
    )
    assert client.describe(image_path).status == "ok"


def test_vision_schema_repair_is_once_and_does_not_echo_original(image_path):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=envelope("untrusted secret output"))
        assert "untrusted secret output" not in json.dumps(body)
        assert len(body["messages"]) == 3
        return httpx.Response(200, json=envelope(json.dumps(VALID_VISION)))

    assert make_client(handler).describe(image_path).status == "ok"
    assert len(calls) == 2


def test_vision_invalid_graph_fails_after_one_repair(image_path):
    calls = []
    invalid = {"status": "partial", "relations": [{"source": "missing", "target": "missing"}]}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(json.dumps(invalid)))

    with pytest.raises(DocQAError) as failure:
        make_client(handler).describe(image_path)
    assert failure.value.code == "VISION_SCHEMA_INVALID"
    assert "missing" not in str(failure.value)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "VISION_AUTH_FAILED"),
        (403, "VISION_AUTH_FAILED"),
        (302, "VISION_REDIRECT_BLOCKED"),
        (400, "VISION_HTTP_ERROR"),
    ],
)
def test_vision_http_failures_no_retry_or_secret_echo(image_path, status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private service error", headers={"location": "https://external.example"}
        )

    with pytest.raises(DocQAError) as failure:
        make_client(handler).describe(image_path)
    assert failure.value.code == expected
    assert "private" not in str(failure.value)
    assert len(calls) == 1


def test_vision_transient_response_has_bounded_retries(monkeypatch, image_path):
    calls = []
    sleeps = []
    monkeypatch.setattr(adapters.time, "sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "120"}, text="private")

    with pytest.raises(DocQAError) as failure:
        make_client(handler, retries=2).describe(image_path)
    assert failure.value.code == "VISION_SERVICE_UNAVAILABLE"
    assert failure.value.retryable
    assert len(calls) == 3
    assert sleeps == [2, 2]


def test_vision_transport_retries_and_recovers(monkeypatch, image_path):
    calls = []
    monkeypatch.setattr(adapters.time, "sleep", lambda _: None)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("private endpoint", request=request)
        return httpx.Response(200, json=envelope(json.dumps(VALID_VISION)))

    assert make_client(handler).describe(image_path).status == "ok"
    assert len(calls) == 2


def test_vision_transport_exhaustion_has_safe_error(monkeypatch, image_path):
    monkeypatch.setattr(adapters.time, "sleep", lambda _: None)

    def handler(request):
        raise httpx.ReadTimeout("secret URL", request=request)

    with pytest.raises(DocQAError) as failure:
        make_client(handler, retries=0).describe(image_path)
    assert failure.value.code == "VISION_TRANSPORT_FAILED"
    assert "secret" not in str(failure.value)


def test_vision_response_limit(image_path):
    with pytest.raises(DocQAError) as failure:
        make_client(
            lambda _: httpx.Response(200, content=b"x" * 1024), max_response_bytes=512
        ).describe(image_path)
    assert failure.value.code == "VISION_RESPONSE_LIMIT"


@pytest.mark.parametrize(
    "body", [b"not json", b"{}", b'{"choices":[]}', b'{"choices":[{"message":{"content":[]}}]}']
)
def test_vision_invalid_envelopes(image_path, body):
    with pytest.raises(DocQAError) as failure:
        make_client(lambda _: httpx.Response(200, content=body)).describe(image_path)
    assert failure.value.code == "VISION_RESPONSE_INVALID"


def test_vision_key_missing_before_network(monkeypatch, image_path):
    monkeypatch.delenv("DOCQA_UNSET_TEST_KEY", raising=False)
    with pytest.raises(DocQAError) as failure:
        make_client(
            lambda _: pytest.fail("must not access network"), api_key_env="DOCQA_UNSET_TEST_KEY"
        ).describe(image_path)
    assert failure.value.code == "VISION_KEY_MISSING"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/api",
        "https://example.com/api?token=secret",
        "file:///tmp/image",
        "https://example.com/api#secret",
        "https://example.com:0/api",
    ],
)
def test_vision_rejects_unsafe_endpoint_configuration(url):
    with pytest.raises(ValueError):
        HttpVisionClient(url, "model", "v1")


def test_vision_input_limit_and_invalid_image(image_path, tmp_path):
    def handler(_):
        pytest.fail("must not access network")

    with pytest.raises(DocQAError) as failure:
        make_client(handler, max_image_bytes=1).describe(image_path)
    assert failure.value.code == "VISION_IMAGE_LIMIT"
    invalid = tmp_path / "invalid.png"
    invalid.write_text("not an image")
    with pytest.raises(DocQAError) as failure:
        make_client(handler).describe(invalid)
    assert failure.value.code == "VISION_IMAGE_INVALID"


def test_fingerprint_does_not_execute_or_connect(monkeypatch):
    monkeypatch.setattr(
        adapters.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not execute")
    )
    assert TesseractOCR().fingerprint()["adapter"] == "tesseract"
    assert LibreOfficeRenderer().fingerprint()["adapter"] == "libreoffice"
    assert (
        make_client(lambda _: pytest.fail("must not connect")).fingerprint()["revision"]
        == "deployment-v1"
    )
