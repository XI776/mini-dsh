"""DeepSeek Chat Completions model adapter, without an SDK dependency."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContextView, ModelFailure, ModelResult, ToolCall, Usage


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def load_api_key(env_file: Path) -> str:
    """Read one secret from the process environment or a local .env file."""
    if key := os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return key
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError("DEEPSEEK_API_KEY is missing from environment and .env") from exc
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != "DEEPSEEK_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            return value
    raise ValueError("DEEPSEEK_API_KEY is missing from environment and .env")


@dataclass(frozen=True)
class DeepSeekPricing:
    """Explicit USD prices per million tokens; caller owns the price snapshot."""

    cache_hit: float
    cache_miss: float
    output: float

    def __post_init__(self) -> None:
        if not all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                   for value in (self.cache_hit, self.cache_miss, self.output)):
            raise ValueError("DeepSeek prices must be finite nonnegative numbers")

    def estimate(self, input_tokens: int, output_tokens: int, cache_hit_tokens: int) -> float:
        if not all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens, cache_hit_tokens)):
            raise ValueError("token counts must be nonnegative integers")
        if cache_hit_tokens > input_tokens:
            raise ValueError("cache hits exceed input tokens")
        return (
            cache_hit_tokens * self.cache_hit
            + (input_tokens - cache_hit_tokens) * self.cache_miss
            + output_tokens * self.output
        ) / 1_000_000


def load_pricing(path: Path) -> DeepSeekPricing:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"cache_hit", "cache_miss", "output"}:
        raise ValueError("DeepSeek pricing must contain cache_hit, cache_miss and output")
    return DeepSeekPricing(**raw)


def _usage(raw: Any, pricing: DeepSeekPricing | None) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage(known=False)
    try:
        input_tokens = raw["prompt_tokens"]
        output_tokens = raw["completion_tokens"]
        cache_hits = raw.get("prompt_cache_hit_tokens", 0)
        if not all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens, cache_hits)):
            raise ValueError("invalid token counts")
        if cache_hits > input_tokens:
            raise ValueError("cache hits exceed input tokens")
        cache_misses = raw.get("prompt_cache_miss_tokens")
        if cache_misses is not None and (type(cache_misses) is not int or cache_misses < 0):
            raise ValueError("invalid cache miss token count")
        if cache_misses is not None and cache_misses != input_tokens - cache_hits:
            raise ValueError("cache token breakdown does not equal prompt tokens")
        cost = None
        if pricing is not None:
            cost = pricing.estimate(input_tokens, output_tokens, cache_hits)
        return Usage(
            input_tokens, output_tokens, cache_read_tokens=cache_hits, cost_usd=cost,
            cache_usage_known="prompt_cache_hit_tokens" in raw and "prompt_cache_miss_tokens" in raw,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelFailure(f"invalid DeepSeek usage: {exc}", Usage(known=False)) from exc


class DeepSeekModel:
    """Turn generic context views into DeepSeek requests and normalize replies."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        pricing: DeepSeekPricing | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is empty")
        if not model.strip():
            raise ValueError("DeepSeek model is empty")
        if not base_url.startswith("https://"):
            raise ValueError("DeepSeek base URL must use HTTPS")
        self._api_key = api_key.strip()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.pricing = pricing
        self._opener = opener or urllib.request.urlopen
        self.identity = f"deepseek:{model}:{self.base_url}"

    @property
    def trace_secrets(self) -> tuple[str, ...]:
        return (self._api_key,)

    def configuration(self) -> dict[str, object]:
        return {"pricing": asdict(self.pricing) if self.pricing is not None else None}

    def resume_from(self, completed_calls: int) -> None:
        if completed_calls < 0:
            raise ValueError("invalid completed call count")

    def complete(self, view: ContextView, tool_schemas: Sequence[Mapping[str, Any]], timeout_s: float) -> ModelResult:
        if timeout_s <= 0:
            raise ModelFailure("DeepSeek request timed out", Usage(known=False))
        reverse: dict[str, str] = {}
        tools = []
        for index, schema in enumerate(tool_schemas):
            name = str(schema["name"])
            alias = name if _FUNCTION_NAME.fullmatch(name) and name not in reverse else f"tool_{index}"
            if alias in reverse:
                raise ModelFailure("duplicate DeepSeek tool alias", Usage(known=False))
            reverse[alias] = name
            description = schema.get("description") or f"Harness tool: {name}"
            if alias != name:
                description = f"{name}: {description}"
            tools.append({"type": "function", "function": {
                "name": alias, "description": str(description), "parameters": schema["parameters"],
            }})
        messages = []
        for message in view.messages:
            role = message.get("role", "user")
            # Context strategies can summarize or clip any event. Treat tool output as
            # user text so every resulting view is valid without native call pairing.
            if role == "tool":
                role = "user"
            if role not in ("system", "user", "assistant"):
                raise ModelFailure("invalid role in context view", Usage(known=False))
            messages.append({"role": role, "content": str(message.get("content", ""))})
        request: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            request["tools"] = tools
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(http_request, timeout=timeout_s) as response:
                raw = response.read(4_000_001)
        except urllib.error.HTTPError as exc:
            # Response bodies can contain request details. Keep them out of traces.
            raise ModelFailure(f"DeepSeek HTTP {exc.code}", Usage(known=False)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise ModelFailure(f"DeepSeek transport error: {type(exc).__name__}", Usage(known=False)) from exc
        if len(raw) > 4_000_000:
            raise ModelFailure("DeepSeek response exceeded size limit", Usage(known=False))
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ModelFailure("DeepSeek returned invalid JSON", Usage(known=False)) from exc
        if not isinstance(data, dict):
            raise ModelFailure("DeepSeek response must be an object", Usage(known=False))
        usage = _usage(data.get("usage"), self.pricing)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            reason = choice["finish_reason"]
            calls = []
            for item in message.get("tool_calls") or []:
                function = item["function"]
                name = reverse[function["name"]]
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                calls.append(ToolCall(name, arguments, str(item["id"])))
            if reason == "tool_calls" and calls:
                return ModelResult(str(message.get("content") or ""), tuple(calls), False, usage)
            if reason == "stop" and not calls:
                return ModelResult(str(message.get("content") or ""), (), True, usage)
            raise ValueError(f"unsupported finish reason: {reason}")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelFailure(f"invalid DeepSeek completion: {exc}", usage) from exc
