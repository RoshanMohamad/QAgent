"""LLM providers.

Three implementations behind one protocol:

``null``
    Deterministic, offline, free. Returns a schema-shaped stub and reports that it is
    not a real model, so every caller must have a rule-based path. CI and the eval
    harness run on this, which keeps them reproducible.
``anthropic``
    Claude Messages API, called over httpx so the package has no hard SDK dependency.
    Structured output is obtained with a forced tool call rather than by parsing prose.
``openai_compatible``
    Any OpenAI-shaped endpoint, including a local Ollama server.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

ANTHROPIC_VERSION = "2023-06-01"
_STRUCTURED_TOOL = "emit_result"


@dataclass
class LlmResult:
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    model: str
    provider: str
    stub: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Provider(Protocol):
    name: str
    is_real: bool

    def complete_json(
        self, *, system: str, user: str, schema: dict, model: str, max_tokens: int = 2048
    ) -> LlmResult: ...


def _estimate_tokens(text: str) -> int:
    """Rough token count used when a provider does not report usage."""
    return max(1, len(text) // 4)


def _stub_from_schema(schema: dict) -> dict[str, Any]:
    """Build a minimal object satisfying a JSON schema.

    Used by the null provider. Every field falls back to an explicit default so
    downstream code exercises the same paths it would with a live model.
    """
    if "default" in schema:
        return schema["default"]

    kind = schema.get("type", "object")
    if kind == "object":
        out: dict[str, Any] = {}
        props = schema.get("properties", {})
        for key in schema.get("required", list(props)):
            out[key] = _stub_value(props.get(key, {"type": "string"}))
        return out
    return _stub_value(schema)


def _stub_value(schema: dict) -> Any:
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "string")
    if kind == "object":
        return _stub_from_schema(schema)
    if kind == "array":
        return []
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return ""


class NullProvider:
    """No network, no cost, fully deterministic."""

    name = "null"
    is_real = False

    def complete_json(
        self, *, system: str, user: str, schema: dict, model: str, max_tokens: int = 2048
    ) -> LlmResult:
        return LlmResult(
            data=_stub_from_schema(schema),
            input_tokens=_estimate_tokens(system + user),
            output_tokens=0,
            latency_ms=0,
            model="null",
            provider=self.name,
            stub=True,
        )


class AnthropicProvider:
    """Claude Messages API with forced tool use for structured output."""

    name = "anthropic"
    is_real = True

    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            timeout=timeout,
        )

    def complete_json(
        self, *, system: str, user: str, schema: dict, model: str, max_tokens: int = 2048
    ) -> LlmResult:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": _STRUCTURED_TOOL,
                    "description": "Return the result. This is the only way to answer.",
                    "input_schema": schema,
                }
            ],
            # Forcing the tool is what makes verdict fields unreachable from prose.
            "tool_choice": {"type": "tool", "name": _STRUCTURED_TOOL},
        }

        started = time.perf_counter()
        response = self._client.post("/v1/messages", json=payload)
        response.raise_for_status()
        body = response.json()
        latency_ms = int((time.perf_counter() - started) * 1000)

        data: dict[str, Any] = {}
        for block in body.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_TOOL:
                data = block.get("input", {})
                break

        usage = body.get("usage", {})
        return LlmResult(
            data=data,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=latency_ms,
            model=body.get("model", model),
            provider=self.name,
        )


class OpenAICompatibleProvider:
    """Any OpenAI-shaped /chat/completions endpoint, including local Ollama."""

    name = "openai_compatible"
    is_real = True

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 60.0) -> None:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def complete_json(
        self, *, system: str, user: str, schema: dict, model: str, max_tokens: int = 2048
    ) -> LlmResult:
        # Not every compatible server supports json_schema, but all of the ones we
        # target support json_object, so the schema is also restated in the prompt.
        instruction = (
            f"{user}\n\nRespond with a single JSON object matching this schema:\n"
            f"{json.dumps(schema)}"
        )
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": instruction},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

        started = time.perf_counter()
        response = self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        latency_ms = int((time.perf_counter() - started) * 1000)

        content = body["choices"][0]["message"]["content"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {}

        usage = body.get("usage", {})
        return LlmResult(
            data=data if isinstance(data, dict) else {},
            input_tokens=usage.get("prompt_tokens", _estimate_tokens(system + instruction)),
            output_tokens=usage.get("completion_tokens", _estimate_tokens(content)),
            latency_ms=latency_ms,
            model=body.get("model", model),
            provider=self.name,
        )


def build_provider(settings) -> Provider:
    """Select a provider from settings, degrading to null rather than failing."""
    choice = settings.qagent_llm_provider

    if choice == "anthropic":
        if not settings.anthropic_api_key:
            return NullProvider()
        return AnthropicProvider(settings.anthropic_api_key)

    if choice == "openai_compatible":
        if not settings.qagent_llm_base_url:
            return NullProvider()
        return OpenAICompatibleProvider(
            settings.qagent_llm_base_url, settings.anthropic_api_key or None
        )

    return NullProvider()
