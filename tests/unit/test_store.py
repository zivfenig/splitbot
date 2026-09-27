import itertools
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from splitbot.models import (
    Category,
    ChangeKind,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    Share,
    Subcategory,
)
from splitbot.state import IllegalTransition
from splitbot.store import DuplicateMessage, Expired, NotRelevantApprover, StateConflict, Store

ILS, USD = Currency.ILS, Currency.USD
PENDING, CONFIRMED = ExpenseState.pending_confirmation, ExpenseState.confirmed

_message_ids = itertools.count(1000)


def shares_of(paid: dict[int, int], owed: dict[int, int]) -> list[Share]:
    return [Share(user_id=u, paid=paid.get(u, 0), owed=owed.get(u, 0)) for u in sorted(paid.keys() | owed.keys())]


def make_expense(
    *,
    chat_id=1,
    message_id=None,
    description="dinner",
    total=9000,
    currency=ILS,
    subcategory=Subcategory.restaurant,
    shares=None,
    spent_on=date(2026, 1, 10),
    state=PENDING,
    created_at=None,
) -> Expense:
    """Default: user 1 paid 90.00, users 1, 2, 3 owe 30.00 each."""
    if shares is None:
        shares = shares_of({1: total}, {1: total // 3, 2: total // 3, 3: total // 3})
    extra = {"created_at": created_at} if created_at else {}
    return Expense(
        chat_id=chat_id,
        message_id=message_id if message_id is not None else next(_message_ids),
        author_id=1,
        description=description,
        total=total,
        currency=currency,
        subcategory=subcategory,
        shares=shares,
        prompt_version="extract_v1",
        spent_on=spent_on,
        state=state,
        **extra,
    )


def save_confirmed(store: Store, **kwargs) -> Expense:
    saved = store.save_expense(make_expense(**kwargs))
    return store.set_state(saved.id, CONFIRMED)


def make_change(expense: Expense, kind=ChangeKind.delete, *, by=1, message_id=None, proposed=None, approvers=None, created_at=None):
    if approvers is None:
        approvers = sorted(s.user_id for s in expense.shares if s.paid > 0 or s.owed > 0)
    extra = {"created_at": created_at} if created_at else {}
    return ChangeRequest(
        chat_id=expense.chat_id,
        message_id=message_id if message_id is not None else next(_message_ids),
        expense_id=expense.id,
        kind=kind,
        requested_by=by,
        proposed=proposed,
        required_approvers=approvers,
        **extra,
    )


def delete_by_everyone(store: Store, expense: Expense) -> None:
    request = store.create_change_request(make_change(expense))
    for user in request.required_approvers:  # nobody is approved automatically, the requester votes too
        request = store.respond(request.id, user, True)
    assert request.state == CONFIRMED


# --- 1 --------------------------------------------------------------------------------------


def test_one_expense_per_message_and_survives_restart(tmp_path):
    store = Store(":memory:")
    assert store.mark_processed(1, 10) is True
    assert store.mark_processed(1, 10) is False
    assert store.is_processed(1, 10)
    assert not store.is_processed(1, 11)
    assert not store.is_processed(2, 10)  # same message id in another chat
    assert store.mark_processed(2, 10) is True

    first = store.save_expense(make_expense(message_id=20, description="first"))
    assert first.id is not None
    assert store.is_processed(1, 20)
    with pytest.raises(DuplicateMessage):
        store.save_expense(make_expense(message_id=20, description="second"))
    assert store.get_expense(first.id) == first
    assert store.get_expense_by_message(1, 20) == first
    assert store.get_expense_by_message(1, 10) is None  # processed, but no expense
    assert store.get_expense_by_message(1, 21) is None
    assert store.get_expense_by_message(2, 20) is None  # same message id, another chat
    with pytest.raises(KeyError):
        store.get_expense(first.id + 1)  # the duplicate left no row behind

    # a message marked processed earlier (no record yet) can still get its one expense
    store.mark_processed(1, 30)
    store.save_expense(make_expense(message_id=30))
    with pytest.raises(DuplicateMessage):
        store.save_expense(make_expense(message_id=30))

    # one message = one record: an expense and a change request can never share a (chat, message)
    target = save_confirmed(store, message_id=50)
    with pytest.raises(DuplicateMessage):
        store.create_change_request(make_change(target, message_id=20))  # 20 already produced an expense
    with pytest.raises(KeyError):
        store.get_change_request(1)  # ...and nothing was stored
    store.create_change_request(make_change(target, message_id=60))
    with pytest.raises(DuplicateMessage):
        store.save_expense(make_expense(message_id=60))  # 60 already produced a change request
    assert store.get_expense_by_message(1, 60) is None
    assert store.get_expense(target.id) == target

    path = str(tmp_path / "ledger.db")
    original = Store(path)
    saved = original.save_expense(make_expense(message_id=40))
    original.mark_processed(1, 41)
    file_target = save_confirmed(original, message_id=42)
    original.create_change_request(make_change(file_target, message_id=43))
    reopened = Store(path)
    assert reopened.get_expense(saved.id) == saved
    with pytest.raises(DuplicateMessage):
        reopened.create_change_request(make_change(file_target, message_id=40))  # 40 is an expense
    with pytest.raises(DuplicateMessage):
        reopened.save_expense(make_expense(message_id=43))  # 43 is a change request
    assert reopened.get_expense_by_message(1, 40) == saved
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    Store(str(tmp_path / "other.db"), busy_timeout_ms=1234)  # the busy timeout is accepted
    assert reopened.is_processed(1, 40)
    assert reopened.is_processed(1, 41)
    assert reopened.mark_processed(1, 41) is False
    with pytest.raises(DuplicateMessage):
        reopened.save_expense(make_expense(message_id=40))


# --- 2 --------------------------------------------------------------------------------------


def test_state_changes_are_compare_and_swap(monkeypatch):
    store = Store(":memory:")
    saved = store.save_expense(make_expense())
    assert saved.state == PENDING

    updated = store.set_state(saved.id, CONFIRMED)
    assert updated.state == CONFIRMED
    assert store.get_expense(saved.id) == updated
    with pytest.raises(IllegalTransition):
        store.set_state(saved.id, PENDING)
    assert store.get_expense(saved.id).state == CONFIRMED

    # a concurrent writer: we hold a stale "pending" copy while someone else rejects it
    other = store.save_expense(make_expense())
    stale = store.get_expense(other.id)
    store.set_state(other.id, ExpenseState.rejected)
    monkeypatch.setattr(store, "get_expense", lambda _id: stale)
    with pytest.raises(StateConflict):
        store.set_state(other.id, CONFIRMED)
    monkeypatch.undo()
    assert store.get_expense(other.id).state == ExpenseState.rejected


# --- 3 --------------------------------------------------------------------------------------


def test_balances_are_per_currency_and_ignore_pending_rejected_and_deleted():
    store = Store(":memory:")
    assert store.balances(1) == {}

    # ILS: user 1 paid 90.00 for 1, 2, 3 (30.00 each); user 2 paid 50.00 for 1 and 2 (25.00 each)
    save_confirmed(store, total=9000, shares=shares_of({1: 9000}, {1: 3000, 2: 3000, 3: 3000}))
    save_confirmed(store, total=5000, shares=shares_of({2: 5000}, {1: 2500, 2: 2500}))
    # USD: user 3 paid 20.00, split with user 1
    save_confirmed(store, total=2000, currency=USD, shares=shares_of({3: 2000}, {1: 1000, 3: 1000}))
    expected = {
        ILS: {1: 3500, 2: -500, 3: -3000},
        USD: {1: -1000, 3: 1000},
    }
    assert store.balances(1) == expected
    assert all(sum(per_user.values()) == 0 for per_user in store.balances(1).values())

    big = shares_of({1: 99900}, {1: 49950, 2: 49950})
    store.save_expense(make_expense(total=99900, shares=big))  # still pending
    rejected = store.save_expense(make_expense(total=99900, shares=big))
    store.set_state(rejected.id, ExpenseState.rejected)
    expired = store.save_expense(make_expense(total=99900, shares=big))
    store.set_state(expired.id, ExpenseState.expired)
    deleted = save_confirmed(store, total=99900, shares=big)
    delete_by_everyone(store, deleted)
    assert store.get_expense(deleted.id).deleted is True
    save_confirmed(store, chat_id=2, total=99900, shares=big)  # another chat

    assert store.balances(1) == expected


# --- 4 --------------------------------------------------------------------------------------


def build_search_ledger() -> Store:
    store = Store(":memory:")
    a, b = {1: 4000, 2: 4000}, {1: 6000, 2: 6000}

    def add(description, total, subcategory, spent_on, paid, owed, currency=ILS):
        save_confirmed(
            store,
            description=description,
            total=total,
            subcategory=subcategory,
            currency=currency,
            spent_on=date.fromisoformat(spent_on),
            shares=shares_of(paid, owed),
        )

    add("Pizza night", 8000, Subcategory.restaurant, "2026-01-05", {1: 8000}, a)
    add("Sushi", 12000, Subcategory.restaurant, "2026-01-20", {2: 12000}, b)
    add("Electricity bill", 30000, Subcategory.electricity, "2026-02-03", {1: 30000}, {1: 15000, 2: 15000})
    add("Wolt pizza", 6000, Subcategory.delivery, "2026-02-10", {2: 6000}, {1: 3000, 2: 3000})
    add("Groceries", 5000, Subcategory.groceries, "2026-02-10", {1: 5000}, {1: 2500, 2: 2500}, USD)  # saved after Wolt
    add("Internet", 20000, Subcategory.internet, "2026-02-15", {1: 10000, 2: 10000}, {1: 10000, 2: 10000})

    # never visible: pending, soft-deleted, and another chat
    both = shares_of({1: 7000}, {1: 3500, 2: 3500})
    store.save_expense(make_expense(description="Pizza pending", total=7000, shares=both, spent_on=date(2026, 1, 6)))
    gone = save_confirmed(store, description="Pizza deleted", total=7000, shares=both, spent_on=date(2026, 1, 6))
    delete_by_everyone(store, gone)
    save_confirmed(store, chat_id=2, description="Pizza other chat", total=7000, shares=both, spent_on=date(2026, 1, 6))
    return store


SEARCHES = [
    ({}, ["Internet", "Groceries", "Wolt pizza", "Electricity bill", "Sushi", "Pizza night"]),  # newest first, id breaks ties
    ({"text": "PIZZA"}, ["Wolt pizza", "Pizza night"]),
    ({"text": "nothing like this"}, []),
    ({"category": Category.eating_out}, ["Wolt pizza", "Sushi", "Pizza night"]),  # restaurant + delivery
    ({"subcategory": Subcategory.restaurant}, ["Sushi", "Pizza night"]),
    ({"payer_id": 2}, ["Internet", "Wolt pizza", "Sushi"]),  # Internet was paid by both
    ({"payer_id": 1}, ["Internet", "Groceries", "Electricity bill", "Pizza night"]),
    ({"currency": USD}, ["Groceries"]),
    ({"month": "2026-01"}, ["Sushi", "Pizza night"]),
    ({"min_total": 12000}, ["Internet", "Electricity bill", "Sushi"]),  # inclusive
    ({"max_total": 6000}, ["Groceries", "Wolt pizza"]),  # inclusive
    ({"min_total": 6000, "max_total": 8000}, ["Wolt pizza", "Pizza night"]),
    ({"limit": 2}, ["Internet", "Groceries"]),
    ({"text": "pizza", "month": "2026-02"}, ["Wolt pizza"]),
    ({"text": "pizza", "payer_id": 1}, ["Pizza night"]),
    ({"category": Category.eating_out, "payer_id": 2, "min_total": 10000}, ["Sushi"]),
    ({"currency": ILS, "month": "2026-02", "max_total": 20000}, ["Internet", "Wolt pizza"]),
]

SUMMARIES = [
    (
        {"by": "category"},
        {ILS: {"eating_out": 26000, "utilities": 50000}, USD: {"groceries": 5000}},
    ),
    (
        {"by": "subcategory"},
        {ILS: {"restaurant": 20000, "electricity": 30000, "delivery": 6000, "internet": 20000}, USD: {"groceries": 5000}},
    ),
    (
        {"by": "month"},
        {ILS: {"2026-01": 20000, "2026-02": 56000}, USD: {"2026-02": 5000}},
    ),
    (
        {"by": "payer"},  # Internet counts 100.00 for each of its two payers
        {ILS: {"1": 48000, "2": 28000}, USD: {"1": 5000}},
    ),
    (
        {"by": "category", "month": "2026-02"},
        {ILS: {"eating_out": 6000, "utilities": 50000}, USD: {"groceries": 5000}},
    ),
    (
        {"by": "payer", "month": "2026-01"},
        {ILS: {"1": 8000, "2": 12000}},
    ),
]


def test_search_and_summary_read_only_confirmed_expenses():
    store = build_search_ledger()
    for filters, expected in SEARCHES:
        found = [e.description for e in store.search_expenses(1, **filters)]
        assert found == expected, filters
    for options, expected in SUMMARIES:
        assert store.spending_summary(1, **options) == expected, options
    assert store.search_expenses(3) == []
    assert store.spending_summary(3, by="category") == {}


# --- 5 --------------------------------------------------------------------------------------


def test_correction_or_delete_applies_only_after_all_approvals_exactly_once(tmp_path, monkeypatch):
    store = Store(":memory:")
    original = save_confirmed(store, description="dinner", message_id=1)
    balances_before = store.balances(1)
    assert balances_before == {ILS: {1: 6000, 2: -3000, 3: -3000}}
    proposed = make_expense(
        description="dinner (fixed)",
        total=12000,
        shares=shares_of({1: 12000}, {1: 4000, 2: 4000, 3: 4000}),
        message_id=999,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    request = store.create_change_request(make_change(original, ChangeKind.correction, proposed=proposed, message_id=500))
    assert request.id is not None
    assert request.state == PENDING
    assert request.approvals == {}  # nobody is approved automatically, not even the requester
    assert store.get_expense(original.id) == original
    assert store.balances(1) == balances_before

    request = store.respond(request.id, 1, True)  # the requester votes like everyone else
    assert request.state == PENDING
    assert request.approvals == {1: True}
    request = store.respond(request.id, 2, True)
    assert request.state == PENDING
    assert store.get_expense(original.id) == original
    with pytest.raises(NotRelevantApprover):
        store.respond(request.id, 99, True)
    request = store.respond(request.id, 2, False)  # a second tap from user 2 changes nothing
    assert request.state == PENDING
    assert request.approvals == {1: True, 2: True}
    assert store.get_expense(original.id) == original

    request = store.respond(request.id, 3, True)
    assert request.state == CONFIRMED
    applied = store.get_expense(original.id)
    assert (applied.total, applied.shares, applied.description) == (12000, proposed.shares, "dinner (fixed)")
    assert (applied.id, applied.chat_id, applied.message_id) == (original.id, 1, 1)
    assert applied.created_at == original.created_at
    assert applied.state == CONFIRMED
    assert store.balances(1) == {ILS: {1: 8000, 2: -4000, 3: -4000}}

    with pytest.raises(IllegalTransition):
        store.respond(request.id, 3, True)
    assert store.get_expense(original.id) == applied
    assert store.get_change_request(request.id).state == CONFIRMED

    # one no from a required approver cancels the request; the ledger stays as it was
    target = save_confirmed(store, description="lunch", message_id=2)
    request = store.create_change_request(make_change(target, ChangeKind.correction, proposed=proposed, message_id=501))
    request = store.respond(request.id, 3, False)
    assert request.state == ExpenseState.rejected
    assert store.get_expense(target.id) == target
    with pytest.raises(IllegalTransition):
        store.respond(request.id, 2, True)
    assert store.get_expense(target.id) == target

    # delete: soft-deleted after everyone approves, and no longer counted anywhere
    doomed = save_confirmed(store, description="doomed", message_id=3)
    assert len(store.search_expenses(1, text="doomed")) == 1
    delete_by_everyone(store, doomed)
    assert store.get_expense(doomed.id).deleted is True
    assert store.search_expenses(1, text="doomed") == []
    # only "dinner (fixed)" (+80.00, -40.00, -40.00) and "lunch" (+60.00, -30.00, -30.00) are left
    assert store.balances(1) == {ILS: {1: 14000, 2: -7000, 3: -7000}}

    # a group of one still has to press approve: pending and untouched until the requester votes
    solo_shares = shares_of({1: 5000}, {1: 5000})
    solo_edit = save_confirmed(store, chat_id=5, total=5000, shares=solo_shares, message_id=1)
    solo_proposed = make_expense(chat_id=5, total=7000, description="solo fixed", shares=shares_of({1: 7000}, {1: 7000}))
    request = store.create_change_request(make_change(solo_edit, ChangeKind.correction, proposed=solo_proposed))
    assert request.state == PENDING
    assert request.approvals == {}
    assert store.get_expense(solo_edit.id) == solo_edit
    request = store.respond(request.id, 1, True)
    assert request.state == CONFIRMED
    assert store.get_expense(solo_edit.id).total == 7000
    solo_delete = save_confirmed(store, chat_id=5, total=5000, shares=solo_shares, message_id=2)
    request = store.create_change_request(make_change(solo_delete))
    assert request.state == PENDING
    assert store.get_expense(solo_delete.id).deleted is False
    request = store.respond(request.id, 1, True)
    assert request.state == CONFIRMED
    assert store.get_expense(solo_delete.id).deleted is True

    # only confirmed, live expenses of the same chat can be changed; one request per message
    pending = store.save_expense(make_expense())
    live = save_confirmed(store)
    with pytest.raises(ValueError):
        store.create_change_request(make_change(pending))
    with pytest.raises(ValueError):
        store.create_change_request(make_change(doomed))
    with pytest.raises(ValueError):
        store.create_change_request(make_change(live).model_copy(update={"chat_id": 6}))  # asked from another chat
    store.create_change_request(make_change(live, message_id=777))
    with pytest.raises(DuplicateMessage):
        store.create_change_request(make_change(live, message_id=777))

    # lost update (a real bug): store B decides from a request it read BEFORE store A's vote
    path = str(tmp_path / "shared.db")
    a, b = Store(path), Store(path)
    shared = save_confirmed(a, description="shared")
    request = a.create_change_request(make_change(shared))
    a.respond(request.id, 1, True)
    stale = b.get_change_request(request.id)
    a.respond(request.id, 2, True)
    monkeypatch.setattr(b, "get_change_request", lambda _id: stale)
    with pytest.raises(StateConflict):
        b.respond(request.id, 3, True)
    monkeypatch.undo()
    stored = a.get_change_request(request.id)
    assert stored.approvals == {1: True, 2: True}  # A's vote survived, B's vote was not stored
    assert stored.state == PENDING
    assert a.get_expense(shared.id).deleted is False
    request = b.respond(request.id, 3, True)  # after re-reading, B can vote
    assert request.approvals == {1: True, 2: True, 3: True}
    assert request.state == CONFIRMED
    assert a.get_expense(shared.id).deleted is True


# --- 6 --------------------------------------------------------------------------------------


def test_stale_pending_items_expire(monkeypatch):
    monkeypatch.delenv("PENDING_EXPIRY_HOURS", raising=False)  # the default is 1 hour
    store = Store(":memory:")
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    old, fresh = now - timedelta(hours=2), now - timedelta(minutes=30)

    old_pending = store.save_expense(make_expense(created_at=old))
    fresh_pending = store.save_expense(make_expense(created_at=fresh))
    old_confirmed = save_confirmed(store, created_at=old)
    target_a = save_confirmed(store, created_at=fresh)
    target_b = save_confirmed(store, created_at=fresh)
    old_request = store.create_change_request(make_change(target_a, created_at=old))
    fresh_request = store.create_change_request(make_change(target_b, created_at=fresh))
    balances = store.balances(1)

    expired = store.expire_stale(now)
    assert len(expired) == 2
    assert set(expired) == {
        Expired(kind="expense", id=old_pending.id, chat_id=1, message_id=old_pending.message_id),
        Expired(kind="change_request", id=old_request.id, chat_id=1, message_id=old_request.message_id),
    }
    assert store.get_expense(old_pending.id).state == ExpenseState.expired
    assert store.get_change_request(old_request.id).state == ExpenseState.expired
    assert store.get_expense(fresh_pending.id).state == PENDING
    assert store.get_change_request(fresh_request.id).state == PENDING
    assert store.get_expense(old_confirmed.id).state == CONFIRMED
    assert store.get_expense(target_a.id) == target_a  # an expired request changed nothing
    assert store.balances(1) == balances  # nothing was written to the ledger
    assert store.expire_stale(now) == []

    with pytest.raises(IllegalTransition):
        store.set_state(old_pending.id, CONFIRMED)
    with pytest.raises(IllegalTransition):
        store.respond(old_request.id, 2, True)
    assert store.get_expense(old_pending.id).state == ExpenseState.expired
    assert store.get_expense(target_a.id).deleted is False

    # an explicit expiry wins over the default: the 30-minute-old items now count as stale
    later = store.expire_stale(now, expiry=timedelta(minutes=10))
    assert {(e.kind, e.id) for e in later} == {("expense", fresh_pending.id), ("change_request", fresh_request.id)}
    assert store.get_expense(old_confirmed.id).state == CONFIRMED
    assert store.balances(1) == balances


# --- 7 --------------------------------------------------------------------------------------


def test_only_the_sender_answers_a_new_expense_and_only_once(tmp_path, monkeypatch):
    store = Store(":memory:")  # make_expense: user 1 is the author

    approved = store.save_expense(make_expense())
    assert store.respond_expense(approved.id, 1, True).state == CONFIRMED
    assert store.get_expense(approved.id).state == CONFIRMED

    rejected = store.save_expense(make_expense())
    assert store.respond_expense(rejected.id, 1, False).state == ExpenseState.rejected
    assert store.get_expense(rejected.id).state == ExpenseState.rejected

    # someone else may not answer for the sender; nothing changes, the sender can still answer
    other = store.save_expense(make_expense())
    with pytest.raises(NotRelevantApprover):
        store.respond_expense(other.id, 2, True)
    with pytest.raises(NotRelevantApprover):
        store.respond_expense(other.id, 2, False)
    assert store.get_expense(other.id).state == PENDING
    assert store.respond_expense(other.id, 1, True).state == CONFIRMED

    # only the first answer counts, whatever the second one says
    for second in (True, False):
        with pytest.raises(IllegalTransition):
            store.respond_expense(other.id, 1, second)
        with pytest.raises(IllegalTransition):
            store.respond_expense(rejected.id, 1, second)
    assert store.get_expense(other.id).state == CONFIRMED
    assert store.get_expense(rejected.id).state == ExpenseState.rejected

    with pytest.raises(KeyError):
        store.respond_expense(99999, 1, True)

    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    late = store.save_expense(make_expense(created_at=now - timedelta(hours=2)))
    store.expire_stale(now, expiry=timedelta(hours=1))
    with pytest.raises(IllegalTransition):
        store.respond_expense(late.id, 1, True)
    assert store.get_expense(late.id).state == ExpenseState.expired

    # only the two confirmed expenses count (30.00 each owed by users 1, 2, 3; user 1 paid 90.00)
    assert store.balances(1) == {ILS: {1: 12000, 2: -6000, 3: -6000}}

    # stale read: store B holds a "pending" copy while store A already rejected the expense
    path = str(tmp_path / "shared.db")
    a, b = Store(path), Store(path)
    contested = a.save_expense(make_expense())
    stale = b.get_expense(contested.id)
    a.respond_expense(contested.id, 1, False)
    monkeypatch.setattr(b, "get_expense", lambda _id: stale)
    with pytest.raises(StateConflict):
        b.respond_expense(contested.id, 1, True)
    monkeypatch.undo()
    assert a.get_expense(contested.id).state == ExpenseState.rejected
    assert a.balances(1) == {}


# --- 8 --------------------------------------------------------------------------------------

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def test_an_answer_after_expiry_is_refused_even_before_the_sweeper_runs(monkeypatch):
    monkeypatch.delenv("PENDING_EXPIRY_HOURS", raising=False)  # the default is 1 hour
    store = Store(":memory:")
    target = save_confirmed(store, description="target")

    def new_expense():
        return store.save_expense(make_expense(created_at=T0))

    def new_request():
        return store.create_change_request(make_change(target, created_at=T0))

    # a new expense
    assert store.respond_expense(new_expense().id, 1, True, now=T0 + timedelta(minutes=59)).state == CONFIRMED
    assert store.respond_expense(new_expense().id, 1, False, now=None).state == ExpenseState.rejected  # no check
    for late in (timedelta(hours=1), timedelta(hours=2)):  # exactly the expiry counts as expired
        expense = new_expense()
        balances = store.balances(1)
        with pytest.raises(IllegalTransition):
            store.respond_expense(expense.id, 1, True, now=T0 + late)
        assert store.get_expense(expense.id).state == ExpenseState.expired
        assert store.balances(1) == balances
        with pytest.raises(IllegalTransition):  # a later answer, with or without a clock, never confirms
            store.respond_expense(expense.id, 1, True, now=None)
        assert store.get_expense(expense.id).state == ExpenseState.expired

    # a change request
    for late in (timedelta(hours=1), timedelta(hours=2)):
        request = new_request()
        with pytest.raises(IllegalTransition):
            store.respond(request.id, 1, True, now=T0 + late)
        stored = store.get_change_request(request.id)
        assert stored.state == ExpenseState.expired
        assert stored.approvals == {}
        with pytest.raises(IllegalTransition):
            store.respond(request.id, 2, True, now=None)
        assert store.get_change_request(request.id).state == ExpenseState.expired
        assert store.get_expense(target.id) == target
    unchecked = new_request()
    assert store.respond(unchecked.id, 1, True, now=None).approvals == {1: True}  # now=None skips the check
    request = new_request()
    for user in (1, 2, 3):
        request = store.respond(request.id, user, True, now=T0 + timedelta(minutes=59))
    assert request.state == CONFIRMED
    assert store.get_expense(target.id).deleted is True


# --- 9 --------------------------------------------------------------------------------------


def test_a_change_is_refused_when_its_target_was_changed_meanwhile():
    store = Store(":memory:")
    proposed = make_expense(
        description="dinner (fixed)", total=12000, shares=shares_of({1: 12000}, {1: 4000, 2: 4000, 3: 4000})
    )

    def approve(request, users):
        for user in users:
            request = store.respond(request.id, user, True)
        return request

    # the delete wins: the correction's last approval must not resurrect or rewrite the expense
    expense = save_confirmed(store, description="dinner")
    delete = store.create_change_request(make_change(expense))
    correction = store.create_change_request(make_change(expense, ChangeKind.correction, proposed=proposed))
    assert delete.state == correction.state == PENDING
    approve(correction, [1, 2])
    approve(delete, [1, 2, 3])
    deleted = store.get_expense(expense.id)
    assert deleted.deleted is True and deleted.description == "dinner"
    with pytest.raises(StateConflict):
        store.respond(correction.id, 3, True)
    assert store.get_expense(expense.id) == deleted
    stored = store.get_change_request(correction.id)
    assert stored.state == PENDING
    assert stored.approvals == {1: True, 2: True}  # the last vote was not stored

    # the correction wins: the delete's last approval still applies, the target is still live
    other = save_confirmed(store, description="lunch")
    correction = store.create_change_request(make_change(other, ChangeKind.correction, proposed=proposed))
    delete = store.create_change_request(make_change(other))
    approve(delete, [1, 2])
    assert approve(correction, [1, 2, 3]).state == CONFIRMED
    assert store.get_expense(other.id).description == "dinner (fixed)"
    assert store.get_expense(other.id).deleted is False
    assert approve(delete, [3]).state == CONFIRMED
    assert store.get_expense(other.id).deleted is True


# --- 10 -------------------------------------------------------------------------------------


class FailingCommit:
    """Delegates everything to the real connection, but the first COMMIT raises."""

    def __init__(self, real):
        self._real = real
        self.armed = True

    def execute(self, sql, *args, **kwargs):
        if self.armed and sql.strip().upper() == "COMMIT":
            self.armed = False
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_failing_commit_rolls_back_and_leaves_the_store_usable():
    store = Store(":memory:")
    store._db = FailingCommit(store._db)
    with pytest.raises(sqlite3.OperationalError):
        store.save_expense(make_expense(message_id=1))
    assert store.get_expense_by_message(1, 1) is None  # not stored
    assert not store.is_processed(1, 1)

    # the very next calls work (no "cannot start a transaction within a transaction")
    saved = store.save_expense(make_expense(message_id=1))
    assert store.get_expense_by_message(1, 1) == saved
    store.save_expense(make_expense(message_id=2))

    store._db.armed = True
    with pytest.raises(sqlite3.OperationalError):
        store.respond_expense(saved.id, 1, True)
    assert store.get_expense(saved.id).state == PENDING
    assert store.respond_expense(saved.id, 1, True).state == CONFIRMED


# --- 11 -------------------------------------------------------------------------------------


def test_revise_pending_expense_replaces_content_and_resets_expiry():
    store = Store(":memory:")
    old_created = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
    pending = store.save_expense(make_expense(created_at=old_created))
    later = old_created + timedelta(minutes=45)

    replacement = make_expense(
        message_id=pending.message_id,  # ignored: the store keeps the original identity fields
        total=15000,
        shares=shares_of({1: 15000}, {1: 5000, 2: 5000, 3: 5000}),
    ).model_copy(update={"version": pending.version})

    updated = store.revise_pending_expense(pending.id, replacement, now=later)

    assert updated.total == 15000
    assert _shares_of(updated) == {1: (15000, 5000), 2: (0, 5000), 3: (0, 5000)}
    assert updated.version == pending.version + 1
    assert updated.created_at == later  # expiry is reset to NOW, not counted from the original time
    assert (updated.id, updated.chat_id, updated.message_id, updated.author_id) == (
        pending.id, pending.chat_id, pending.message_id, pending.author_id,
    )
    assert updated.state == PENDING
    stored = store.get_expense(pending.id)
    assert stored == updated

    # now=None resets created_at to the real wall clock, not the caller's `later`
    fresh = store.save_expense(make_expense(created_at=old_created, message_id=pending.message_id + 1))
    again = replacement.model_copy(update={"version": fresh.version})
    before_call = datetime.now(timezone.utc)
    revised_no_now = store.revise_pending_expense(fresh.id, again)
    assert revised_no_now.created_at >= before_call
    assert (revised_no_now.created_at - before_call).total_seconds() < 5


def _shares_of(expense) -> dict[int, tuple[int, int]]:
    return {s.user_id: (s.paid, s.owed) for s in expense.shares}


def test_revise_pending_expense_is_compare_and_swap_on_version():
    store = Store(":memory:")
    pending = store.save_expense(make_expense(total=15000, shares=shares_of({1: 15000}, {1: 5000, 2: 5000, 3: 5000})))
    stale = store.get_expense(pending.id)  # version 0, read before anyone else touches it

    # someone else revises the SAME expense first: the real stored version becomes 1
    real_writer_replacement = make_expense(
        total=30000, shares=shares_of({1: 30000}, {1: 10000, 2: 10000, 3: 10000})
    ).model_copy(update={"version": stale.version})
    winner = store.revise_pending_expense(pending.id, real_writer_replacement, now=T0)
    assert winner.total == 30000 and winner.version == 1

    # a naive implementation that compares the freshly-read `current.version` to ITSELF (instead
    # of to `replacement.version`, the caller's own stale expectation) would let this through and
    # silently overwrite the real writer's change. The real store must reject it instead.
    our_stale_replacement = make_expense(
        total=9000, shares=shares_of({1: 9000}, {1: 3000, 2: 3000, 3: 3000})
    ).model_copy(update={"version": stale.version})  # version 0: stale
    with pytest.raises(StateConflict):
        store.revise_pending_expense(pending.id, our_stale_replacement, now=T0)
    after = store.get_expense(pending.id)
    assert after.total == 30000  # the real writer's change was NOT overwritten
    assert after.version == 1

    # the reverse: a stale `respond_expense` call (read before a revision) must also conflict
    store2 = Store(":memory:")
    pending2 = store2.save_expense(make_expense())
    stale2 = store2.get_expense(pending2.id)  # version 0
    revised2_replacement = make_expense(total=20000, shares=shares_of({1: 20000}, {1: 20000})).model_copy(
        update={"version": stale2.version}
    )
    store2.revise_pending_expense(pending2.id, revised2_replacement, now=T0)  # bumps to version 1
    assert store2.get_expense(pending2.id).total == 20000

    def stale_read(_id, _stale=stale2):
        return _stale

    original_get_expense = store2.get_expense
    store2.get_expense = stale_read
    try:
        with pytest.raises(StateConflict):
            store2.respond_expense(pending2.id, 1, True, now=T0)
    finally:
        store2.get_expense = original_get_expense
    final2 = store2.get_expense(pending2.id)
    assert final2.total == 20000  # the revision survived; the stale confirmation did not apply
    assert final2.state == PENDING


def test_revise_pending_expense_refuses_a_non_pending_expense():
    store = Store(":memory:")
    replacement = make_expense(total=9000, shares=shares_of({1: 9000}, {1: 3000, 2: 3000, 3: 3000}))

    confirmed = save_confirmed(store)
    with pytest.raises(IllegalTransition):
        store.revise_pending_expense(confirmed.id, replacement.model_copy(update={"version": confirmed.version}))
    assert store.get_expense(confirmed.id) == confirmed

    rejected = store.save_expense(make_expense())
    rejected = store.respond_expense(rejected.id, rejected.author_id, False)
    with pytest.raises(IllegalTransition):
        store.revise_pending_expense(rejected.id, replacement.model_copy(update={"version": rejected.version}))
    assert store.get_expense(rejected.id) == rejected

    old = store.save_expense(make_expense(created_at=T0))
    store.expire_stale(T0 + timedelta(hours=2))
    expired = store.get_expense(old.id)
    assert expired.state == ExpenseState.expired
    with pytest.raises(IllegalTransition):
        store.revise_pending_expense(old.id, replacement.model_copy(update={"version": expired.version}))
    assert store.get_expense(old.id) == expired


def test_revise_pending_expense_after_expiry_is_refused_and_the_record_expires():
    store = Store(":memory:")

    for late in (timedelta(hours=1), timedelta(hours=2)):  # exactly the expiry counts as expired
        target = store.save_expense(make_expense(created_at=T0))
        replacement = make_expense(total=9000, shares=shares_of({1: 9000}, {1: 3000, 2: 3000, 3: 3000})).model_copy(
            update={"version": target.version}
        )
        with pytest.raises(IllegalTransition):
            store.revise_pending_expense(target.id, replacement, now=T0 + late)
        stored = store.get_expense(target.id)
        assert stored.state == ExpenseState.expired  # left expired, not reverted
        assert stored.total == target.total  # never revised
        with pytest.raises(IllegalTransition):  # a later attempt, with or without a clock, never revises it
            store.revise_pending_expense(target.id, replacement, now=None)
        assert store.get_expense(target.id).state == ExpenseState.expired
