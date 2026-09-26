from datetime import datetime, timedelta, timezone

import pytest

from splitbot.models import ExpenseState
from splitbot.state import IllegalTransition, approval_outcome, is_expired, transition

PENDING = ExpenseState.pending_confirmation
CONFIRMED = ExpenseState.confirmed
REJECTED = ExpenseState.rejected
EXPIRED = ExpenseState.expired

TRANSITIONS = [
    (PENDING, CONFIRMED, True),
    (PENDING, REJECTED, True),
    (PENDING, EXPIRED, True),
    (PENDING, PENDING, False),
] + [(final, new, False) for final in (CONFIRMED, REJECTED, EXPIRED) for new in ExpenseState]


@pytest.mark.parametrize("current, new, legal", TRANSITIONS)
def test_illegal_state_transitions_are_rejected(current, new, legal):
    if legal:
        assert transition(current, new) == new
    else:
        with pytest.raises(IllegalTransition):
            transition(current, new)


@pytest.mark.parametrize(
    "required, approvals, expected",
    [
        ([1, 2, 3], {1: True, 2: True, 3: True}, CONFIRMED),
        ([1, 2, 3], {1: True, 2: True}, PENDING),
        ([1, 2, 3], {}, PENDING),
        ([1, 2, 3], {1: True, 2: False, 3: True}, REJECTED),  # one no cancels, others yes
        ([1, 2, 3], {1: True, 2: False}, REJECTED),  # one no cancels, one has not voted
        ([1, 2, 3], {2: False}, REJECTED),
        ([1], {1: True}, CONFIRMED),  # a group of one
        ([1], {1: False}, REJECTED),
        ([1, 2], {1: True, 9: True}, PENDING),  # a stranger's yes does not complete the set
        ([1, 2], {1: True, 2: True, 9: False}, CONFIRMED),  # a stranger's no does not cancel
        ([], {1: True}, ValueError),  # nobody required is a bug, never "confirmed"
    ],
)
def test_change_needs_all_relevant_approvals_and_one_no_cancels(required, approvals, expected):
    if expected is ValueError:
        with pytest.raises(ValueError):
            approval_outcome(required, approvals)
    else:
        assert approval_outcome(required, approvals) == expected


T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "age, expected",
    [
        (timedelta(0), False),
        (timedelta(hours=47, minutes=59, seconds=59), False),
        (timedelta(hours=48), True),
        (timedelta(hours=49), True),
    ],
)
def test_pending_items_expire_after_48_hours(age, expected):
    assert is_expired(T0, T0 + age) is expected
