"""SQLite ledger: the single source of truth. Expenses, change requests, idempotency, and the
queries (balances, search, summaries). All money math lives here or in money.py, never in the LLM.

Only `confirmed`, not-deleted expenses count in balances, search and summaries.
Single-thread use (sqlite3 default), one connection per Store.
"""

import sqlite3
from datetime import datetime
from typing import Literal

from splitbot.models import (
    Category,
    ChangeKind,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    Subcategory,
)
from splitbot.state import IllegalTransition, approval_outcome, is_expired, transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS expenses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    state      TEXT NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0,
    data       TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS change_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    expense_id INTEGER NOT NULL,
    state      TEXT NOT NULL,
    data       TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);
"""

_PENDING = ExpenseState.pending_confirmation.value
_CONFIRMED = ExpenseState.confirmed.value


class StateConflict(RuntimeError):
    """Someone else changed the record first: reload it and decide again."""


class DuplicateMessage(RuntimeError):
    """This message already produced a record."""


class NotRelevantApprover(ValueError):
    """The user is not one of the change request's required approvers."""


class Store:
    def __init__(self, path: str = ":memory:"):
        """Open (and create if needed) the database at `path`; ":memory:" for tests."""
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    # --- idempotency: one record per (chat_id, message_id) -----------------

    def is_processed(self, chat_id: int, message_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ).fetchone()
        return row is not None

    def mark_processed(self, chat_id: int, message_id: int) -> bool:
        """True the first time this message is seen, False for every repeat. For messages that
        produce no record (chat, questions). Records are saved with save_expense /
        create_change_request, which mark the message processed in the same transaction."""
        with self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
        return cursor.rowcount == 1

    # --- expenses -----------------------------------------------------------

    def save_expense(self, expense: Expense) -> Expense:
        """Insert the expense (state as given, normally pending_confirmation) and mark its message
        processed in ONE transaction. Returns it with its `id`. A second expense for the same
        (chat_id, message_id) raises DuplicateMessage and changes nothing."""
        try:
            with self._db:
                self._mark(expense.chat_id, expense.message_id)
                cursor = self._db.execute(
                    "INSERT INTO expenses (chat_id, message_id, state, deleted, data) VALUES (?, ?, ?, ?, ?)",
                    (expense.chat_id, expense.message_id, expense.state.value, int(expense.deleted),
                     expense.model_dump_json(exclude={"id"})),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMessage(f"message {expense.chat_id}:{expense.message_id} already has a record") from exc
        return expense.model_copy(update={"id": cursor.lastrowid})

    def get_expense(self, expense_id: int) -> Expense:
        """The stored expense, including soft-deleted ones (`deleted=True`). Unknown id -> KeyError."""
        row = self._db.execute(
            "SELECT id, state, deleted, data FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no expense {expense_id}")
        return self._to_expense(row)

    def set_state(self, expense_id: int, new_state: ExpenseState) -> Expense:
        """Move an expense through the state machine (`state.transition`; illegal -> IllegalTransition).
        The write happens only if the state is still what was read (compare-and-swap): otherwise
        StateConflict. Returns the updated expense."""
        current = self.get_expense(expense_id)
        new_state = transition(current.state, new_state)
        with self._db:
            cursor = self._db.execute(
                "UPDATE expenses SET state = ? WHERE id = ? AND state = ?",
                (new_state.value, expense_id, current.state.value),
            )
        if cursor.rowcount != 1:
            raise StateConflict(f"expense {expense_id} changed while we were updating it")
        return current.model_copy(update={"state": new_state})

    # --- queries (confirmed, not deleted) -------------------------------------

    def balances(self, chat_id: int) -> dict[Currency, dict[int, int]]:
        """Net balance per currency per user id, in minor units: sum over the user's shares of
        (paid - owed). Positive = the group owes them, negative = they owe. Per currency the
        numbers sum to 0. Currencies are never mixed."""
        result: dict[Currency, dict[int, int]] = {}
        for expense in self._live(chat_id):
            per_user = result.setdefault(expense.currency, {})
            for share in expense.shares:
                per_user[share.user_id] = per_user.get(share.user_id, 0) + share.paid - share.owed
        return result

    def search_expenses(
        self,
        chat_id: int,
        *,
        text: str | None = None,
        category: Category | None = None,
        subcategory: Subcategory | None = None,
        payer_id: int | None = None,
        currency: Currency | None = None,
        month: str | None = None,
        min_total: int | None = None,
        max_total: int | None = None,
        limit: int = 20,
    ) -> list[Expense]:
        """Filters are ANDed. `text`: case-insensitive substring of the description. `category`:
        the main category derived from the subcategory. `payer_id`: the user has paid > 0.
        `month`: "YYYY-MM" of `spent_on`. `min_total`/`max_total`: inclusive, minor units.
        Newest first (spent_on, then id, descending), at most `limit`."""

        def matches(e: Expense) -> bool:
            return (
                (text is None or text.casefold() in e.description.casefold())
                and (category is None or e.category == category)
                and (subcategory is None or e.subcategory == subcategory)
                and (payer_id is None or any(s.user_id == payer_id and s.paid > 0 for s in e.shares))
                and (currency is None or e.currency == currency)
                and (month is None or e.spent_on.strftime("%Y-%m") == month)
                and (min_total is None or e.total >= min_total)
                and (max_total is None or e.total <= max_total)
            )

        found = [e for e in self._live(chat_id) if matches(e)]
        found.sort(key=lambda e: (e.spent_on, e.id), reverse=True)
        return found[:limit]

    def spending_summary(
        self,
        chat_id: int,
        *,
        by: Literal["category", "subcategory", "month", "payer"],
        month: str | None = None,
    ) -> dict[Currency, dict[str, int]]:
        """Total spent (sum of `total`, minor units) per currency, grouped by the main category
        value, subcategory value, "YYYY-MM", or the payer's user id as a string (an expense with
        several payers counts each payer's paid amount). Optional `month` filter ("YYYY-MM")."""
        result: dict[Currency, dict[str, int]] = {}
        for expense in self._live(chat_id):
            if month is not None and expense.spent_on.strftime("%Y-%m") != month:
                continue
            totals = result.setdefault(expense.currency, {})
            if by == "payer":
                for share in expense.shares:
                    if share.paid > 0:
                        key = str(share.user_id)
                        totals[key] = totals.get(key, 0) + share.paid
                continue
            key = {
                "category": expense.category.value,
                "subcategory": expense.subcategory.value,
                "month": expense.spent_on.strftime("%Y-%m"),
            }[by]
            totals[key] = totals.get(key, 0) + expense.total
        return {currency: totals for currency, totals in result.items() if totals}

    # --- change requests (corrections and deletes) -----------------------------

    def create_change_request(self, request: ChangeRequest) -> ChangeRequest:
        """Store a correction/delete of a CONFIRMED, not-deleted expense of the same chat (else
        ValueError). The requester's ✓ is recorded automatically, and the message is marked
        processed in the same transaction (a repeated (chat_id, message_id) raises
        DuplicateMessage). If the requester is the only required approver the request is
        confirmed and applied immediately (a group of one). Returns the stored request."""
        try:
            target = self.get_expense(request.expense_id)
        except KeyError:
            raise ValueError(f"no expense {request.expense_id}") from None
        if target.chat_id != request.chat_id or target.state != ExpenseState.confirmed or target.deleted:
            raise ValueError("only a confirmed, not-deleted expense of the same chat can be changed")

        approvals = {**request.approvals, request.requested_by: True}
        outcome = approval_outcome(request.required_approvers, approvals)
        stored = request.model_copy(update={"approvals": approvals, "state": ExpenseState.pending_confirmation})
        try:
            with self._db:
                self._mark(request.chat_id, request.message_id)
                cursor = self._db.execute(
                    "INSERT INTO change_requests (chat_id, message_id, expense_id, state, data) VALUES (?, ?, ?, ?, ?)",
                    (request.chat_id, request.message_id, request.expense_id, _PENDING,
                     stored.model_dump_json(exclude={"id"})),
                )
                stored = stored.model_copy(update={"id": cursor.lastrowid})
                if outcome == ExpenseState.confirmed:  # a group of one
                    self._apply(stored)
                    stored = stored.model_copy(update={"state": ExpenseState.confirmed})
                    self._write_request(stored, expected_state=_PENDING)
        except sqlite3.IntegrityError as exc:
            raise DuplicateMessage(f"message {request.chat_id}:{request.message_id} already has a record") from exc
        return stored

    def get_change_request(self, request_id: int) -> ChangeRequest:
        """Unknown id -> KeyError."""
        row = self._db.execute(
            "SELECT id, state, data FROM change_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no change request {request_id}")
        return self._to_request(row)

    def respond(self, request_id: int, user_id: int, approve: bool) -> ChangeRequest:
        """Record a required approver's vote and resolve the request with `state.approval_outcome`.
        Not a required approver -> NotRelevantApprover. Request no longer pending ->
        IllegalTransition. The same user voting again changes nothing (first vote stands, a
        double tap is harmless). One ✗ -> rejected, the ledger is untouched. All ✓ -> confirmed AND
        applied in the same transaction, exactly once: a correction replaces the expense's contents
        with `proposed` (same id, chat_id, message_id and created_at, state confirmed); a delete
        soft-deletes it. The state write is compare-and-swap (StateConflict), and if the target
        expense is no longer confirmed and not deleted, StateConflict and nothing changes."""
        request = self.get_change_request(request_id)
        if user_id not in request.required_approvers:
            raise NotRelevantApprover(f"user {user_id} is not a required approver")
        if request.state != ExpenseState.pending_confirmation:
            raise IllegalTransition(f"change request is already {request.state.value}")
        if user_id in request.approvals:
            return request
        approvals = {**request.approvals, user_id: approve}
        outcome = approval_outcome(request.required_approvers, approvals)
        updated = request.model_copy(update={"approvals": approvals, "state": outcome})
        with self._db:
            if outcome == ExpenseState.confirmed:
                self._apply(updated)
            self._write_request(updated, expected_state=_PENDING)
        return updated

    # --- expiry ---------------------------------------------------------------

    def expire_stale(self, now: datetime) -> int:
        """Mark every expense and change request still pending_confirmation whose
        `state.is_expired(created_at, now)` as expired (compare-and-swap). Returns how many."""
        expired = 0
        with self._db:
            for expense in self._all("expenses", _PENDING):
                if is_expired(expense.created_at, now):
                    expired += self._db.execute(
                        "UPDATE expenses SET state = ? WHERE id = ? AND state = ?",
                        (ExpenseState.expired.value, expense.id, _PENDING),
                    ).rowcount
            for request in self._all("change_requests", _PENDING):
                if is_expired(request.created_at, now):
                    expired += self._db.execute(
                        "UPDATE change_requests SET state = ? WHERE id = ? AND state = ?",
                        (ExpenseState.expired.value, request.id, _PENDING),
                    ).rowcount
        return expired

    # --- internals ------------------------------------------------------------

    def _mark(self, chat_id: int, message_id: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id)
        )

    def _live(self, chat_id: int) -> list[Expense]:
        rows = self._db.execute(
            "SELECT id, state, deleted, data FROM expenses WHERE chat_id = ? AND state = ? AND deleted = 0 ORDER BY id",
            (chat_id, _CONFIRMED),
        ).fetchall()
        return [self._to_expense(r) for r in rows]

    def _all(self, table: str, state: str) -> list:
        if table == "expenses":
            rows = self._db.execute("SELECT id, state, deleted, data FROM expenses WHERE state = ?", (state,)).fetchall()
            return [self._to_expense(r) for r in rows]
        rows = self._db.execute("SELECT id, state, data FROM change_requests WHERE state = ?", (state,)).fetchall()
        return [self._to_request(r) for r in rows]

    def _apply(self, request: ChangeRequest) -> None:
        """Apply a confirmed change to the ledger. Call inside a transaction: raising here rolls
        it back, so a change is applied exactly once or not at all."""
        target = self._db.execute(
            "SELECT id, state, deleted, data FROM expenses WHERE id = ? AND state = ? AND deleted = 0",
            (request.expense_id, _CONFIRMED),
        ).fetchone()
        if target is None:
            raise StateConflict(f"expense {request.expense_id} is no longer confirmed and not deleted")
        expense = self._to_expense(target)
        if request.kind == ChangeKind.delete:
            self._db.execute("UPDATE expenses SET deleted = 1 WHERE id = ?", (expense.id,))
            return
        replacement = request.proposed.model_copy(
            update={
                "id": expense.id,
                "chat_id": expense.chat_id,
                "message_id": expense.message_id,
                "created_at": expense.created_at,
                "state": ExpenseState.confirmed,
                "deleted": False,
            }
        )
        self._db.execute("UPDATE expenses SET data = ? WHERE id = ?", (replacement.model_dump_json(exclude={"id"}), expense.id))

    def _write_request(self, request: ChangeRequest, *, expected_state: str) -> None:
        cursor = self._db.execute(
            "UPDATE change_requests SET state = ?, data = ? WHERE id = ? AND state = ?",
            (request.state.value, request.model_dump_json(exclude={"id"}), request.id, expected_state),
        )
        if cursor.rowcount != 1:
            raise StateConflict(f"change request {request.id} changed while we were updating it")

    @staticmethod
    def _to_expense(row: tuple) -> Expense:
        expense_id, state, deleted, data = row
        return Expense.model_validate_json(data).model_copy(
            update={"id": expense_id, "state": ExpenseState(state), "deleted": bool(deleted)}
        )

    @staticmethod
    def _to_request(row: tuple) -> ChangeRequest:
        request_id, state, data = row
        return ChangeRequest.model_validate_json(data).model_copy(
            update={"id": request_id, "state": ExpenseState(state)}
        )
