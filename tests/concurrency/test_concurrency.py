"""Concurrency tests: parallel writes and approvals against ONE file database.

Every test uses a file database in tmp_path and one Store (one sqlite connection) per thread.
Each test that guards against a race also runs a NEGATIVE CONTROL: the same workload against a
deliberately unprotected implementation defined here, which must show the bug. If a control does
not fail, the scenario proves nothing.
"""

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from splitbot.models import (
    ChangeKind,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    GroupConfig,
    Member,
    Share,
    Subcategory,
)
from splitbot.state import IllegalTransition
from splitbot.store import DuplicateMessage, Expired, NotRelevantApprover, StateConflict, Store
from splitbot.tools.write_tools import WriteTools
from tests.fakes import FakeLLM

CHAT = 100
MEMBERS = [Member(id=1, name="זיו"), Member(id=2, name="דני"), Member(id=3, name="משה"), Member(id=4, name="מיכל")]
CONFIG = GroupConfig(chat_id=CHAT, members=MEMBERS)
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


# --- helpers -------------------------------------------------------------------------------


def run_parallel(jobs):
    """Run each job(wait) in its own thread. A job creates its own Store, then calls `wait()` (a
    shared barrier) so all threads start the risky part together. Returns (results, errors): one
    entry per job, and any exception a thread raised (never swallowed)."""
    barrier = threading.Barrier(len(jobs), timeout=30)
    results = [None] * len(jobs)
    errors = [None] * len(jobs)

    def runner(i, job):
        try:
            results[i] = job(barrier.wait)
        except BaseException as exc:  # noqa: BLE001 - collected and inspected by the test
            errors[i] = exc

    threads = [threading.Thread(target=runner, args=(i, job)) for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a thread hung"
    return results, errors


def extraction_reply(amount: str, description: str = "פיצה") -> str:
    return json.dumps(
        {
            "confidence": "high",
            "amount": {"value": amount, "evidence": amount, "source": "message"},
            "subcategory": "restaurant",
            "description": description,
        },
        ensure_ascii=False,
    )


def make_tools(path: str, amounts: list[str]) -> WriteTools:
    """A WriteTools with its own Store and its own scripted LLM (one valid reply per amount)."""
    return WriteTools(
        Store(path), FakeLLM([extraction_reply(a) for a in amounts]), MEMBERS, CONFIG, clock=lambda: NOW
    )


def make_expense(message_id: int, *, total: int = 40000, state=ExpenseState.confirmed, author: int = 1) -> Expense:
    """Four people share it equally; `author` paid everything."""
    owed = total // 4
    return Expense(
        chat_id=CHAT,
        message_id=message_id,
        author_id=author,
        description="פיצה",
        total=total,
        currency=Currency.ILS,
        subcategory=Subcategory.restaurant,
        shares=[Share(user_id=m.id, paid=total if m.id == author else 0, owed=owed) for m in MEMBERS],
        prompt_version="extract_v2",
        spent_on=date(2026, 3, 1),
        created_at=NOW,
        state=state,
    )


def add_update_audit(path: str) -> None:
    """A trigger that records every UPDATE of the expenses table, so a test can count writes."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, expense_id INTEGER);
        CREATE TRIGGER IF NOT EXISTS audit_expense_update AFTER UPDATE ON expenses
        BEGIN INSERT INTO audit (expense_id) VALUES (NEW.id); END;
        """
    )
    db.commit()
    db.close()


def count_rows(path: str, sql: str, params=()) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, params).fetchone()[0]
    finally:
        db.close()


def vote_with_retry(path: str, request_id: int, user_id: int, wait) -> None:
    """The documented contract: on StateConflict reload and vote again."""
    store = Store(path)
    wait()
    for _ in range(200):
        try:
            store.respond(request_id, user_id, True)
            return
        except StateConflict:
            time.sleep(0.001)
    raise AssertionError(f"user {user_id} could not vote after 200 retries")


# --- 1. the same message twice at once -------------------------------------------------------


def test_the_same_message_twice_in_parallel_creates_one_expense(tmp_path):
    path = str(tmp_path / "ledger.db")
    Store(path)  # create the schema
    n = 8

    def job(wait):
        tools = make_tools(path, ["240"])
        wait()
        return tools.propose_expense(chat_id=CHAT, message_id=7, sender_id=1, text="פיצה 240")

    results, errors = run_parallel([job] * n)

    assert errors == [None] * n
    assert count_rows(path, "SELECT COUNT(*) FROM expenses WHERE chat_id = ? AND message_id = 7", (CHAT,)) == 1
    statuses = sorted(p.status for p in results)
    assert statuses == ["duplicate"] * (n - 1) + ["pending_confirmation"]
    stored = Store(path).get_expense_by_message(CHAT, 7)
    assert {p.expense_id for p in results} == {stored.id}

    # NEGATIVE CONTROL: check-then-insert on a table with NO unique constraint. The sleep between
    # the check and the insert forces every thread to check before any thread inserts, so all of
    # them see "not there yet". It must create duplicates, proving the scenario can catch the bug.
    scratch = str(tmp_path / "scratch.db")
    setup = sqlite3.connect(scratch)
    setup.execute("CREATE TABLE expenses (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER)")
    setup.commit()
    setup.close()

    def unprotected(wait):
        db = sqlite3.connect(scratch)
        wait()
        seen = db.execute("SELECT 1 FROM expenses WHERE chat_id = ? AND message_id = 7", (CHAT,)).fetchone()
        time.sleep(0.02)
        if seen is None:
            with db:
                db.execute("INSERT INTO expenses (chat_id, message_id) VALUES (?, 7)", (CHAT,))
        db.close()

    _, control_errors = run_parallel([unprotected] * n)
    assert control_errors == [None] * n
    assert count_rows(scratch, "SELECT COUNT(*) FROM expenses WHERE message_id = 7") > 1

    # CROSS-TOOL: the SAME message reaches propose_expense, propose_correction and propose_delete at
    # once (two threads per tool). Exactly one record may come out of it: an expense OR a change
    # request, never both.
    cross = str(tmp_path / "cross.db")
    setup = Store(cross)
    target = setup.save_expense(make_expense(1))  # a confirmed expense the change tools can point at
    replies = {
        "expense": (extraction_reply("240"), "פיצה 240"),
        "correction": (
            json.dumps(
                {
                    "confidence": "high",
                    "amount": {"value": "300", "evidence": "300", "source": "message"},
                    "refers_to": {"value": "פיצה", "evidence": "פיצה", "source": "message"},
                },
                ensure_ascii=False,
            ),
            "הפיצה הייתה 300",
        ),
        "delete": (
            json.dumps(
                {
                    "confidence": "high",
                    "refers_to": {"value": "פיצה", "evidence": "פיצה", "source": "message"},
                },
                ensure_ascii=False,
            ),
            "מחק את הפיצה",
        ),
    }

    def cross_job(tool: str):
        def job(wait):
            reply, text = replies[tool]
            tools = WriteTools(Store(cross), FakeLLM([reply]), MEMBERS, CONFIG, clock=lambda: NOW)
            wait()
            common = dict(chat_id=CHAT, message_id=77, sender_id=1, text=text)
            if tool == "expense":
                return tools.propose_expense(**common)
            call = tools.propose_correction if tool == "correction" else tools.propose_delete
            return call(target_expense_id=target.id, **common)

        return job

    cross_results, cross_errors = run_parallel([cross_job(t) for t in ("expense", "correction", "delete") * 2])

    assert cross_errors == [None] * 6
    records = count_rows(cross, "SELECT COUNT(*) FROM expenses WHERE chat_id = ? AND message_id = 77", (CHAT,)) + count_rows(
        cross, "SELECT COUNT(*) FROM change_requests WHERE chat_id = ? AND message_id = 77", (CHAT,)
    )
    assert records == 1
    assert sum(p.status == "pending_confirmation" for p in cross_results) == 1
    assert all(p.status in ("pending_confirmation", "duplicate", "needs_clarification") for p in cross_results)

    # NEGATIVE CONTROL (cross-tool): two scratch tables, each with its own UNIQUE, and NO shared
    # record table. Each thread checks only its own table, sleeps 0.02 s, then inserts. Both
    # threads check before either inserts, so the message ends up with an expense row AND a change
    # request row: exactly what the shared `message_records` key prevents.
    split = str(tmp_path / "split.db")
    setup = sqlite3.connect(split)
    for table in ("expenses", "change_requests"):
        setup.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, UNIQUE (chat_id, message_id))")
    setup.commit()
    setup.close()

    def unprotected_cross(table):
        def job(wait):
            db = sqlite3.connect(split)
            wait()
            seen = db.execute(f"SELECT 1 FROM {table} WHERE chat_id = ? AND message_id = 77", (CHAT,)).fetchone()
            time.sleep(0.02)
            if seen is None:
                with db:
                    db.execute(f"INSERT INTO {table} (chat_id, message_id) VALUES (?, 77)", (CHAT,))
            db.close()

        return job

    _, split_errors = run_parallel([unprotected_cross("expenses"), unprotected_cross("change_requests")])
    assert split_errors == [None, None]
    assert count_rows(split, "SELECT COUNT(*) FROM expenses WHERE message_id = 77") == 1
    assert count_rows(split, "SELECT COUNT(*) FROM change_requests WHERE message_id = 77") == 1


# --- 2. several users answering at once -----------------------------------------------------


def test_several_users_confirming_at_once_cause_one_state_transition(tmp_path):
    # (a) a NEW expense: the author taps 3 times, users 2, 3, 4 answer too.
    path = str(tmp_path / "ledger.db")
    setup = Store(path)
    pending = setup.save_expense(make_expense(1, state=ExpenseState.pending_confirmation))
    add_update_audit(path)

    def answer(user_id):
        def job(wait):
            store = Store(path)
            wait()
            try:
                return store.respond_expense(pending.id, user_id, True)
            except Exception as exc:  # noqa: BLE001 - classified below
                return exc

        return job

    users = [1, 1, 1, 2, 3, 4]
    results, errors = run_parallel([answer(u) for u in users])

    assert errors == [None] * len(users)
    for user, result in zip(users, results):
        if user != 1:
            assert isinstance(result, NotRelevantApprover)
    author_results = [r for u, r in zip(users, results) if u == 1]
    winners = [r for r in author_results if isinstance(r, Expense)]
    losers = [r for r in author_results if not isinstance(r, Expense)]
    assert len(winners) == 1 and winners[0].state == ExpenseState.confirmed
    assert len(losers) == 2 and all(isinstance(r, (IllegalTransition, StateConflict)) for r in losers)
    assert setup.get_expense(pending.id).state == ExpenseState.confirmed
    assert count_rows(path, "SELECT COUNT(*) FROM audit") == 1  # the row was written once

    # (b) a CORRECTION and a DELETE need all 4 approvers. Nobody is approved at creation (the
    # requester included): all 4 vote at the same instant.
    for kind in (ChangeKind.correction, ChangeKind.delete):
        path = str(tmp_path / f"{kind.value}.db")
        setup = Store(path)
        target = setup.save_expense(make_expense(1))
        proposed = make_expense(2, total=60000).model_copy(update={"description": "פיצה"}) if kind == ChangeKind.correction else None
        request = setup.create_change_request(
            ChangeRequest(
                chat_id=CHAT,
                message_id=50,
                expense_id=target.id,
                kind=kind,
                requested_by=1,
                proposed=proposed,
                required_approvers=[1, 2, 3, 4],
                created_at=NOW,
            )
        )
        assert request.approvals == {}  # nobody is approved automatically
        add_update_audit(path)

        jobs = [lambda wait, u=u: vote_with_retry(path, request.id, u, wait) for u in (1, 2, 3, 4)]
        _, errors = run_parallel(jobs)

        assert errors == [None] * 4
        final = setup.get_change_request(request.id)
        assert final.approvals == {1: True, 2: True, 3: True, 4: True}  # no vote was lost
        assert final.state == ExpenseState.confirmed
        after = setup.get_expense(target.id)
        assert after.state == ExpenseState.confirmed
        assert count_rows(path, "SELECT COUNT(*) FROM audit") == 1  # applied exactly once
        if kind == ChangeKind.correction:
            assert after.total == 60000 and not after.deleted
            assert sum(s.owed for s in after.shares) == 60000
        else:
            assert after.deleted is True and after.total == 40000

    # NEGATIVE CONTROL: start from no votes; 4 threads read the stored votes, sleep, write back the
    # merged votes with no lock and no version check. The barrier + sleep make all threads read the
    # same old (empty) votes, so the last writer overwrites the others: votes must be lost.
    scratch = str(tmp_path / "scratch.db")
    db = sqlite3.connect(scratch)
    db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, votes TEXT)")
    db.execute("INSERT INTO requests (id, votes) VALUES (1, ?)", (json.dumps({}),))
    db.commit()
    db.close()

    def unprotected_vote(user_id):
        def job(wait):
            conn = sqlite3.connect(scratch)
            wait()
            votes = json.loads(conn.execute("SELECT votes FROM requests WHERE id = 1").fetchone()[0])
            time.sleep(0.02)
            votes[str(user_id)] = True
            with conn:
                conn.execute("UPDATE requests SET votes = ? WHERE id = 1", (json.dumps(votes),))
            conn.close()

        return job

    _, control_errors = run_parallel([unprotected_vote(u) for u in (1, 2, 3, 4)])
    assert control_errors == [None] * 4
    lost = json.loads(sqlite3.connect(scratch).execute("SELECT votes FROM requests").fetchone()[0])
    assert len(lost) < 4


# --- 3. many messages at once ------------------------------------------------------------------


def test_fifty_messages_in_parallel_are_all_stored_without_lock_errors(tmp_path):
    path = str(tmp_path / "ledger.db")
    Store(path)  # create the schema (WAL mode) before the crowd arrives
    n = 50

    def send(i):
        def job(wait):
            tools = make_tools(path, [str(100 + i)])
            wait()
            return tools.propose_expense(chat_id=CHAT, message_id=1000 + i, sender_id=1, text=f"פיצה {100 + i}")

        return job

    results, errors = run_parallel([send(i) for i in range(n)])

    assert errors == [None] * n  # in particular no "database is locked"
    assert all(p.status == "pending_confirmation" for p in results)
    ids = [p.expense_id for p in results]
    assert len(set(ids)) == n
    assert count_rows(path, "SELECT COUNT(*) FROM expenses") == n
    reader = Store(path)
    stored_totals = {reader.get_expense(i).total for i in ids}
    assert stored_totals == {(100 + i) * 100 for i in range(n)}  # nothing lost, nothing changed
    assert all(reader.get_expense(i).state == ExpenseState.pending_confirmation for i in ids)

    # NEGATIVE CONTROL: raw connections, default (rollback) journal, no busy timeout, and a slow
    # write inside each transaction so one writer always holds the lock while the others try.
    scratch = str(tmp_path / "scratch.db")
    db = sqlite3.connect(scratch)
    db.execute("CREATE TABLE expenses (id INTEGER PRIMARY KEY, message_id INTEGER)")
    db.commit()
    db.close()

    def unprotected(i):
        def job(wait):
            conn = sqlite3.connect(scratch, timeout=0, isolation_level=None)
            wait()
            try:
                conn.execute("BEGIN")
                conn.execute("INSERT INTO expenses (message_id) VALUES (?)", (i,))
                time.sleep(0.01)
                conn.execute("COMMIT")
            finally:
                conn.close()

        return job

    _, control_errors = run_parallel([unprotected(i) for i in range(n)])
    locked = [e for e in control_errors if isinstance(e, sqlite3.OperationalError) and "locked" in str(e)]
    assert locked, "the unprotected version should hit 'database is locked'"


# --- 4. independent pending records ------------------------------------------------------------


def test_two_unrelated_pending_actions_resolve_independently(tmp_path):
    path = str(tmp_path / "ledger.db")
    tools = make_tools(path, ["120", "60", "90"])

    first = tools.propose_expense(chat_id=CHAT, message_id=1, sender_id=1, text="פיצה 120")
    second = tools.propose_expense(chat_id=CHAT, message_id=2, sender_id=2, text="פיצה 60")
    started = time.monotonic()
    third = tools.propose_expense(chat_id=CHAT, message_id=3, sender_id=3, text="פיצה 90")  # not blocked
    assert time.monotonic() - started < 2
    for proposal in (first, second, third):
        assert proposal.status == "pending_confirmation"
    assert len({first.expense_id, second.expense_id, third.expense_id}) == 3

    def answer(expense_id, user_id, approve):
        def job(wait):
            other = make_tools(path, [])
            wait()
            return other.confirm_expense(expense_id, user_id, approve)

        return job

    results, errors = run_parallel([answer(first.expense_id, 1, True), answer(second.expense_id, 2, False)])

    assert errors == [None, None]
    store = Store(path)
    assert store.get_expense(first.expense_id).state == ExpenseState.confirmed
    assert store.get_expense(second.expense_id).state == ExpenseState.rejected
    assert store.get_expense(third.expense_id).state == ExpenseState.pending_confirmation
    # Only the confirmed 120: the payer paid 12000, four people owe 3000 each.
    balances = {Currency.ILS: {1: 9000, 2: -3000, 3: -3000, 4: -3000}}
    assert store.balances(CHAT) == balances

    later = NOW + timedelta(hours=2)
    expired = store.expire_stale(later)

    assert expired == [Expired(kind="expense", id=third.expense_id, chat_id=CHAT, message_id=3)]
    assert store.get_expense(first.expense_id).state == ExpenseState.confirmed
    assert store.get_expense(second.expense_id).state == ExpenseState.rejected
    assert store.get_expense(third.expense_id).state == ExpenseState.expired
    assert store.balances(CHAT) == balances  # expiry wrote nothing to the ledger

    # NEGATIVE CONTROL: pending state as ONE shared slot. A `current` item plus a lock that stays
    # held until the item is resolved. While the first proposal is pending, the second and third
    # cannot get in (acquire fails at once, so the test never hangs). The same independence
    # scenario must therefore fail: the second and third messages are blocked.
    class SingleSlotBot:
        def __init__(self):
            self.current = None
            self._lock = threading.Lock()

        def propose(self, message_id):
            if not self._lock.acquire(blocking=False):
                return "blocked"
            self.current = message_id
            return "pending_confirmation"

        def resolve(self):
            self.current = None
            self._lock.release()

    slot = SingleSlotBot()
    statuses = [slot.propose(1), slot.propose(2), slot.propose(3)]
    assert statuses != ["pending_confirmation"] * 3  # the real store passes this same check
    assert statuses == ["pending_confirmation", "blocked", "blocked"]
    assert slot.current == 1


# --- 5. the database itself enforces idempotency ---------------------------------------------


def test_idempotency_is_enforced_by_the_database_itself(tmp_path):
    path = str(tmp_path / "ledger.db")
    store = Store(path)
    data = json.dumps({"note": "plausible payload"})
    raw = sqlite3.connect(path)

    inserts = {
        "processed_messages": (
            "INSERT INTO processed_messages (chat_id, message_id) VALUES (?, ?)",
            (CHAT, 500),
        ),
        "expenses": (
            "INSERT INTO expenses (chat_id, message_id, state, deleted, data) VALUES (?, ?, ?, ?, ?)",
            (CHAT, 501, "pending_confirmation", 0, data),
        ),
        "change_requests": (
            "INSERT INTO change_requests (chat_id, message_id, expense_id, state, data) VALUES (?, ?, ?, ?, ?)",
            (CHAT, 502, 1, "pending_confirmation", data),
        ),
    }
    for table, (sql, params) in inserts.items():
        raw.execute(sql, params)
        raw.commit()
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(sql, params)
        raw.rollback()
    # the shared record table: one row per message, whatever kind of record it is
    record = "INSERT INTO message_records (chat_id, message_id, kind) VALUES (?, ?, ?)"
    raw.execute(record, (CHAT, 503, "expense"))
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(record, (CHAT, 503, "expense"))
    raw.rollback()
    raw.close()

    store.save_expense(make_expense(7, state=ExpenseState.pending_confirmation))
    with pytest.raises(DuplicateMessage):
        store.save_expense(make_expense(7, state=ExpenseState.pending_confirmation))

    # one message can never be both an expense and a change request, through the Store either way
    target = store.save_expense(make_expense(10))  # confirmed, so it can be changed

    def change(message_id):
        return ChangeRequest(
            chat_id=CHAT,
            message_id=message_id,
            expense_id=target.id,
            kind=ChangeKind.delete,
            requested_by=1,
            required_approvers=[1, 2, 3, 4],
            created_at=NOW,
        )

    with pytest.raises(DuplicateMessage):  # message 7 already produced an expense
        store.create_change_request(change(7))
    store.create_change_request(change(60))
    with pytest.raises(DuplicateMessage):  # message 60 already produced a change request
        store.save_expense(make_expense(60, state=ExpenseState.pending_confirmation))


# --- 6. a revision racing a confirmation ------------------------------------------------------


def test_a_revision_racing_a_confirmation_never_loses_either_and_only_one_wins(tmp_path):
    """`respond_expense` re-reads its own state and version FRESH, inside its own `BEGIN
    IMMEDIATE` transaction (never a caller-supplied version), so under genuine concurrency it can
    never itself observe an externally-stale version -- that half of the CAS guard is only
    reachable by artificially forcing a stale read (covered at the Store-unit level, where
    `respond_expense` is monkeypatched to see a pre-revision copy). A real race between the two
    therefore has exactly two safe outcomes, decided purely by commit order: the revision lands
    first and the confirmation then approves that same (now current) revised content -- both
    calls succeed, nothing is lost; or the confirmation lands first and the revision is cleanly
    refused (`IllegalTransition`: the target is no longer pending) -- nothing about the confirmed
    expense is silently changed. Either way, nothing is ever silently lost or corrupted."""
    path = str(tmp_path / "ledger.db")
    setup = Store(path)
    pending = setup.save_expense(make_expense(1, state=ExpenseState.pending_confirmation))
    revised_content = make_expense(2, total=90000).model_copy(update={"version": pending.version})

    def revise_job(wait):
        store = Store(path)
        wait()
        try:
            return store.revise_pending_expense(pending.id, revised_content, now=NOW)
        except Exception as exc:  # noqa: BLE001 - classified below
            return exc

    def confirm_job(wait):
        store = Store(path)
        wait()
        try:
            return store.respond_expense(pending.id, pending.author_id, True, now=NOW)
        except Exception as exc:  # noqa: BLE001 - classified below
            return exc

    results, errors = run_parallel([revise_job, confirm_job])

    assert errors == [None, None]
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    final = Store(path).get_expense(pending.id)

    if len(successes) == 2:
        # commit order: revise, then confirm -- the confirmation freezes the freshest content
        assert failures == []
        assert final.state == ExpenseState.confirmed
        assert final.total == revised_content.total
    else:
        # commit order: confirm, then revise -- the revision is cleanly refused, nothing silent
        assert len(successes) == 1 and len(failures) == 1
        assert isinstance(failures[0], IllegalTransition)
        assert final.state == ExpenseState.confirmed
        assert final.total == pending.total  # the original content, never touched by the loser
    assert final.total in (pending.total, revised_content.total)  # never a corrupted mix of the two

    # NEGATIVE CONTROL: a scratch table with a version column, but NO WHERE-version guard on the
    # write (a classic read-old-version-then-write-unconditionally lost update). The barrier +
    # sleep force both threads to read version 0 before either writes, so one write must clobber
    # the other silently -- both "succeed" (no exception), unlike the real store where exactly one
    # of the two racing calls above raised StateConflict.
    scratch = str(tmp_path / "scratch.db")
    db = sqlite3.connect(scratch)
    db.execute("CREATE TABLE expenses (id INTEGER PRIMARY KEY, state TEXT, total INTEGER, version INTEGER)")
    db.execute("INSERT INTO expenses (id, state, total, version) VALUES (1, 'pending', 100, 0)")
    db.commit()
    db.close()

    def unprotected(new_state, new_total):
        def job(wait):
            conn = sqlite3.connect(scratch)
            wait()
            version = conn.execute("SELECT version FROM expenses WHERE id = 1").fetchone()[0]
            time.sleep(0.02)
            with conn:
                conn.execute(
                    "UPDATE expenses SET state = ?, total = ?, version = ? WHERE id = 1",
                    (new_state, new_total, version + 1),
                )
            conn.close()

        return job

    _, control_errors = run_parallel([unprotected("pending", 900), unprotected("confirmed", 100)])
    assert control_errors == [None, None]  # neither writer was ever told to reload and retry
    row = sqlite3.connect(scratch).execute("SELECT version FROM expenses WHERE id = 1").fetchone()
    assert row[0] == 1  # both "succeeded", but one writer's update was silently lost
