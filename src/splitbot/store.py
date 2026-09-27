"""SQLite ledger: the single source of truth. Expenses, change requests, idempotency, and the
queries (balances, search, summaries). All money math lives here or in money.py, never in the LLM.

Only `confirmed`, not-deleted expenses count in balances, search and summaries.

One connection per Store, used from one thread; concurrent writers use one Store each on the same
database file. Concurrency rules: the database runs in WAL mode with a busy timeout (default
5000 ms) so writers queue instead of failing with "database is locked"; every write happens in a
`BEGIN IMMEDIATE` transaction (the write lock is taken BEFORE anything is read), and every state
change is additionally compare-and-swap (`... WHERE state = <state we read>`, and for both
expenses and change requests also a version counter): a write based on a stale read raises
StateConflict and stores nothing, it never silently overwrites someone else's change. A pending
expense can also be REVISED in place (`revise_pending_expense`, for a free-text correction before
it is ever confirmed), with the same state+version compare-and-swap. Idempotency per (chat_id,
message_id) is enforced by the database itself: every record (an expense OR a change request) also
writes ONE row into `message_records` with PRIMARY KEY (chat_id, message_id), so one message can never
produce both an expense and a change request, even from two threads at once.

If COMMIT itself fails, the transaction is rolled back and the Store stays usable.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS message_records (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS expenses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    state      TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
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
    version    INTEGER NOT NULL DEFAULT 0,
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


@dataclass(frozen=True)
class Expired:
    """A pending record that `expire_stale` moved to `expired`."""

    kind: Literal["expense", "change_request"]
    id: int
    chat_id: int
    message_id: int


class Store:
    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 5000):
        """Open (and create if needed) the database at `path`; ":memory:" for tests. A file
        database is switched to WAL mode; `busy_timeout_ms` is how long a writer waits for the
        lock (SQLite `busy_timeout`) before failing."""
        # isolation_level=None: no implicit transactions; every write opens its own BEGIN IMMEDIATE.
        self._db = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
        self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)

    @contextmanager
    def _tx(self):
        """A write transaction. BEGIN IMMEDIATE takes the write lock BEFORE anything is read, so a
        read-modify-write inside it cannot interleave with another writer; commits on success,
        rolls everything back when the block raises or the COMMIT itself fails."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        try:
            self._db.execute("COMMIT")
        except BaseException:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the failed COMMIT already ended the transaction
            raise

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
        with self._tx():
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
        return cursor.rowcount == 1

    # --- expenses -----------------------------------------------------------

    def save_expense(self, expense: Expense) -> Expense:
        """Insert the expense (state as given, normally pending_confirmation, version 0) and mark
        its message processed in ONE transaction. Returns it with its `id`. A second expense for
        the same (chat_id, message_id) raises DuplicateMessage and changes nothing."""
        expense = expense.model_copy(update={"version": 0})
        try:
            with self._tx():
                self._mark(expense.chat_id, expense.message_id)
                self._record(expense.chat_id, expense.message_id, "expense")
                cursor = self._db.execute(
                    "INSERT INTO expenses (chat_id, message_id, state, version, deleted, data) VALUES (?, ?, ?, 0, ?, ?)",
                    (expense.chat_id, expense.message_id, expense.state.value, int(expense.deleted),
                     expense.model_dump_json(exclude={"id"})),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMessage(f"message {expense.chat_id}:{expense.message_id} already has a record") from exc
        return expense.model_copy(update={"id": cursor.lastrowid})

    def get_expense(self, expense_id: int) -> Expense:
        """The stored expense, including soft-deleted ones (`deleted=True`). Unknown id -> KeyError."""
        row = self._db.execute(
            "SELECT id, state, version, deleted, data FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no expense {expense_id}")
        return self._to_expense(row)

    def set_state(self, expense_id: int, new_state: ExpenseState) -> Expense:
        """Move an expense through the state machine (`state.transition`; illegal -> IllegalTransition).
        The write happens only if the state AND version are still what was read (compare-and-swap):
        otherwise StateConflict. Returns the updated expense (version bumped by one)."""
        with self._tx():
            current = self.get_expense(expense_id)
            new_state = transition(current.state, new_state)
            cursor = self._db.execute(
                "UPDATE expenses SET state = ?, version = version + 1 WHERE id = ? AND state = ? AND version = ?",
                (new_state.value, expense_id, current.state.value, current.version),
            )
            if cursor.rowcount != 1:
                raise StateConflict(f"expense {expense_id} changed while we were updating it")
        return current.model_copy(update={"state": new_state, "version": current.version + 1})

    def get_expense_by_message(self, chat_id: int, message_id: int) -> Expense | None:
        """The expense created from this (chat_id, message_id), or None."""
        row = self._db.execute(
            "SELECT id, state, version, deleted, data FROM expenses WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return None if row is None else self._to_expense(row)

    def revise_pending_expense(self, expense_id: int, replacement: Expense, *, now: datetime | None = None) -> Expense:
        """Replace a PENDING expense's own content (amount/currency/payer/participants/shares/
        subcategory/description/words) with `replacement`, in place: same id, chat_id, message_id
        and author_id; `created_at` is reset to `now` (or the current wall clock if `now` is
        None), which RESETS its expiry -- the sender is actively correcting it right now.
        Compare-and-swap on state AND version (`replacement.version` must equal the currently
        stored version): a revision based on a stale read raises StateConflict and changes
        nothing, exactly like a lost-update race between two writers. The expense must still be
        pending_confirmation, else IllegalTransition (already confirmed/rejected/expired: nothing
        left to revise). When `now` is given and the CURRENT stored copy has already expired, it
        is marked expired (committed) and IllegalTransition is raised instead, same rule as
        `respond_expense`. Returns the updated expense (version bumped by one)."""
        expired = False
        with self._tx():
            current = self.get_expense(expense_id)
            if current.state == ExpenseState.pending_confirmation and now is not None and is_expired(
                current.created_at, now
            ):
                self._db.execute(
                    "UPDATE expenses SET state = ? WHERE id = ? AND state = ?",
                    (ExpenseState.expired.value, expense_id, _PENDING),
                )
                expired = True  # committed below; raising here would roll the expiry back
            else:
                if current.state != ExpenseState.pending_confirmation:
                    raise IllegalTransition(f"expense {expense_id} is already {current.state.value}, nothing to revise")
                if replacement.version != current.version:
                    raise StateConflict(f"expense {expense_id} changed while we were revising it")
                updated = replacement.model_copy(update={
                    "id": expense_id, "chat_id": current.chat_id, "message_id": current.message_id,
                    "author_id": current.author_id, "state": ExpenseState.pending_confirmation,
                    "created_at": now or datetime.now(timezone.utc), "version": current.version + 1,
                    "deleted": False,
                })
                cursor = self._db.execute(
                    "UPDATE expenses SET data = ?, version = version + 1 "
                    "WHERE id = ? AND state = ? AND version = ?",
                    (updated.model_dump_json(exclude={"id"}), expense_id, _PENDING, current.version),
                )
                if cursor.rowcount != 1:
                    raise StateConflict(f"expense {expense_id} changed while we were revising it")
        if expired:
            raise IllegalTransition(f"expense {expense_id} expired")
        return updated

    def respond_expense(
        self, expense_id: int, user_id: int, approve: bool, now: datetime | None = None
    ) -> Expense:
        """The sender's answer to a NEW expense's confirmation. Only the expense's `author_id`
        may answer (anyone else -> NotRelevantApprover); the expense must still be
        pending_confirmation (else IllegalTransition). approve=True -> confirmed, False ->
        rejected. One transaction, compare-and-swap: when several answers arrive at once exactly one
        makes the transition and the others get IllegalTransition (or StateConflict). When `now` is
        given and the expense is older than the expiry it is marked expired (committed) and
        IllegalTransition is raised, even if the sweeper has not run. The state write is
        compare-and-swap on state AND version (the same guard a revision uses), so an answer
        computed from a stale read (e.g. before a same-moment revision) raises StateConflict
        instead of silently confirming outdated content. Returns the updated expense."""
        expired = False
        with self._tx():
            expense = self.get_expense(expense_id)
            if user_id != expense.author_id:
                raise NotRelevantApprover(f"user {user_id} is not the sender of expense {expense_id}")
            if expense.state == ExpenseState.pending_confirmation and now is not None and is_expired(
                expense.created_at, now
            ):
                self._db.execute(
                    "UPDATE expenses SET state = ? WHERE id = ? AND state = ?",
                    (ExpenseState.expired.value, expense_id, _PENDING),
                )
                expired = True  # committed below; raising here would roll the expiry back
            else:
                new_state = transition(
                    expense.state, ExpenseState.confirmed if approve else ExpenseState.rejected
                )
                cursor = self._db.execute(
                    "UPDATE expenses SET state = ?, version = version + 1 WHERE id = ? AND state = ? AND version = ?",
                    (new_state.value, expense_id, expense.state.value, expense.version),
                )
                if cursor.rowcount != 1:
                    raise StateConflict(f"expense {expense_id} changed while we were updating it")
        if expired:
            raise IllegalTransition(f"expense {expense_id} expired")
        return expense.model_copy(update={"state": new_state, "version": expense.version + 1})

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
        ValueError) as pending_confirmation with NO approvals: nobody is approved automatically, the
        requester included (a group of one must press approve too, via `respond`); the change is
        never applied here. The message is marked processed and recorded in `message_records` in
        the same transaction: a (chat_id, message_id) that already produced an expense OR a change
        request raises DuplicateMessage and changes nothing. Returns the stored request."""
        stored = request.model_copy(
            update={"approvals": {}, "state": ExpenseState.pending_confirmation, "version": 0}
        )
        try:
            with self._tx():
                try:
                    target = self.get_expense(request.expense_id)
                except KeyError:
                    raise ValueError(f"no expense {request.expense_id}") from None
                if target.chat_id != request.chat_id or target.state != ExpenseState.confirmed or target.deleted:
                    raise ValueError("only a confirmed, not-deleted expense of the same chat can be changed")
                self._mark(request.chat_id, request.message_id)
                self._record(request.chat_id, request.message_id, "change_request")
                cursor = self._db.execute(
                    "INSERT INTO change_requests (chat_id, message_id, expense_id, state, version, data) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (request.chat_id, request.message_id, request.expense_id, _PENDING,
                     stored.model_dump_json(exclude={"id", "version"})),
                )
                stored = stored.model_copy(update={"id": cursor.lastrowid})
        except sqlite3.IntegrityError as exc:
            raise DuplicateMessage(f"message {request.chat_id}:{request.message_id} already has a record") from exc
        return stored

    def get_change_request(self, request_id: int) -> ChangeRequest:
        """Unknown id -> KeyError."""
        row = self._db.execute(
            "SELECT id, state, version, data FROM change_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no change request {request_id}")
        return self._to_request(row)

    def respond(self, request_id: int, user_id: int, approve: bool, now: datetime | None = None) -> ChangeRequest:
        """Record a required approver's vote and resolve the request with `state.approval_outcome`.
        Not a required approver -> NotRelevantApprover. Request no longer pending ->
        IllegalTransition. The same user voting again changes nothing (first vote stands, a
        double tap is harmless). One ✗ -> rejected, the ledger is untouched. All ✓ -> confirmed AND
        applied in the same transaction, exactly once: a correction replaces the expense's contents
        with `proposed` (same id, chat_id, message_id and created_at, state confirmed); a delete
        soft-deletes it. When `now` is given and the request is older than the expiry
        (`state.is_expired`), it is marked expired (committed) and IllegalTransition is raised: an
        approval is never accepted after expiry, even if the sweeper has not run. `now=None` skips
        the check (the bot passes its clock). The whole call runs in one `BEGIN IMMEDIATE` transaction, the state
        write is compare-and-swap on state AND a per-request version (a vote computed from a stale
        read raises StateConflict and stores nothing: votes are never lost, never overwritten),
        and if the target expense is no longer confirmed and not deleted, StateConflict and
        nothing changes."""
        expired = False
        with self._tx():
            request = self.get_change_request(request_id)
            if user_id not in request.required_approvers:
                raise NotRelevantApprover(f"user {user_id} is not a required approver")
            if request.state != ExpenseState.pending_confirmation:
                raise IllegalTransition(f"change request is already {request.state.value}")
            if now is not None and is_expired(request.created_at, now):
                self._write_request(
                    request.model_copy(update={"state": ExpenseState.expired}),
                    expected_state=_PENDING,
                    expected_version=request.version,
                )
                expired = True  # committed below; raising here would roll the expiry back
            elif user_id in request.approvals:
                return request
            else:
                approvals = {**request.approvals, user_id: approve}
                outcome = approval_outcome(request.required_approvers, approvals)
                updated = request.model_copy(update={"approvals": approvals, "state": outcome})
                if outcome == ExpenseState.confirmed:
                    self._apply(updated)
                result = self._write_request(updated, expected_state=_PENDING, expected_version=request.version)
        if expired:
            raise IllegalTransition(f"change request {request_id} expired")
        return result

    # --- expiry ---------------------------------------------------------------

    def expire_stale(self, now: datetime, expiry: timedelta | None = None) -> list[Expired]:
        """Mark every expense and change request still pending_confirmation whose
        `state.is_expired(created_at, now, expiry)` as expired (compare-and-swap, one
        transaction) and return them (the bot posts a one-line notice per item; nothing is
        written to the ledger). `expiry` defaults to `config.pending_expiry()`."""
        expired: list[Expired] = []
        with self._tx():
            for expense in self._all("expenses", _PENDING):
                if is_expired(expense.created_at, now, expiry) and self._db.execute(
                    "UPDATE expenses SET state = ?, version = version + 1 WHERE id = ? AND state = ?",
                    (ExpenseState.expired.value, expense.id, _PENDING),
                ).rowcount == 1:
                    expired.append(Expired("expense", expense.id, expense.chat_id, expense.message_id))
            for request in self._all("change_requests", _PENDING):
                if is_expired(request.created_at, now, expiry) and self._db.execute(
                    "UPDATE change_requests SET state = ?, version = version + 1 WHERE id = ? AND state = ?",
                    (ExpenseState.expired.value, request.id, _PENDING),
                ).rowcount == 1:
                    expired.append(Expired("change_request", request.id, request.chat_id, request.message_id))
        return expired

    # --- internals ------------------------------------------------------------

    def _mark(self, chat_id: int, message_id: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id)
        )

    def _record(self, chat_id: int, message_id: int, kind: str) -> None:
        """One row per message in `message_records`: a plain INSERT, so a second record for the
        same message fails with IntegrityError (the callers turn it into DuplicateMessage)."""
        self._db.execute(
            "INSERT INTO message_records (chat_id, message_id, kind) VALUES (?, ?, ?)", (chat_id, message_id, kind)
        )

    def _live(self, chat_id: int) -> list[Expense]:
        rows = self._db.execute(
            "SELECT id, state, version, deleted, data FROM expenses WHERE chat_id = ? AND state = ? AND deleted = 0 ORDER BY id",
            (chat_id, _CONFIRMED),
        ).fetchall()
        return [self._to_expense(r) for r in rows]

    def _all(self, table: str, state: str) -> list:
        if table == "expenses":
            rows = self._db.execute(
                "SELECT id, state, version, deleted, data FROM expenses WHERE state = ?", (state,)
            ).fetchall()
            return [self._to_expense(r) for r in rows]
        rows = self._db.execute(
            "SELECT id, state, version, data FROM change_requests WHERE state = ?", (state,)
        ).fetchall()
        return [self._to_request(r) for r in rows]

    def _apply(self, request: ChangeRequest) -> None:
        """Apply a confirmed change to the ledger. Call inside a transaction: raising here rolls
        it back, so a change is applied exactly once or not at all."""
        target = self._db.execute(
            "SELECT id, state, version, deleted, data FROM expenses WHERE id = ? AND state = ? AND deleted = 0",
            (request.expense_id, _CONFIRMED),
        ).fetchone()
        if target is None:
            raise StateConflict(f"expense {request.expense_id} is no longer confirmed and not deleted")
        expense = self._to_expense(target)
        if request.kind == ChangeKind.delete:
            self._db.execute("UPDATE expenses SET deleted = 1, version = version + 1 WHERE id = ?", (expense.id,))
            return
        replacement = request.proposed.model_copy(
            update={
                "id": expense.id,
                "chat_id": expense.chat_id,
                "message_id": expense.message_id,
                "created_at": expense.created_at,
                "state": ExpenseState.confirmed,
                "version": expense.version + 1,
                "deleted": False,
            }
        )
        self._db.execute(
            "UPDATE expenses SET data = ?, version = version + 1 WHERE id = ?",
            (replacement.model_dump_json(exclude={"id"}), expense.id),
        )

    def _write_request(self, request: ChangeRequest, *, expected_state: str, expected_version: int) -> ChangeRequest:
        """Compare-and-swap on state AND version: a write based on a stale read changes nothing."""
        cursor = self._db.execute(
            "UPDATE change_requests SET state = ?, data = ?, version = version + 1 "
            "WHERE id = ? AND state = ? AND version = ?",
            (request.state.value, request.model_dump_json(exclude={"id", "version"}), request.id, expected_state,
             expected_version),
        )
        if cursor.rowcount != 1:
            raise StateConflict(f"change request {request.id} changed while we were updating it")
        return request.model_copy(update={"version": expected_version + 1})

    @staticmethod
    def _to_expense(row: tuple) -> Expense:
        expense_id, state, version, deleted, data = row
        return Expense.model_validate_json(data).model_copy(
            update={"id": expense_id, "state": ExpenseState(state), "version": version, "deleted": bool(deleted)}
        )

    @staticmethod
    def _to_request(row: tuple) -> ChangeRequest:
        request_id, state, version, data = row
        return ChangeRequest.model_validate_json(data).model_copy(
            update={"id": request_id, "state": ExpenseState(state), "version": version}
        )
