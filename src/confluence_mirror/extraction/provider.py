"""Model provider adapter: Claude through the official Anthropic SDK (optional extra).

The SDK is imported lazily, so the mirror, the parser and inspection never load it
(``pip install 'confluence-pg-mirror[model]'`` adds it). The adapter:

- sends exactly the application request built by :mod:`payload` (no tools, no sampling
  parameters; Claude Haiku 5.5 thinks adaptively by default and ``effort`` sets depth);
- captures the body the SDK actually puts on the wire, before transport, and the nonsecret
  request context (endpoint, API version, SDK version) - never authentication headers;
- keeps only the final text output and safe response metadata; thinking blocks are dropped;
- maps refusal, truncation, empty output, unsupported shapes and timeouts to explicit
  outcomes, with ``outcome_unknown`` when a request may have reached the server.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from confluence_mirror.logging_utils import redact, register_secret

log = logging.getLogger(__name__)

_SAFE_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "user-agent")


@dataclass
class ModelSettings:
    provider: str = "anthropic"
    model: str = "claude-haiku-5-5"
    effort: str = "medium"
    max_output_tokens: int = 16000
    endpoint: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: float = 120.0
    max_retries: int = 2  # three transient HTTP attempts in total
    concurrency: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "max_output_tokens": self.max_output_tokens,
            "endpoint": self.endpoint,
        }


@dataclass
class Outcome:
    status: (
        str  # succeeded | refusal | truncated | empty | unsupported_shape | outcome_unknown | error
    )
    text: str | None = None
    parsed: Any = None
    usage: dict[str, int] = field(default_factory=dict)
    response_meta: dict[str, Any] = field(default_factory=dict)
    sent_body: dict | None = None
    request_context: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ProviderUnavailable(Exception):
    pass


class AnthropicProvider:
    def __init__(self, settings: ModelSettings, *, http_client: Any = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderUnavailable(
                "the Anthropic SDK is not installed: pip install 'confluence-pg-mirror[model]'"
            ) from exc
        self._anthropic = anthropic
        self.settings = settings
        key = os.environ.get(settings.api_key_env, "").strip()
        if key:
            register_secret(key)
        self._captured: list[tuple[dict, dict]] = []

        def capture(request: Any) -> None:
            try:
                body = json.loads(request.content or b"{}")
            except ValueError:
                body = None
            headers = {k: v for k, v in request.headers.items() if k.lower() in _SAFE_HEADERS}
            self._captured.append((body, headers))

        client_kw: dict[str, Any] = {
            "base_url": settings.endpoint,
            "timeout": settings.timeout_seconds,
            "max_retries": settings.max_retries,
            "http_client": http_client
            or anthropic.DefaultHttpxClient(event_hooks={"request": [capture]}),
        }
        if http_client is not None:
            http_client.event_hooks = {**http_client.event_hooks, "request": [capture]}
        if key:
            client_kw["api_key"] = key
        self.client = anthropic.Anthropic(**client_kw)

    def _context(self, headers: dict) -> dict[str, Any]:
        return {
            "provider": "anthropic",
            "endpoint": self.settings.endpoint,
            "sdk_version": getattr(self._anthropic, "__version__", None),
            "headers": headers,
        }

    def send(self, request: dict) -> Outcome:
        a = self._anthropic
        self._captured.clear()
        try:
            message = self.client.messages.create(**request)
        except (a.APITimeoutError, a.APIConnectionError) as exc:
            # A request may have reached the server: its outcome (and cost) is unknown.
            return self._fail("outcome_unknown", exc)
        except a.APIStatusError as exc:
            return self._fail("error", exc, http_status=exc.status_code)
        sent, headers = self._captured[-1] if self._captured else (None, {})
        texts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        usage = {
            "input_tokens": getattr(message.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(message.usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        }
        details = getattr(message, "stop_details", None)
        meta = {
            "id": message.id,
            "request_id": getattr(message, "_request_id", None),
            "requested_model": request.get("model"),
            "returned_model": message.model,
            "stop_reason": message.stop_reason,
            "stop_details": details.model_dump() if details is not None else None,
            "content_types": [getattr(b, "type", None) for b in message.content],
        }
        text = "".join(texts) if texts else None
        common = {
            "usage": usage,
            "response_meta": meta,
            "sent_body": sent,
            "request_context": self._context(headers),
            "text": text,
        }
        if message.stop_reason == "refusal":
            return Outcome("refusal", **common)
        if message.stop_reason == "max_tokens":
            return Outcome("truncated", **common)
        if not text:
            return Outcome("empty", **common)
        try:
            parsed = json.loads(text)
        except ValueError:
            return Outcome("unsupported_shape", error="final output is not JSON", **common)
        if not isinstance(parsed, dict):
            return Outcome("unsupported_shape", error="final output is not an object", **common)
        return Outcome("succeeded", parsed=parsed, **common)

    def _fail(self, status: str, exc: Exception, http_status: int | None = None) -> Outcome:
        sent, headers = self._captured[-1] if self._captured else (None, {})
        return Outcome(
            status,
            sent_body=sent,
            request_context=self._context(headers),
            error=redact(f"{type(exc).__name__}: {exc}"),
            response_meta={"http_status": http_status},
        )

    def close(self) -> None:
        self.client.close()


def make_provider(settings: ModelSettings) -> AnthropicProvider:
    """Indirection so tests can substitute a provider."""
    if settings.provider != "anthropic":
        raise ProviderUnavailable(f"unsupported provider {settings.provider!r}")
    return AnthropicProvider(settings)


def cost_usd(usage: dict, pricing: dict | None) -> float | None:
    """Cost from a dated pricing entry; None (unknown) when no price is configured."""
    if not pricing:
        return None
    return round(
        usage.get("input_tokens", 0) * pricing["input_per_mtok"] / 1e6
        + usage.get("output_tokens", 0) * pricing["output_per_mtok"] / 1e6,
        6,
    )
