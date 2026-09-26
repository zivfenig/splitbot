"""Router 1: OpenAI embeddings. The message is embedded and compared (cosine similarity) with a
small labeled reference set; no text is generated."""

import json
import math
import time
from decimal import Decimal
from pathlib import Path

from openai import OpenAI, OpenAIError

from splitbot.config import price_for, require
from splitbot.router.base import LABELS, MAX_MESSAGE_CHARS, RouteResult, error_result

REFERENCE_PATH = Path(__file__).resolve().parents[3] / "tests" / "llm_evals" / "datasets" / "router_reference.jsonl"


def load_references(path: Path | None = None) -> list[tuple[str, str]]:
    """Read the human's verified reference set (default REFERENCE_PATH, resolved at call time):
    one JSON object per line with "message", "label" (expense|query|ignore) and "verified".
    Returns [(message, label), ...] in file order. Raises ValueError for a row that is not
    verified or has an unknown label, or an empty file."""
    source = path if path is not None else REFERENCE_PATH
    references = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("verified") is not True:
            raise ValueError(f"reference row {row.get('id')!r} is not verified")
        if row.get("label") not in LABELS:
            raise ValueError(f"reference row {row.get('id')!r} has an unknown label")
        references.append((row["message"], row["label"]))
    if not references:
        raise ValueError("the reference set is empty")
    return references


class EmbeddingRouter:
    """`client` is any object shaped like `openai.OpenAI`: `client.embeddings.create(model=...,
    input=[texts])` returns an object with `.data` (a list, in input order, of objects with
    `.embedding`: list[float]) and `.usage.prompt_tokens`. `references` is [(message, label)].

    Setup (`prepare()`, called automatically by the first `route()`, idempotent): ONE
    embeddings call for all reference messages; it fills `self.setup` =
    {"tokens": int, "latency_s": float, "cost_usd": Decimal | None} (kept apart from the
    per-message figures). If it fails, `route()` returns error results (never raises).

    route(message): one embeddings call for the message. Class score = the mean of the
    top `top_k` (default 3) cosine similarities to that class's reference vectors (all of them
    if the class has fewer). Probabilities = softmax over the three class scores divided by
    `temperature` (default 0.05, a fixed constant, never tuned). `label` = argmax. `cost_usd` =
    `usage.prompt_tokens * price_in_per_mtok / 1_000_000` (None without a price). `latency_s` =
    wall time of the whole route call (>= 0). An empty/blank message, a message longer than
    `base.MAX_MESSAGE_CHARS`, an `openai.OpenAIError`, or a malformed response -> `error_result`.
    """

    name = "embedding"

    def __init__(
        self,
        *,
        client,
        model: str,
        references: list[tuple[str, str]],
        price_in_per_mtok: Decimal | None = None,
        top_k: int = 3,
        temperature: float = 0.05,
    ):
        if {label for _, label in references} != set(LABELS):
            raise ValueError("the reference set needs at least one row per label")
        self._client = client
        self.model = model
        self._references = references
        self._price = price_in_per_mtok
        self._top_k = top_k
        self._temperature = temperature
        self._ref_vectors: list[tuple[str, list[float]]] | None = None
        self.setup: dict | None = None

    @classmethod
    def from_env(cls, *, references_path: Path | None = None, prices_path: Path | None = None) -> "EmbeddingRouter":
        """OPENAI_API_KEY and OPENAI_EMBEDDING_MODEL (both required, else ConfigError); references
        from `load_references(references_path)`; the price from `config.price_for(model,
        prices_path)` (unlisted -> None)."""
        api_key = require("OPENAI_API_KEY")
        model = require("OPENAI_EMBEDDING_MODEL")
        price = price_for(model, prices_path)
        return cls(
            client=OpenAI(api_key=api_key, timeout=30, max_retries=2),
            model=model,
            references=load_references(references_path),
            price_in_per_mtok=price.in_per_mtok if price else None,
        )

    def prepare(self) -> None:
        if self._ref_vectors is not None:
            return
        started = time.perf_counter()
        response = self._client.embeddings.create(model=self.model, input=[text for text, _ in self._references])
        vectors = [_unit(item.embedding) for item in response.data]
        if len(vectors) != len(self._references):
            raise ValueError("one embedding per reference expected")
        tokens = response.usage.prompt_tokens
        self._ref_vectors = [(label, vec) for (_, label), vec in zip(self._references, vectors)]
        self.setup = {
            "tokens": tokens,
            "latency_s": time.perf_counter() - started,
            "cost_usd": self._cost(tokens),
        }

    def route(self, message: str) -> RouteResult:
        if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            return error_result("message is empty or too long")
        try:
            self.prepare()  # setup is measured apart from the per-message figures
        except (OpenAIError, AttributeError, IndexError, TypeError, ValueError):
            return error_result("could not embed the reference set")
        started = time.perf_counter()
        try:
            response = self._client.embeddings.create(model=self.model, input=[message])
            vector = _unit(response.data[0].embedding)
            tokens = response.usage.prompt_tokens
            sims: dict[str, list[float]] = {label: [] for label in LABELS}
            for label, ref in self._ref_vectors:
                sims[label].append(sum(a * b for a, b in zip(vector, ref)))
            class_scores = {
                label: sum(top) / len(top)
                for label, values in sims.items()
                for top in [sorted(values, reverse=True)[: self._top_k]]
            }
            peak = max(class_scores.values())
            exps = {label: math.exp((score - peak) / self._temperature) for label, score in class_scores.items()}
            total = sum(exps.values())
            scores = {label: exps[label] / total for label in LABELS}
            cost = self._cost(tokens)
        except (OpenAIError, AttributeError, IndexError, TypeError, ValueError, ZeroDivisionError):
            return error_result("embedding call failed or returned something unreadable", time.perf_counter() - started)
        label = max(LABELS, key=lambda name: scores[name])
        return RouteResult(label=label, scores=scores, latency_s=time.perf_counter() - started, cost_usd=cost)

    def _cost(self, tokens: int) -> Decimal | None:
        if self._price is None:
            return None
        return Decimal(tokens) * self._price / Decimal(1_000_000)


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("zero vector")
    return [x / norm for x in vector]
