"""Thin wrapper around the OpenAI chat API. No prompts live here (they are in prompts/)."""

import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from openai import OpenAI, OpenAIError

from splitbot.config import ConfigError, optional, price_for, require

_MILLION = Decimal(1_000_000)


class LLMError(RuntimeError):
    """The LLM call failed, was cut off, or returned nothing usable."""


@dataclass(frozen=True)
class LLMResult:
    text: str  # the model's reply (a JSON string)
    model: str  # model name that was configured (recorded in every eval result)
    temperature: float  # temperature that was configured (recorded in every eval result)
    input_tokens: int
    output_tokens: int
    latency_s: float  # wall time of the call, >= 0
    cost_usd: Decimal | None  # None when the prices are not configured
    cached_tokens: int | None = None  # input tokens served from the prompt cache; None when not reported


@dataclass(frozen=True)
class ToolCall:
    id: str  # the model's tool-call id (echoed back in the tool message)
    name: str
    arguments: dict  # parsed JSON object; never a string


@dataclass(frozen=True)
class ChatResult:
    """One assistant turn of a tool-calling conversation."""

    content: str | None  # the text the model wrote; None when it only called tools
    tool_calls: tuple[ToolCall, ...]  # empty when the model answered in text
    message: dict  # the assistant message in OpenAI format, to append to the history as it is
    model: str
    temperature: float
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: Decimal | None
    cached_tokens: int | None = None


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> LLMResult: ...


class ChatLLM(Protocol):
    """What the agent loop needs: one tool-calling turn."""

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult: ...


class OpenAIClient:
    """`sdk` is any object shaped like `openai.OpenAI` (tests pass a stub); when None, a real
    `OpenAI(api_key=api_key, timeout=timeout_s)` is created.

    complete(system, user) calls exactly once:
        sdk.chat.completions.create(
            model=<model>, temperature=<temperature>, timeout=<timeout_s>,
            max_completion_tokens=<max_output_tokens>,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
    The user text is ONLY ever sent in the "user" message, never in the system message.
    From the response it reads: `response.choices[0].message.content`,
    `response.choices[0].finish_reason`, `response.usage.prompt_tokens` and
    `response.usage.completion_tokens`, and (optional) `response.usage.prompt_tokens_details.
    cached_tokens` -> `LLMResult.cached_tokens` (None when `prompt_tokens_details` is missing or
    its `cached_tokens` is None: an API/SDK object without it must not break the call).
    `latency_s` is measured around that one call (a real
    client is built with `max_retries=2`, so it can include SDK retries).

    Raises LLMError when: the SDK raises any `openai.OpenAIError` (API error, timeout,
    connection error); `response.choices` is empty; `content` is None or empty/blank;
    `finish_reason == "length"` (the JSON was cut off); or `response.usage` is None
    (without token counts the eval cost and latency numbers would silently be wrong).

    Cost (Decimal, never float): `input_tokens * price_in_per_mtok / 1_000_000 +
    output_tokens * price_out_per_mtok / 1_000_000`; None if either price is None.
    The returned `model` and `temperature` are the configured ones.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        price_in_per_mtok: Decimal | None = None,
        price_out_per_mtok: Decimal | None = None,
        timeout_s: float = 30.0,
        max_output_tokens: int = 1000,
        sdk: OpenAI | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.price_in_per_mtok = price_in_per_mtok
        self.price_out_per_mtok = price_out_per_mtok
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self._sdk = sdk if sdk is not None else OpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)

    @classmethod
    def from_env(cls, *, model: str | None = None, prices_path: Path | None = None) -> "OpenAIClient":
        """Read settings from environment variables (loaded from .env by splitbot.config):
        OPENAI_API_KEY (required), OPENAI_MODEL (required unless `model` is given; a given
        `model` wins), OPENAI_TEMPERATURE (optional, default 0). Prices are NOT read from the
        environment: they come from the versioned price list (`config.price_for(model,
        prices_path)`); a model that is not listed gets no prices (cost unknown, None).

        Raises `splitbot.config.ConfigError` when the key or model is missing, when the model
        name contains whitespace (outer whitespace is stripped first), when the temperature
        is not a number between 0 and 2 (inclusive), or when the price file is unreadable
        (`load_prices` errors). A temperature of "-0" is read as 0.0 (positive zero).
        """
        api_key = require("OPENAI_API_KEY")
        model = (model or "").strip() or require("OPENAI_MODEL")
        if any(ch.isspace() for ch in model):
            raise ConfigError("OPENAI_MODEL must not contain whitespace")
        try:
            temperature = float(optional("OPENAI_TEMPERATURE", "0")) + 0.0  # "-0" -> 0.0
        except ValueError:
            raise ConfigError("OPENAI_TEMPERATURE must be a number") from None
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ConfigError("OPENAI_TEMPERATURE must be between 0 and 2")
        price = price_for(model, prices_path)  # None: the model is not listed, cost stays unknown
        return cls(
            api_key=api_key,
            model=model,
            temperature=temperature,
            price_in_per_mtok=price.in_per_mtok if price else None,
            price_out_per_mtok=price.out_per_mtok if price else None,
        )

    def complete(self, system: str, user: str) -> LLMResult:
        started = time.perf_counter()
        try:
            response = self._sdk.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                timeout=self.timeout_s,
                max_completion_tokens=self.max_output_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except OpenAIError as exc:
            # Only the error type: never echo request text (it is user data).
            raise LLMError(f"OpenAI call failed: {type(exc).__name__}") from exc
        latency_s = time.perf_counter() - started

        if not response.choices:
            raise LLMError("the reply had no choices")
        if response.usage is None:
            raise LLMError("the reply had no token usage")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("the reply was cut off (finish_reason=length)")
        text = choice.message.content
        if text is None or not text.strip():
            raise LLMError("the reply was empty")

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        details = getattr(response.usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", None) if details is not None else None
        return LLMResult(
            text=text,
            model=self.model,
            temperature=self.temperature,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=latency_s,
            cost_usd=self._cost(input_tokens, output_tokens),
            cached_tokens=cached_tokens,
        )

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        """One tool-calling turn. Calls exactly once:
            sdk.chat.completions.create(
                model=<model>, temperature=<temperature>, timeout=<timeout_s>,
                max_completion_tokens=<max_output_tokens>,
                messages=messages, tools=tools, tool_choice="auto")
        (no `response_format`; `tools` and `tool_choice` are left out when `tools` is empty). The
        messages are sent as given: the caller keeps user text in "user" messages only.
        From `response.choices[0].message` it reads `content` and `tool_calls` (each
        `.id`, `.function.name`, `.function.arguments` = a JSON string that must parse to a JSON
        OBJECT, giving `ToolCall.arguments`); `ChatResult.message` is
        {"role": "assistant", "content": <content or None>} plus, when there are tool calls,
        "tool_calls": [{"id", "type": "function", "function": {"name", "arguments": <the original
        JSON string>}}]. Tokens, latency, cached tokens and cost are computed exactly as in
        `complete`.

        Raises LLMError when: the SDK raises any `openai.OpenAIError`; `response.choices` is empty;
        `response.usage` is None; `finish_reason == "length"`; the message has neither text (after
        stripping) nor tool calls; or any tool call's arguments are not valid JSON or not a JSON
        object. Error text never echoes model output or request text."""
        kwargs = {}
        if tools:
            kwargs = {"tools": tools, "tool_choice": "auto"}
        started = time.perf_counter()
        try:
            response = self._sdk.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                timeout=self.timeout_s,
                max_completion_tokens=self.max_output_tokens,
                messages=messages,
                **kwargs,
            )
        except OpenAIError as exc:
            raise LLMError(f"OpenAI call failed: {type(exc).__name__}") from exc
        latency_s = time.perf_counter() - started

        if not response.choices:
            raise LLMError("the reply had no choices")
        if response.usage is None:
            raise LLMError("the reply had no token usage")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("the reply was cut off (finish_reason=length)")
        content = choice.message.content
        raw_calls = getattr(choice.message, "tool_calls", None) or []
        if not raw_calls and (content is None or not content.strip()):
            raise LLMError("the reply had neither text nor tool calls")

        tool_calls = []
        message: dict = {"role": "assistant", "content": content}
        for raw in raw_calls:
            try:
                arguments = json.loads(raw.function.arguments)
            except (TypeError, ValueError):
                raise LLMError("a tool call's arguments were not valid JSON") from None
            if not isinstance(arguments, dict):
                raise LLMError("a tool call's arguments were not a JSON object")
            tool_calls.append(ToolCall(id=raw.id, name=raw.function.name, arguments=arguments))
        if raw_calls:
            message["tool_calls"] = [
                {"id": raw.id, "type": "function", "function": {"name": raw.function.name, "arguments": raw.function.arguments}}
                for raw in raw_calls
            ]

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        details = getattr(response.usage, "prompt_tokens_details", None)
        return ChatResult(
            content=content if content is not None and content.strip() else None,
            tool_calls=tuple(tool_calls),
            message=message,
            model=self.model,
            temperature=self.temperature,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=latency_s,
            cost_usd=self._cost(input_tokens, output_tokens),
            cached_tokens=getattr(details, "cached_tokens", None) if details is not None else None,
        )

    def _cost(self, input_tokens: int, output_tokens: int) -> Decimal | None:
        if self.price_in_per_mtok is None or self.price_out_per_mtok is None:
            return None
        return (
            Decimal(input_tokens) * self.price_in_per_mtok / _MILLION
            + Decimal(output_tokens) * self.price_out_per_mtok / _MILLION
        )
