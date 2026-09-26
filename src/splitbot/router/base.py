"""Router contract: decide, cheaply and without generating text, whether a group-chat message
needs the agent. The only thresholded decision is binary: `ignore` vs `pass` (to the agent).

The class named "expense" is the router's ACTION / money-related class: a new expense, a
correction or a delete. The router does not tell those apart (the extractor's message_type does).
Rule: never drop a real expense. Any doubt or failure -> pass.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

Label = Literal["expense", "query", "ignore"]
LABELS: tuple[str, ...] = ("expense", "query", "ignore")
MAX_MESSAGE_CHARS = 1000  # longer messages are never judged by a router: they pass to the agent


@dataclass(frozen=True)
class RouteResult:
    label: Label  # the router's own best label (argmax of `scores`)
    scores: dict[str, float]  # one score per label in LABELS, each in [0, 1], summing to about 1
    latency_s: float  # wall time of this call, >= 0
    cost_usd: Decimal | None  # this call's cost; None when unknown
    error: str | None = None  # set when the router failed (timeout, bad reply, ...): a fixed reason, never message text


class Router(Protocol):
    name: str  # "embedding" or "jev"

    def route(self, message: str) -> RouteResult:
        """Never raises: any failure becomes `error_result(...)`."""
        ...


def error_result(reason: str, latency_s: float = 0.0) -> RouteResult:
    """The result for a failed call: label "expense", scores {"expense": 1.0, "query": 0.0,
    "ignore": 0.0}, cost None, `error` = reason. It always passes to the agent."""
    return RouteResult(
        label="expense",
        scores={"expense": 1.0, "query": 0.0, "ignore": 0.0},
        latency_s=latency_s,
        cost_usd=None,
        error=reason,
    )


def decide(result: RouteResult, threshold: float) -> Literal["ignore", "pass"]:
    """"ignore" only when the result has NO error and `scores["ignore"] >= threshold`
    (inclusive); everything else is "pass" (the message goes to the agent)."""
    if result.error is None and result.scores["ignore"] >= threshold:
        return "ignore"
    return "pass"
