"""The Anthropic adapter against the real SDK with a mock transport (no network)."""

import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from confluence_mirror.extraction.provider import AnthropicProvider, ModelSettings  # noqa: E402

KEY = "sk-ant-synthetic-test-key-000000"
REQUEST = {
    "model": "claude-haiku-5-5",
    "max_tokens": 1000,
    "system": "sys",
    "messages": [{"role": "user", "content": "hi"}],
    "output_config": {
        "effort": "medium",
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"records": {"type": "array"}},
                "required": ["records"],
                "additionalProperties": False,
            },
        },
    },
}


def message(content, stop="end_turn", **extra):
    return {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 40},
        **extra,
    }


def provider(monkeypatch, handler):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(wrapped))
    return AnthropicProvider(ModelSettings(max_retries=0), http_client=client), seen


def test_success_keeps_final_text_only_and_captures_the_sent_body(monkeypatch):
    def handler(request):
        return httpx2.Response(
            200,
            json=message(
                [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": json.dumps({"records": []})},
                ]
            ),
            headers={"request-id": "req_synthetic"},
        )

    p, seen = provider(monkeypatch, handler)
    out = p.send(REQUEST)
    assert out.status == "succeeded" and out.parsed == {"records": []}
    assert out.usage["input_tokens"] == 120
    assert out.response_meta["request_id"] == "req_synthetic"
    assert out.response_meta["content_types"] == ["thinking", "text"]
    assert out.text == json.dumps({"records": []})  # thinking is never retained
    assert out.sent_body == json.loads(seen[0].content)
    assert out.sent_body["output_config"]["effort"] == "medium"
    headers = {k.lower() for k in out.request_context["headers"]}
    assert "x-api-key" not in headers and "authorization" not in headers
    assert KEY not in json.dumps(out.request_context) + json.dumps(out.sent_body)
    assert KEY in seen[0].headers["authorization"]  # it was sent, just never recorded


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (
            message(
                [],
                stop="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "x"},
            ),
            "refusal",
        ),
        (message([{"type": "text", "text": '{"rec'}], stop="max_tokens"), "truncated"),
        (message([{"type": "thinking", "thinking": "", "signature": "s"}]), "empty"),
        (message([{"type": "text", "text": "not json"}]), "unsupported_shape"),
    ],
)
def test_unsuccessful_outcomes_are_explicit(monkeypatch, body, status):
    p, _ = provider(monkeypatch, lambda request: httpx2.Response(200, json=body))
    assert p.send(REQUEST).status == status


def test_transport_failure_is_outcome_unknown(monkeypatch):
    def handler(request):
        raise httpx2.ReadTimeout("synthetic timeout", request=request)

    p, _ = provider(monkeypatch, handler)
    out = p.send(REQUEST)
    assert out.status == "outcome_unknown" and "Timeout" in out.error


def test_http_error_is_error(monkeypatch):
    p, _ = provider(
        monkeypatch,
        lambda r: httpx2.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
        ),
    )
    out = p.send(REQUEST)
    assert out.status == "error" and out.response_meta["http_status"] == 400
