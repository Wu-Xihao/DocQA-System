"""The browser boundary is checked independently of API business handlers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from docqa.web.security import (
    COOKIE_NAME,
    LocalSecurity,
    _client_from_cookie,
    _session_cookie,
    csrf_token,
)

KEY = b"k" * 32
CLIENT_ID = "1" * 32
HOST = b"127.0.0.1:8765"


async def echo(request: Request):
    client_id = request.state.client_id
    payload = {
        "client_id": client_id,
        "token": csrf_token(KEY, client_id) if client_id else None,
    }
    if request.method in {"POST", "PUT", "PATCH"}:
        payload["body"] = (await request.body()).decode("utf-8")
    return JSONResponse(
        payload,
        headers={"cache-control": "public", "x-frame-options": "SAMEORIGIN"},
    )


def application(*, port=8765, max_body_bytes=65536):
    app = Starlette(
        routes=[Route("/{path:path}", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])]
    )
    return LocalSecurity(app, key=KEY, port=port, max_body_bytes=max_body_bytes)


@pytest.fixture
def client():
    with TestClient(application(), base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def bootstrap(client):
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    return {"X-DocQA-Token": response.json()["token"]}


def assert_security(response):
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "access-control-allow-origin" not in response.headers


def raw_request(
    *,
    method="POST",
    path="/api/v1/questions",
    headers=(),
    events=None,
    max_body_bytes=65536,
    app=None,
):
    """Use raw ASGI events so HTTP-client header normalization cannot hide bugs."""
    messages = []
    queue = list(events if events is not None else [{"type": "http.request", "body": b"{}"}])

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(event):
        messages.append(event)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", HOST), *headers],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }
    asyncio.run((app or application(max_body_bytes=max_body_bytes))(scope, receive, send))
    response = messages[0]
    body = b"".join(item.get("body", b"") for item in messages[1:])
    return response, json.loads(body)


def authenticated_headers():
    return [
        (b"cookie", f"{COOKIE_NAME}={_session_cookie(KEY, CLIENT_ID)}".encode()),
        (b"x-docqa-token", csrf_token(KEY, CLIENT_ID).encode()),
        (b"content-type", b"application/json"),
    ]


def test_bootstrap_issues_signed_strict_http_only_cookie_and_state(client):
    first = client.get("/api/v1/bootstrap")
    assert first.status_code == 200
    payload = first.json()
    assert len(payload["client_id"]) == 32
    assert payload["token"] == csrf_token(KEY, payload["client_id"])
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # This listener uses local HTTP.
    assert _client_from_cookie(KEY, client.cookies[COOKIE_NAME]) == payload["client_id"]
    assert_security(first)
    second = client.get("/api/v1/bootstrap")
    assert second.json() == payload
    assert "set-cookie" not in second.headers


def test_root_also_bootstraps_but_static_and_live_do_not(client):
    assert "set-cookie" not in client.get("/api/v1/health/live").headers
    assert "set-cookie" not in client.get("/static/app.js").headers
    assert "set-cookie" in client.get("/").headers


def test_invalid_cookie_rotates_only_at_bootstrap(client):
    client.cookies.set(COOKIE_NAME, "invalid")
    assert client.get("/api/v1/questions").status_code == 401
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    assert "set-cookie" in response.headers


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:8765",
        "127.0.0.1.evil.example:8765",
        "127.0.0.1",
        "localhost",
        "127.0.0.1:8766",
        "localhost:80",
        "127.0.0.1:08765",
        "127.0.0.1:8765,evil.example",
        "127.0.0.1:8765 ",
        "LOCALHOST:8765",
        "[::1]:8765",
    ],
)
def test_dns_rebinding_and_nonexact_host_are_rejected(client, host):
    response = client.get("/api/v1/health/live", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_HOST"
    assert_security(response)


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765"])
def test_configured_loopback_hosts_are_accepted(client, host):
    assert client.get("/api/v1/health/live", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.1:80", "localhost:80"])
def test_port_80_permits_canonical_host_without_port(host):
    with TestClient(application(port=80), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/health/live", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8765",
        "http://127.0.0.1:8765/",
        "http://localhost:8765",
        "http://127.0.0.1:8766",
        "http://evil.example",
        "null",
    ],
)
def test_origin_is_exact_and_matches_current_host(client, origin):
    response = client.get("/api/v1/bootstrap", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
    assert_security(response)


def test_matching_origin_is_accepted(client):
    assert (
        client.get("/api/v1/bootstrap", headers={"Origin": "http://127.0.0.1:8765"}).status_code
        == 200
    )


def test_cross_site_navigation_cannot_bootstrap(client):
    response = client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"]
)
def test_proxy_headers_cannot_override_boundary(client, header):
    response = client.get("/api/v1/health/live", headers={header: "evil.example"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROXY_HEADERS_FORBIDDEN"


def test_options_has_no_cors_preflight(client):
    response = client.options("/api/v1/bootstrap")
    assert response.status_code == 405
    assert_security(response)


def test_every_json_api_read_requires_cookie_and_csrf(client):
    assert client.get("/api/v1/questions").status_code == 401
    headers = bootstrap(client)
    assert client.get("/api/v1/questions").status_code == 403
    assert client.get("/api/v1/questions", headers=headers).status_code == 200
    assert client.get("/api/v1", headers=headers).status_code == 200


def test_tokens_are_session_bound_and_not_cookie_signatures(client):
    bootstrap(client)
    wrong = csrf_token(KEY, "2" * 32)
    assert client.get("/api/v1/questions", headers={"X-DocQA-Token": wrong}).status_code == 403
    cookie_signature = client.cookies[COOKIE_NAME].split(".")[1]
    assert (
        client.get("/api/v1/questions", headers={"X-DocQA-Token": cookie_signature}).status_code
        == 403
    )
    assert client.get("/api/v1/questions?token=" + wrong).status_code == 403


def test_content_exemption_is_get_only_and_still_requires_cookie(client):
    assert client.get("/api/v1/sources/asset/content").status_code == 401
    bootstrap(client)
    assert client.get("/api/v1/sources/asset/content").status_code == 200
    assert client.post("/api/v1/sources/asset/content", json={}).status_code == 403
    assert client.get("/api/v1/sources/asset/contentx").status_code == 403
    assert (
        client.get(
            "/api/v1/sources/asset/content", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_bootstrap_path_is_not_an_auth_exemption(client, method):
    assert client.request(method, "/api/v1/bootstrap", json={}).status_code == 401
    bootstrap(client)
    assert client.request(method, "/api/v1/bootstrap", json={}).status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_json_mutations_receive_the_exact_prebuffered_body(client, method):
    headers = bootstrap(client)
    payload = '{"question":"你好"}'.encode()
    response = client.request(
        method,
        "/api/v1/questions",
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        content=payload,
    )
    assert response.status_code == 200
    assert response.json()["body"].encode() == payload


@pytest.mark.parametrize("content_type", [None, "text/plain", "multipart/form-data", "text/json"])
def test_non_json_body_is_rejected_before_business_handler(client, content_type):
    headers = bootstrap(client)
    if content_type:
        headers["Content-Type"] = content_type
    response = client.post("/api/v1/questions", headers=headers, content=b"{}")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "JSON_REQUIRED"


@pytest.mark.parametrize("value", [b"-1", b"+2", b"2,2", b" 2", b"2 ", b"no", b"", b"9" * 20])
def test_malformed_content_length_is_rejected(value):
    response, payload = raw_request(headers=[*authenticated_headers(), (b"content-length", value)])
    assert response["status"] == 400
    assert payload["error"]["code"] == "INVALID_CONTENT_LENGTH"


@pytest.mark.parametrize("declared", [b"1", b"3"])
def test_content_length_mismatch_does_not_reach_handler(declared):
    response, payload = raw_request(
        headers=[*authenticated_headers(), (b"content-length", declared)]
    )
    assert response["status"] == 400
    assert payload["error"]["code"] == "BODY_LENGTH_MISMATCH"


def test_oversized_declared_body_is_rejected_without_reading():
    response, payload = raw_request(
        headers=[*authenticated_headers(), (b"content-length", b"65537")], events=[]
    )
    assert response["status"] == 413
    assert payload["error"]["code"] == "BODY_TOO_LARGE"


@pytest.mark.parametrize("declared", [None, b"1"])
def test_streaming_size_limit_cannot_be_bypassed_by_missing_or_false_length(declared):
    headers = authenticated_headers()
    if declared is not None:
        headers.append((b"content-length", declared))
    response, payload = raw_request(
        max_body_bytes=4,
        headers=headers,
        events=[
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": False},
        ],
    )
    assert response["status"] == 413
    assert payload["error"]["code"] == "BODY_TOO_LARGE"


def test_exact_streaming_limit_is_accepted():
    response, payload = raw_request(
        max_body_bytes=4,
        headers=authenticated_headers(),
        events=[
            {"type": "http.request", "body": b"12", "more_body": True},
            {"type": "http.request", "body": b"34", "more_body": False},
        ],
    )
    assert response["status"] == 200
    assert payload["body"] == "1234"


def test_disconnected_body_is_not_parsed():
    response, payload = raw_request(headers=authenticated_headers(), events=[])
    assert response["status"] == 400
    assert payload["error"]["code"] == "BODY_INCOMPLETE"


@pytest.mark.parametrize(
    "name,value",
    [
        (b"Host", HOST),
        (b"Origin", b"http://127.0.0.1:8765"),
        (b"Sec-Fetch-Site", b"same-origin"),
        (b"X-DocQA-Token", b"a"),
        (b"Cookie", b"other=value"),
        (b"Content-Length", b"2"),
        (b"Content-Type", b"application/json"),
        (b"Transfer-Encoding", b"chunked"),
    ],
)
def test_duplicate_security_headers_are_rejected_even_with_mixed_case(name, value):
    headers = [(name.lower(), value), (name, value)]
    if name.lower() == b"host":
        headers = [(name, value)]  # raw_request already supplies Host.
    response, payload = raw_request(headers=headers)
    assert response["status"] == 400
    assert payload["error"]["code"] == "DUPLICATE_HEADER"


def test_transfer_encoding_and_length_cannot_coexist():
    response, payload = raw_request(
        headers=[
            *authenticated_headers(),
            (b"transfer-encoding", b"chunked"),
            (b"content-length", b"2"),
        ]
    )
    assert response["status"] == 400
    assert payload["error"]["code"] == "INVALID_CONTENT_LENGTH"


def test_non_ascii_csrf_is_an_auth_error_not_server_error():
    headers = [(name, value) for name, value in authenticated_headers() if name != b"x-docqa-token"]
    response, payload = raw_request(headers=[*headers, (b"x-docqa-token", b"\xff" * 64)])
    assert response["status"] == 403
    assert payload["error"]["code"] == "CSRF_REQUIRED"


def test_duplicate_cookie_names_and_tampered_signatures_fail_closed(client):
    cookie = _session_cookie(KEY, CLIENT_ID)
    for value in [
        cookie[:-1] + ("0" if cookie[-1] != "0" else "1"),
        cookie + ";docqa_session=" + cookie,
    ]:
        response = client.get("/api/v1/questions", headers={"Cookie": "docqa_session=" + value})
        assert response.status_code == 401


def test_signatures_use_separate_domains_and_change_with_key():
    signature = _session_cookie(KEY, CLIENT_ID).split(".")[1]
    assert signature != csrf_token(KEY, CLIENT_ID)
    assert csrf_token(KEY, CLIENT_ID) != csrf_token(b"z" * 32, CLIENT_ID)
    assert _client_from_cookie(b"z" * 32, _session_cookie(KEY, CLIENT_ID)) is None
    assert (
        csrf_token(KEY, CLIENT_ID)
        == hmac.new(KEY, b"docqa.web.csrf.v1:" + CLIENT_ID.encode(), hashlib.sha256).hexdigest()
    )


@pytest.mark.parametrize("key", [b"", b"x" * 31, "not bytes", None])
def test_key_must_have_at_least_32_bytes(key):
    with pytest.raises(ValueError):
        LocalSecurity(None, key=key)


@pytest.mark.parametrize("port", [0, 65536, -1, True, "8765"])
def test_listener_port_must_be_valid(port):
    with pytest.raises(ValueError):
        LocalSecurity(None, key=KEY, port=port)


@pytest.mark.parametrize("limit", [0, -1, True, "65536"])
def test_body_limit_must_be_a_positive_integer(limit):
    with pytest.raises(ValueError):
        LocalSecurity(None, key=KEY, max_body_bytes=limit)


@pytest.mark.parametrize("client_id", ["", "f" * 31, "F" * 32, "../evil", None])
def test_invalid_client_ids_cannot_be_signed(client_id):
    with pytest.raises(ValueError):
        csrf_token(KEY, client_id)


def test_websocket_is_closed_before_application_is_called():
    called = False
    messages = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "websocket.connect"}

    async def send(event):
        messages.append(event)

    asyncio.run(LocalSecurity(app, key=KEY)({"type": "websocket"}, receive, send))
    assert called is False
    assert messages == [{"type": "websocket.close", "code": 1008}]


def test_errors_are_small_stable_and_do_not_echo_request_secrets(client):
    secret = "secret-must-not-be-echoed"
    response = client.post(
        "/api/v1/questions", headers={"X-DocQA-Token": secret}, json={"key": secret}
    )
    assert response.status_code == 401
    assert secret not in response.text
    assert response.json()["schema_version"] == "docqa.web-error.v1"
    assert response.json()["error"]["retryable"] is False
    assert_security(response)
