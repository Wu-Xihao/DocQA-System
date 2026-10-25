"""Single-user loopback HTTP boundary, without trusting browser-provided paths.

This is deliberately pure ASGI: the request-size limit is applied while reading
the stream, before FastAPI or a multipart/JSON parser can consume the body.
Cookies identify local browser sessions; they are not multi-user authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

ASGIApp = Callable[[dict[str, Any], Callable, Callable], Awaitable[None]]
COOKIE_NAME = "docqa_session"
_CLIENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_COOKIE_VALUE = re.compile(r"([a-f0-9]{32})\.([a-f0-9]{64})\Z")
_SENSITIVE_HEADERS = frozenset(
    {
        b"host",
        b"origin",
        b"sec-fetch-site",
        b"x-docqa-token",
        b"cookie",
        b"content-length",
        b"content-type",
        b"transfer-encoding",
    }
)
_SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        b"base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
)
_SECURITY_NAMES = frozenset(name for name, _ in _SECURITY_HEADERS)


def _require_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("The local session signing key must contain at least 32 bytes.")


def _signature(key: bytes, purpose: bytes, client_id: str) -> str:
    _require_key(key)
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
        raise ValueError("Invalid local client ID.")
    return hmac.new(key, purpose + b":" + client_id.encode("ascii"), hashlib.sha256).hexdigest()


def csrf_token(key: bytes, client_id: str) -> str:
    """Return the domain-separated, session-bound token for an API header."""
    return _signature(key, b"docqa.web.csrf.v1", client_id)


def _session_cookie(key: bytes, client_id: str) -> str:
    """Sign one client ID (the value, rather than the Set-Cookie header)."""
    return client_id + "." + _signature(key, b"docqa.web.session.v1", client_id)


def _client_from_cookie(key: bytes, value: str) -> str | None:
    match = _COOKIE_VALUE.fullmatch(value)
    if match is None:
        return None
    client_id, supplied = match.groups()
    expected = _signature(key, b"docqa.web.session.v1", client_id)
    return client_id if hmac.compare_digest(supplied, expected) else None


def _cookie_client(key: bytes, raw: bytes) -> str | None:
    try:
        cookie = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    values = []
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            if not separator:
                return None
            values.append(value)
    # Do not let different cookie parsers choose different session identities.
    if len(values) != 1:
        return None
    return _client_from_cookie(key, values[0])


class LocalSecurity:
    """Enforce an exact local origin, signed sessions and bounded JSON requests.

    ``key`` must be loaded from the server's persistent secret, never generated
    per request. ``port`` is the actual listener port; proxy headers are refused.
    The application obtains the validated identity at ``scope['state']['client_id']``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        key: bytes,
        port: int = 8765,
        max_body_bytes: int = 65536,
    ) -> None:
        _require_key(key)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("The local listener port must be between 1 and 65535.")
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes <= 0
        ):
            raise ValueError("The request-body limit must be a positive integer.")
        self.app = app
        self.key = key
        self.max_body_bytes = max_body_bytes
        self.allowed_hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        if port == 80:
            self.allowed_hosts.update({b"127.0.0.1", b"localhost"})

    async def _error(self, send: Callable, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {
                "schema_version": "docqa.web-error.v1",
                "error": {"code": code, "message": message, "retryable": False},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    *_SECURITY_HEADERS,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers: dict[bytes, bytes] = {}
        for raw_name, value in scope.get("headers", []):
            name = raw_name.lower()
            if name in _SENSITIVE_HEADERS and name in headers:
                await self._error(send, 400, "DUPLICATE_HEADER", "Duplicate security header.")
                return
            if (name == b"forwarded" or name.startswith(b"x-forwarded-")) and value:
                await self._error(
                    send, 400, "PROXY_HEADERS_FORBIDDEN", "Proxy headers are disabled."
                )
                return
            headers[name] = value

        host = headers.get(b"host", b"")
        if host not in self.allowed_hosts:
            await self._error(
                send, 400, "INVALID_HOST", "Use the configured loopback HTTP address."
            )
            return
        if b"origin" in headers and headers[b"origin"] != b"http://" + host:
            await self._error(send, 403, "ORIGIN_FORBIDDEN", "The request origin is not allowed.")
            return
        if headers.get(b"sec-fetch-site", b"").lower() == b"cross-site":
            await self._error(
                send, 403, "CROSS_SITE_FORBIDDEN", "Cross-site requests are disabled."
            )
            return

        method = scope.get("method", "GET").upper()
        if method == "OPTIONS":
            await self._error(send, 405, "OPTIONS_FORBIDDEN", "Cross-origin preflight is disabled.")
            return

        content_length = headers.get(b"content-length")
        length: int | None = None
        if content_length is not None:
            if (
                len(content_length) > 19
                or re.fullmatch(rb"[0-9]+", content_length) is None
                or b"transfer-encoding" in headers
            ):
                await self._error(
                    send, 400, "INVALID_CONTENT_LENGTH", "Invalid body length header."
                )
                return
            length = int(content_length)

        path = scope.get("path", "")
        bootstrap = method == "GET" and path in {"/", "/api/v1/bootstrap"}
        live = method == "GET" and path == "/api/v1/health/live"
        api = path == "/api/v1" or path.startswith("/api/v1/")
        client_id = _cookie_client(self.key, headers.get(b"cookie", b""))
        issue_cookie = bootstrap and client_id is None
        if issue_cookie:
            client_id = secrets.token_hex(16)
        scope.setdefault("state", {})["client_id"] = client_id

        if api and not bootstrap and not live:
            if client_id is None:
                await self._error(
                    send, 401, "SESSION_REQUIRED", "Open the local application first."
                )
                return
            content = method == "GET" and path.endswith("/content")
            if not content:
                supplied = headers.get(b"x-docqa-token", b"")
                expected = csrf_token(self.key, client_id).encode("ascii")
                if not hmac.compare_digest(supplied, expected):
                    await self._error(
                        send, 403, "CSRF_REQUIRED", "A valid session token is required."
                    )
                    return

        buffered: bytes | None = None
        if method in {"POST", "PUT", "PATCH"}:
            media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if media_type != b"application/json":
                await self._error(
                    send, 415, "JSON_REQUIRED", "The request must use application/json."
                )
                return
            if length is not None and length > self.max_body_bytes:
                await self._error(send, 413, "BODY_TOO_LARGE", "The request body is too large.")
                return
            parts = []
            size = 0
            while True:
                event = await receive()
                if event["type"] != "http.request":
                    await self._error(
                        send, 400, "BODY_INCOMPLETE", "The request body is incomplete."
                    )
                    return
                part = event.get("body", b"")
                size += len(part)
                if size > self.max_body_bytes:
                    await self._error(send, 413, "BODY_TOO_LARGE", "The request body is too large.")
                    return
                # Empty ASGI chunks must not grow the buffer's list without
                # consuming the byte budget. Nonempty chunks number at most
                # max_body_bytes, even with an adversarial one-byte stream.
                if part:
                    parts.append(part)
                if not event.get("more_body", False):
                    break
            if length is not None and length != size:
                await self._error(
                    send, 400, "BODY_LENGTH_MISMATCH", "The body length does not match."
                )
                return
            buffered = b"".join(parts)

        replayed = False

        async def secured_receive() -> dict[str, Any]:
            nonlocal replayed
            if buffered is not None and not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        async def secured_send(event: dict[str, Any]) -> None:
            if event["type"] == "http.response.start":
                event = dict(event)
                response_headers = [
                    (name, value)
                    for name, value in event.get("headers", [])
                    if name.lower() not in _SECURITY_NAMES
                ]
                response_headers.extend(_SECURITY_HEADERS)
                if issue_cookie:
                    cookie = (
                        f"{COOKIE_NAME}={_session_cookie(self.key, client_id)}; "
                        "Path=/; HttpOnly; SameSite=Strict"
                    )
                    response_headers.append((b"set-cookie", cookie.encode("ascii")))
                event["headers"] = response_headers
            await send(event)

        await self.app(scope, secured_receive, secured_send)
