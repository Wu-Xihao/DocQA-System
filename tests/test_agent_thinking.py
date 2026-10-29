"""Optional provider thinking flags preserve legacy wire and identity defaults."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from docqa.agent.config import ChatServiceOptions, load_options
from docqa.agent.providers import PROMPT_REVISION, HttpChatClient

ENDPOINT = "https://chat.invalid/chat/completions"
PLAN = {
    "intent": "fact",
    "required_topics": [{"topic_id": "T1", "text": "A supported fact"}],
    "exact_terms": [],
    "subqueries": [{"query": "A supported fact", "topic_ids": ["T1"]}],
    "output_format": "markdown",
}


def options(**updates):
    return ChatServiceOptions(
        **{
            "endpoint": ENDPOINT,
            "model": "configured-chat",
            "revision": "deployment-1",
            "retries": 0,
            **updates,
        }
    )


def envelope(value):
    return {
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(value)},
            }
        ]
    }


@pytest.mark.parametrize("role", ["generator", "verifier"])
@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
def test_thinking_is_sent_only_when_explicit_and_does_not_replace_json_contract(role, thinking):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(PLAN))

    client = HttpChatClient(
        options(thinking=thinking), role=role, transport=httpx.MockTransport(handler)
    )
    plan = client.plan("A supported fact", None, {})
    assert plan.required_topics[0].topic_id == "T1"
    assert len(calls) == 1
    payload = calls[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["model"] == "configured-chat"
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == 0
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert (
        json.loads(payload["messages"][1]["content"])["output_schema"]["additionalProperties"]
        is False
    )
    if thinking is None:
        assert set(payload) == {"model", "temperature", "max_tokens", "response_format", "messages"}
    else:
        assert payload["thinking"] == {"type": thinking}
        assert set(payload) == {
            "model",
            "temperature",
            "max_tokens",
            "response_format",
            "messages",
            "thinking",
        }


def test_omitted_thinking_keeps_the_exact_legacy_fingerprint():
    expected = {
        "protocol": "agent.chat-completions.v1",
        "prompt_revision": PROMPT_REVISION,
        "role": "generator",
        "synthetic": False,
        "endpoint_sha256": hashlib.sha256(ENDPOINT.encode()).hexdigest(),
        "model": "configured-chat",
        "revision": "deployment-1",
        "max_input_utf8_bytes": 131072,
        "max_output_tokens": 4096,
        "temperature": 0,
    }
    assert HttpChatClient(options()).fingerprint() == expected
    assert HttpChatClient(options(thinking=None)).fingerprint() == expected


def test_explicit_thinking_choices_have_distinct_identities():
    legacy = HttpChatClient(options()).fingerprint()
    enabled = HttpChatClient(options(thinking="enabled")).fingerprint()
    disabled = HttpChatClient(options(thinking="disabled")).fingerprint()
    assert enabled == {**legacy, "thinking": "enabled"}
    assert disabled == {**legacy, "thinking": "disabled"}
    assert enabled != disabled


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_format_repair_preserves_the_explicit_thinking_flag(thinking):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json=envelope({} if len(calls) == 1 else PLAN))

    client = HttpChatClient(options(thinking=thinking), transport=httpx.MockTransport(handler))
    assert client.plan("A supported fact", None, {}).intent == "fact"
    assert len(calls) == 2
    assert all(payload["thinking"] == {"type": thinking} for payload in calls)
    assert "format_repair" in json.loads(calls[1]["messages"][1]["content"])


@pytest.mark.parametrize("value", [True, False, 1, "auto", " disabled ", {"type": "disabled"}])
def test_invalid_thinking_values_are_rejected_before_a_request(value):
    with pytest.raises(ValidationError):
        options(thinking=value)


def test_yaml_thinking_values_are_loaded_for_both_roles(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(
        "generator:\n  thinking: disabled\nverifier:\n  thinking: enabled\n", encoding="utf-8"
    )
    loaded = load_options(path)
    assert loaded.generator.thinking == "disabled"
    assert loaded.verifier.thinking == "enabled"
