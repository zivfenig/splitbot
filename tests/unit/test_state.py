import pytest

from splitbot.models import ExpenseState as S
from splitbot.state import IllegalTransition, transition


@pytest.mark.parametrize(
    "current, new, legal",
    [
        (S.pending_approval, S.approved, True),
        (S.pending_approval, S.rejected, True),
        (S.pending_approval, S.expired, True),
        (S.approved, S.submitting, True),
        (S.submitting, S.submitted, True),
        (S.submitting, S.failed, True),
        (S.failed, S.submitting, True),  # retry
        (S.pending_approval, S.submitted, False),
        (S.pending_approval, S.submitting, False),
        (S.approved, S.submitted, False),  # must go through submitting
        (S.submitted, S.submitting, False),
        (S.submitted, S.failed, False),
        (S.rejected, S.approved, False),
        (S.expired, S.approved, False),
        (S.failed, S.submitted, False),  # a retry must go through submitting
    ],
)
def test_illegal_state_transitions_are_rejected(current, new, legal):
    if legal:
        assert transition(current, new) == new
    else:
        with pytest.raises(IllegalTransition):
            transition(current, new)
