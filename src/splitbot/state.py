"""Expense state machine. Pure: illegal transitions raise."""

from splitbot.models import ExpenseState as S

_ALLOWED: dict[S, set[S]] = {
    S.pending_approval: {S.approved, S.rejected, S.expired},
    S.approved: {S.submitting},
    S.submitting: {S.submitted, S.failed},
    S.failed: {S.submitting},  # retry
    S.submitted: set(),
    S.rejected: set(),
    S.expired: set(),
}


class IllegalTransition(ValueError):
    pass


def transition(current: S, new: S) -> S:
    if new not in _ALLOWED[current]:
        raise IllegalTransition(f"{current.value} -> {new.value}")
    return new
