"""Router contract helpers: `error_result` and `decide` (pure functions, no network)."""

from decimal import Decimal

import pytest

from splitbot.router.base import LABELS, RouteResult, decide, error_result


def _result(ignore: float, error: str | None = None, label: str = "ignore") -> RouteResult:
    """A hand-built result: the rest of the probability goes to expense."""
    scores = {"expense": 1.0 - ignore, "query": 0.0, "ignore": ignore}
    return RouteResult(label=label, scores=scores, latency_s=0.1, cost_usd=Decimal("0.000001"), error=error)


def _check_error_result_shape():
    result = error_result("timeout", 0.25)
    assert result.label == "expense"
    assert result.scores == {"expense": 1.0, "query": 0.0, "ignore": 0.0}
    assert set(result.scores) == set(LABELS)
    assert result.cost_usd is None
    assert result.error == "timeout"
    assert result.latency_s == 0.25
    # the latency is optional and defaults to zero
    assert error_result("bad reply").latency_s == 0.0


def _check_error_result_always_passes():
    # even with the most permissive threshold (0.0), a failed call must not be ignored
    assert decide(error_result("timeout"), 0.0) == "pass"


def _check_error_wins_over_high_ignore_score():
    assert decide(_result(0.99, error="timeout"), 0.5) == "pass"


def _check_below_threshold_passes():
    assert decide(_result(0.79), 0.8) == "pass"


def _check_exactly_at_threshold_is_ignore():
    assert decide(_result(0.75), 0.75) == "ignore"


def _check_above_threshold_is_ignore():
    assert decide(_result(0.9), 0.75) == "ignore"


def _check_threshold_above_one_always_passes():
    assert decide(_result(1.0), 1.01) == "pass"
    assert decide(_result(0.99), 1.01) == "pass"


def _check_expense_label_with_high_ignore_score_is_decided_by_score():
    # the label field is not used by decide(): the ignore score alone decides
    assert decide(_result(0.8, label="expense"), 0.8) == "ignore"
    assert decide(_result(0.79, label="expense"), 0.8) == "pass"


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(_check_error_result_shape, id="error_result-is-an-expense-with-fixed-scores-and-no-cost"),
        pytest.param(_check_error_result_always_passes, id="error_result-passes-even-at-threshold-zero"),
        pytest.param(_check_error_wins_over_high_ignore_score, id="error-passes-despite-ignore-score-0.99"),
        pytest.param(_check_below_threshold_passes, id="ignore-score-below-threshold-passes"),
        pytest.param(_check_exactly_at_threshold_is_ignore, id="score-exactly-at-threshold-is-ignore"),
        pytest.param(_check_above_threshold_is_ignore, id="score-above-threshold-is-ignore"),
        pytest.param(_check_threshold_above_one_always_passes, id="threshold-1.01-always-passes"),
        pytest.param(_check_expense_label_with_high_ignore_score_is_decided_by_score, id="label-is-ignored-only-score-decides"),
    ],
)
def test_router_failure_or_low_ignore_score_never_drops_a_message(check):
    check()
