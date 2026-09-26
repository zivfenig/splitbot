"""Router 2: Jev (TypeSafe decision model) through OpenRouter, a `choice` question."""

import json
import re
import time
from decimal import Decimal
from pathlib import Path

import httpx

from splitbot.config import optional, price_for, require
from splitbot.router.base import LABELS, MAX_MESSAGE_CHARS, RouteResult, error_result

URL = "https://openrouter.ai/api/alpha/decisions"
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_VERSION_NAME = re.compile(r"[A-Za-z0-9_]{1,60}")


def load_question(version: str = "jev_router_v1") -> dict:
    """The question from `prompts/<version>.json`: {"instructions": str, "criteria": {label: str}}.
    Raises ValueError when `version` is not a plain name (letters, digits, underscore; at most 60
    characters) or the criteria keys are not exactly expense, query, ignore; FileNotFoundError
    when the file does not exist."""
    if not _VERSION_NAME.fullmatch(version):
        raise ValueError(f"not a question version name: {version!r}")
    data = json.loads((PROMPTS_DIR / f"{version}.json").read_text(encoding="utf-8"))
    criteria = data.get("criteria")
    if not isinstance(data.get("instructions"), str) or not isinstance(criteria, dict) or set(criteria) != set(LABELS):
        raise ValueError("the question needs instructions and criteria for exactly expense, query, ignore")
    return {"instructions": data["instructions"], "criteria": criteria}


class JevRouter:
    """Request (verified against the real API): `POST URL` with headers `Authorization: Bearer
    <api_key>` and `Content-Type: application/json`, JSON body `{"model": <model>, "state":
    <message>, "questions": {"route": {"type": "choice", "instructions": ..., "criteria": {...}}}}`
    (instructions/criteria from `load_question()`). The message appears ONLY as the value of
    `state`. Response (HTTP 200): `{"answers": {"route": {"choice": str, "probabilities": {label:
    float}, "confidence": float}}, "usage": {"input_tokens": int, "output_tokens": int, "cost":
    float}, ...}`.

    route(message): `scores` = the probabilities, renormalized to sum 1 (the keys must be exactly
    expense, query, ignore, values >= 0 and a positive sum, else error); `label` = argmax. Cost =
    `usage.cost` (as Decimal of its text) when present and >= 0, else computed from
    `usage.input_tokens/output_tokens` and the per-1M prices when both prices are given, else
    None. `latency_s` = wall time of the HTTP call. An empty/blank message or one longer than
    `base.MAX_MESSAGE_CHARS`, a non-200 status, `httpx.HTTPError` (timeouts, connection errors),
    invalid JSON or a missing key -> `error_result` (never raises, and the reason never contains
    the message or the response body).
    """

    name = "jev"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "typesafe/jev-1.13",
        http: httpx.Client | None = None,
        timeout_s: float = 20.0,
        price_in_per_mtok: Decimal | None = None,
        price_out_per_mtok: Decimal | None = None,
        question_version: str = "jev_router_v1",
    ):
        self._api_key = api_key
        self.model = model
        self._http = http if http is not None else httpx.Client(timeout=timeout_s)
        self._price_in = price_in_per_mtok
        self._price_out = price_out_per_mtok
        self._question = load_question(question_version)

    @classmethod
    def from_env(cls, *, prices_path: Path | None = None) -> "JevRouter":
        """OPENROUTER_API_KEY (required, else ConfigError), JEV_MODEL (optional, default
        "typesafe/jev-1.13"); prices from `config.price_for(model, prices_path)`."""
        api_key = require("OPENROUTER_API_KEY")
        model = optional("JEV_MODEL", "typesafe/jev-1.13")
        price = price_for(model, prices_path)
        return cls(
            api_key=api_key,
            model=model,
            price_in_per_mtok=price.in_per_mtok if price else None,
            price_out_per_mtok=price.out_per_mtok if price else None,
        )

    def route(self, message: str) -> RouteResult:
        if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            return error_result("message is empty or too long")
        body = {"model": self.model, "state": message, "questions": {"route": {"type": "choice", **self._question}}}
        started = time.perf_counter()
        try:
            response = self._http.post(
                URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError:
            return error_result("the request failed", time.perf_counter() - started)
        latency = time.perf_counter() - started
        if response.status_code != 200:
            return error_result(f"HTTP {response.status_code}", latency)
        try:
            data = response.json()
            probabilities = data["answers"]["route"]["probabilities"]
            scores = _normalized(probabilities)
        except (ValueError, KeyError, TypeError, AttributeError):
            return error_result("the response could not be read", latency)
        label = max(LABELS, key=lambda name: scores[name])
        return RouteResult(label=label, scores=scores, latency_s=latency, cost_usd=self._cost(data.get("usage")))

    def _cost(self, usage) -> Decimal | None:
        if not isinstance(usage, dict):
            return None
        reported = usage.get("cost")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
            return Decimal(str(reported))
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        if self._price_in is None or self._price_out is None or not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
            return None
        return (Decimal(tokens_in) * self._price_in + Decimal(tokens_out) * self._price_out) / Decimal(1_000_000)


def _normalized(probabilities) -> dict[str, float]:
    """The three probabilities as floats summing to 1. ValueError for anything else."""
    if not isinstance(probabilities, dict) or set(probabilities) != set(LABELS):
        raise ValueError("probabilities must have exactly the three labels")
    values = {}
    for label in LABELS:
        value = probabilities[label]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("a probability must be a non-negative number")
        values[label] = float(value)
    total = sum(values.values())
    if total <= 0:
        raise ValueError("probabilities sum to zero")
    return {label: value / total for label, value in values.items()}
