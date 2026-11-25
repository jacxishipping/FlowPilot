"""Live HTTP LLM provider adapters for OpenAI, Anthropic, and Google Gemini with cost metering."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from flowpilot import ConfigurationError, NonRetryableNodeError, RetryableNodeError
from flowpilot_nodes_core import AgentToolCall, ModelCompletion, ModelProvider

# Approximate USD per 1M tokens (input_per_1m, output_per_1m) for budget metering
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-3-5-sonnet-latest": (3.00, 15.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = MODEL_PRICING_PER_1M.get(model, (1.00, 3.00))
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0, 6)


def _raise_for_provider_status(provider: str, response: httpx.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableNodeError(
            f"{provider} API transient error HTTP {response.status_code}: {response.text[:300]}",
            code=f"{provider.upper()}_HTTP_{response.status_code}",
        )
    if response.status_code >= 400:
        raise NonRetryableNodeError(
            f"{provider} API error HTTP {response.status_code}: {response.text[:300]}",
            code=f"{provider.upper()}_HTTP_{response.status_code}",
        )


class OpenAIModelProvider:
    """OpenAI Chat Completions adapter supporting tool calls and exact token usage."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        if not self._api_key:
            raise ConfigurationError("OPENAI_API_KEY is not configured.")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                for name in tools
            ]

        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
        _raise_for_provider_status("openai", resp)
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))

        parsed_calls: list[AgentToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            parsed_calls.append(AgentToolCall(tool_name=str(fn.get("name", "")), arguments=args))

        return ModelCompletion(
            content=str(msg.get("content") or ""),
            tool_calls=tuple(parsed_calls),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost_usd(model, in_tok, out_tok),
            finish_reason=str(choice.get("finish_reason", "stop")),
        )


class AnthropicModelProvider:
    """Anthropic Messages API adapter supporting Claude models and tool_use blocks."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        if not self._api_key:
            raise ConfigurationError("ANTHROPIC_API_KEY is not configured.")
        system_parts = [str(m["content"]) for m in messages if m.get("role") == "system"]
        chat_msgs = [
            {
                "role": "assistant" if m.get("role") == "assistant" else "user",
                "content": str(m.get("content", "")),
            }
            for m in messages
            if m.get("role") != "system"
        ]
        body: dict[str, Any] = {
            "model": model,
            "messages": chat_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if tools:
            body["tools"] = [
                {"name": name, "input_schema": {"type": "object", "properties": {}}}
                for name in tools
            ]

        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
                json=body,
            )
        _raise_for_provider_status("anthropic", resp)
        data = resp.json()
        text_chunks: list[str] = []
        parsed_calls: list[AgentToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_chunks.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parsed_calls.append(
                    AgentToolCall(
                        tool_name=str(block.get("name", "")),
                        arguments=dict(block.get("input") or {}),
                    )
                )
        usage = data.get("usage", {})
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        return ModelCompletion(
            content="\n".join(text_chunks),
            tool_calls=tuple(parsed_calls),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost_usd(model, in_tok, out_tok),
            finish_reason=str(data.get("stop_reason", "end_turn")),
        )


class GeminiModelProvider:
    """Google Gemini generateContent REST adapter with functionCall parsing and token metering."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        if not self._api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured.")
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": str(m.get("content", ""))}],
            }
            for m in messages
            if m.get("role") != "system"
        ]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        system_parts = [str(m["content"]) for m in messages if m.get("role") == "system"]
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": name, "description": f"FlowPilot tool {name}"}
                        for name in tools
                    ]
                }
            ]

        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/models/{model}:generateContent",
                params={"key": self._api_key},
                json=body,
            )
        _raise_for_provider_status("gemini", resp)
        data = resp.json()
        cand = data["candidates"][0]
        parts = cand.get("content", {}).get("parts", [])
        text_chunks: list[str] = []
        parsed_calls: list[AgentToolCall] = []
        for part in parts:
            if "text" in part:
                text_chunks.append(str(part["text"]))
            if "functionCall" in part:
                fc = part["functionCall"]
                parsed_calls.append(
                    AgentToolCall(
                        tool_name=str(fc.get("name", "")),
                        arguments=dict(fc.get("args") or {}),
                    )
                )
        usage = data.get("usageMetadata", {})
        in_tok = int(usage.get("promptTokenCount", 0))
        out_tok = int(usage.get("candidatesTokenCount", 0))
        return ModelCompletion(
            content="\n".join(text_chunks),
            tool_calls=tuple(parsed_calls),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost_usd(model, in_tok, out_tok),
            finish_reason=str(cand.get("finishReason", "STOP")),
        )


class OllamaModelProvider:
    """Offline / self-hosted Ollama Chat & Embeddings adapter ($0 API cost)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")
        self._transport = transport

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
                for name in tools
            ]

        async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=body)
        _raise_for_provider_status("ollama", resp)
        data = resp.json()
        msg = data.get("message", {})
        parsed_calls: list[AgentToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", {})
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            parsed_calls.append(AgentToolCall(tool_name=str(fn.get("name", "")), arguments=args))

        return ModelCompletion(
            content=str(msg.get("content") or ""),
            tool_calls=tuple(parsed_calls),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            cost_usd=0.0,
            finish_reason=str(data.get("done_reason", "stop")),
        )

    async def embed(self, *, model: str, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
        _raise_for_provider_status("ollama", resp)
        vec = resp.json().get("embedding", [])
        return [float(x) for x in vec] if isinstance(vec, list) else []


class MultiModelProvider:
    """Routes ModelProvider calls by provider name ('openai', 'anthropic', 'gemini', 'ollama')."""

    def __init__(self, providers: dict[str, ModelProvider]) -> None:
        self._providers = {k.lower(): v for k, v in providers.items()}

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        target = self._providers.get(provider.lower())
        if target is None:
            raise ConfigurationError(
                f"LLM provider '{provider}' is not configured. "
                f"Available: {sorted(self._providers.keys())}."
            )
        return await target.complete(
            provider=provider,
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
