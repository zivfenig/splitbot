"""SQLite store: processed messages (idempotency) and expenses.

The outbox is not a separate table: it is a query over expenses that still have to reach
Splitwise (approved, submitting, failed). No I/O besides the database file.
"""

import sqlite3

from splitbot.models import Expense, ExpenseState
from splitbot.state import transition

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
    data       TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);
"""

_OUTBOX_STATES = (ExpenseState.approved, ExpenseState.submitting, ExpenseState.failed)


class StateConflict(RuntimeError):
    """Someone else changed the expense first: reload it and decide again."""


class DuplicateMessage(RuntimeError):
    """This message already produced an expense."""


class Store:
    def __init__(self, path: str = ":memory:"):
        # Single-thread use only (sqlite3 default): the bot runs in one asyncio loop.
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    def is_processed(self, chat_id: int, message_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ).fetchone()
        return row is not None

    def mark_processed(self, chat_id: int, message_id: int) -> bool:
        """True the first time this message is seen, False for every repeat.
        For messages that produce no expense. Expenses: `save_expense` marks it atomically."""
        with self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )
        return cursor.rowcount == 1

    def save_expense(self, expense: Expense) -> Expense:
        """Save the expense and mark its message processed in ONE transaction, so a crash can
        never leave a message marked as done without its expense. One expense per message."""
        try:
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
                    (expense.chat_id, expense.message_id),
                )
                cursor = self._db.execute(
                    "INSERT INTO expenses (chat_id, message_id, state, data) VALUES (?, ?, ?, ?)",
                    (expense.chat_id, expense.message_id, expense.state.value, expense.model_dump_json(exclude={"id"})),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMessage(f"message {expense.chat_id}:{expense.message_id} already has an expense") from exc
        return expense.model_copy(update={"id": cursor.lastrowid})

    def get_expense(self, expense_id: int) -> Expense:
        row = self._db.execute("SELECT id, state, data FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if row is None:
            raise KeyError(f"no expense {expense_id}")
        return self._to_expense(row)

    def set_state(self, expense_id: int, new_state: ExpenseState, *, splitwise_id: int | None = None) -> Expense:
        """Change state through the state machine (illegal transitions raise). The write only
        happens if the state is still what we read (compare-and-swap), else StateConflict."""
        current = self.get_expense(expense_id)
        new_state = transition(current.state, new_state)
        if new_state == ExpenseState.submitted and splitwise_id is None and current.splitwise_id is None:
            raise ValueError("`submitted` needs the Splitwise id")
        updated = current.model_copy(
            update={
                "state": new_state,
                "splitwise_id": splitwise_id if splitwise_id is not None else current.splitwise_id,
            }
        )
        with self._db:
            cursor = self._db.execute(
                "UPDATE expenses SET state = ?, data = ? WHERE id = ? AND state = ?",
                (updated.state.value, updated.model_dump_json(exclude={"id"}), expense_id, current.state.value),
            )
        if cursor.rowcount != 1:
            raise StateConflict(f"expense {expense_id} changed while we were updating it")
        return updated

    def find_submitting(self) -> list[Expense]:
        """Expenses stuck in `submitting`: search Splitwise for our key before any retry."""
        return self._by_states((ExpenseState.submitting,))

    def outbox(self) -> list[Expense]:
        return self._by_states(_OUTBOX_STATES)

    def _by_states(self, states: tuple[ExpenseState, ...]) -> list[Expense]:
        marks = ",".join("?" for _ in states)
        rows = self._db.execute(
            f"SELECT id, state, data FROM expenses WHERE state IN ({marks}) ORDER BY id",
            [s.value for s in states],
        ).fetchall()
        return [self._to_expense(r) for r in rows]

    @staticmethod
    def _to_expense(row: tuple) -> Expense:
        expense_id, state, data = row
        return Expense.model_validate_json(data).model_copy(update={"id": expense_id, "state": ExpenseState(state)})
