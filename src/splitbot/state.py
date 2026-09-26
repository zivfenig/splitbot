"""State machine for expenses and change requests, and the approval outcome. Pure.

States: pending_confirmation -> confirmed | rejected | expired. confirmed, rejected and expired
are final. A change request (correction/delete) uses the same states.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from splitbot.models import ExpenseState

EXPIRY = timedelta(hours=48)

_ALLOWED: dict[ExpenseState, set[ExpenseState]] = {
    ExpenseState.pending_confirmation: {ExpenseState.confirmed, ExpenseState.rejected, ExpenseState.expired},
    ExpenseState.confirmed: set(),
    ExpenseState.rejected: set(),
    ExpenseState.expired: set(),
}


class IllegalTransition(ValueError):
    pass


def transition(current: ExpenseState, new: ExpenseState) -> ExpenseState:
    """Return `new` if `current -> new` is allowed, else raise IllegalTransition."""
    if new not in _ALLOWED[current]:
        raise IllegalTransition(f"{current.value} -> {new.value}")
    return new


def approval_outcome(required: Iterable[int], approvals: Mapping[int, bool]) -> ExpenseState:
    """State a change request should be in, given who must approve and the votes so far
    (user id -> True for ✓, False for ✗).

    Any ✗ from a required approver -> rejected (one ✗ cancels). Otherwise, every required
    approver has said ✓ -> confirmed. Otherwise -> pending_confirmation. Votes from people who
    are not required are ignored. Nobody required -> raises ValueError.
    """
    needed = set(required)
    if not needed:
        raise ValueError("nobody is required to approve")
    votes = {user: vote for user, vote in approvals.items() if user in needed}
    if any(vote is False for vote in votes.values()):
        return ExpenseState.rejected
    if all(votes.get(user) is True for user in needed):
        return ExpenseState.confirmed
    return ExpenseState.pending_confirmation


def is_expired(created_at: datetime, now: datetime) -> bool:
    """True when at least EXPIRY (48 hours) have passed since `created_at` (timezone-aware
    datetimes; exactly 48 hours counts as expired)."""
    return now - created_at >= EXPIRY
