"""Bounded provider SSE transport; incomplete streams never become saved replies."""

import json
import os

import httpx

from docqa.agent.providers import _remaining
from docqa.domain.errors import DocQAError


def converse_stream(client, messages, deadline):
    config = client.config
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": config.max_output_tokens,
        "stream": True,
    }
    if config.thinking is not None:
        payload["thinking"] = {"type": config.thinking}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(raw) > config.max_input_utf8_bytes:
        raise DocQAError("MODEL_INPUT_TOO_LARGE", "Chat input exceeds budget")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if config.api_key_env:
        key = os.environ.get(config.api_key_env)
        if not key or not key.strip():
            raise DocQAError("MODEL_CREDENTIAL_MISSING", "Chat credential is unavailable")
        headers["Authorization"] = "Bearer " + key
    stopped, done, total, size = False, False, 0, 0
    pending, event = bytearray(), []
    try:
        with httpx.Client(
            transport=client.transport,
            timeout=min(config.timeout, _remaining(deadline)),
            follow_redirects=False,
            trust_env=False,
        ) as session:
            with session.stream("POST", config.endpoint, content=raw, headers=headers) as response:
                if response.status_code != 200:
                    raise DocQAError(
                        "MODEL_SERVICE_HTTP_ERROR", "Streaming service rejected request"
                    )
                for chunk in response.iter_bytes():
                    _remaining(deadline)
                    total += len(chunk)
                    if total > min(config.max_response_bytes * 16, 4 * 1024 * 1024):
                        raise DocQAError(
                            "MODEL_RESPONSE_INVALID", "Streaming response exceeds limit"
                        )
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                        line = line.rstrip(b"\r")
                        if line.startswith(b"data:"):
                            event.append(line[5:].lstrip(b" "))
                        elif not line and event:
                            data = b"\n".join(event)
                            event = []
                            if data == b"[DONE]":
                                done = True
                                if not stopped:
                                    raise DocQAError(
                                        "MODEL_RESPONSE_INVALID",
                                        "Stream ended without successful completion",
                                    )
                                break
                            value = json.loads(data)
                            choices = value.get("choices")
                            if choices == [] and "usage" in value:
                                continue
                            if (
                                not isinstance(choices, list)
                                or len(choices) != 1
                                or choices[0].get("index") != 0
                            ):
                                raise ValueError("Invalid stream choice")
                            choice = choices[0]
                            delta = choice.get("delta", {})
                            if (
                                delta.get("tool_calls")
                                or delta.get("function_call")
                                or delta.get("refusal")
                                or delta.get("role", "assistant") != "assistant"
                            ):
                                raise ValueError("Unexpected stream command")
                            text = delta.get("content") or ""
                            if not isinstance(text, str) or (stopped and text):
                                raise ValueError("Invalid stream content")
                            if text:
                                size += len(text.encode("utf-8"))
                                if size > min(config.max_response_bytes, 96000):
                                    raise ValueError("Stream content exceeds budget")
                                yield text
                            reason = choice.get("finish_reason")
                            if reason is not None:
                                if reason != "stop":
                                    raise ValueError("Truncated or unsuccessful stream")
                                stopped = True
                    if done:
                        break
                    if len(pending) > 65536 or sum(map(len, event)) > 65536:
                        raise ValueError("Streaming event exceeds budget")
    except httpx.TimeoutException as error:
        _remaining(deadline)
        raise DocQAError("MODEL_SERVICE_TIMEOUT", "Streaming model timed out") from error
    except httpx.TransportError as error:
        raise DocQAError("MODEL_SERVICE_UNAVAILABLE", "Streaming connection failed") from error
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise DocQAError("MODEL_RESPONSE_INVALID", "Invalid streaming response") from error
    _remaining(deadline)
    if not done or not stopped or not size:
        raise DocQAError("MODEL_RESPONSE_INVALID", "Incomplete streaming response")
