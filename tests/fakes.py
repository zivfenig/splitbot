"""Test doubles shared by integration tests and the eval harness self-tests."""

from splitbot.llm.client import LLMResult


class FakeLLM:
    """A scripted LLMClient. `replies` are returned in order; an `Exception` item is raised
    instead. Every call is recorded in `calls` as (system, user). Asking for more calls than
    there are replies raises AssertionError, so "never a third call" is testable."""

    def __init__(self, replies: list[str | Exception]):
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        if not self._replies:
            raise AssertionError("FakeLLM: more calls than scripted replies")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(
            text=reply,
            model="fake",
            temperature=0.0,
            input_tokens=10,
            output_tokens=5,
            latency_s=0.0,
            cost_usd=None,
        )
