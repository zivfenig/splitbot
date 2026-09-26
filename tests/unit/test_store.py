import itertools
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
from splitbot.store import DuplicateMessage, NotRelevantApprover, StateConflict, Store

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
    for user in request.required_approvers[1:]:
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
    with pytest.raises(KeyError):
        store.get_expense(first.id + 1)  # the duplicate left no row behind

    # a message marked processed earlier (no record yet) can still get its one expense
    store.mark_processed(1, 30)
    store.save_expense(make_expense(message_id=30))
    with pytest.raises(DuplicateMessage):
        store.save_expense(make_expense(message_id=30))

    path = str(tmp_path / "ledger.db")
    original = Store(path)
    saved = original.save_expense(make_expense(message_id=40))
    original.mark_processed(1, 41)
    reopened = Store(path)
    assert reopened.get_expense(saved.id) == saved
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


def test_correction_or_delete_applies_only_after_all_approvals_exactly_once():
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
    assert request.approvals == {1: True}
    assert store.get_expense(original.id) == original

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

    # a group of one is confirmed and applied at once
    solo_shares = shares_of({1: 5000}, {1: 5000})
    solo_edit = save_confirmed(store, chat_id=5, total=5000, shares=solo_shares, message_id=1)
    solo_proposed = make_expense(chat_id=5, total=7000, description="solo fixed", shares=shares_of({1: 7000}, {1: 7000}))
    request = store.create_change_request(make_change(solo_edit, ChangeKind.correction, proposed=solo_proposed))
    assert request.state == CONFIRMED
    assert store.get_expense(solo_edit.id).total == 7000
    solo_delete = save_confirmed(store, chat_id=5, total=5000, shares=solo_shares, message_id=2)
    request = store.create_change_request(make_change(solo_delete))
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


# --- 6 --------------------------------------------------------------------------------------


def test_stale_pending_items_expire():
    store = Store(":memory:")
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    old, fresh = now - timedelta(hours=49), now - timedelta(hours=47)

    old_pending = store.save_expense(make_expense(created_at=old))
    fresh_pending = store.save_expense(make_expense(created_at=fresh))
    old_confirmed = save_confirmed(store, created_at=old)
    target_a = save_confirmed(store, created_at=fresh)
    target_b = save_confirmed(store, created_at=fresh)
    old_request = store.create_change_request(make_change(target_a, created_at=old))
    fresh_request = store.create_change_request(make_change(target_b, created_at=fresh))

    assert store.expire_stale(now) == 2
    assert store.get_expense(old_pending.id).state == ExpenseState.expired
    assert store.get_change_request(old_request.id).state == ExpenseState.expired
    assert store.get_expense(fresh_pending.id).state == PENDING
    assert store.get_change_request(fresh_request.id).state == PENDING
    assert store.get_expense(old_confirmed.id).state == CONFIRMED
    assert store.get_expense(target_a.id) == target_a  # an expired request changed nothing
    assert store.expire_stale(now) == 0

    with pytest.raises(IllegalTransition):
        store.set_state(old_pending.id, CONFIRMED)
    with pytest.raises(IllegalTransition):
        store.respond(old_request.id, 2, True)
    assert store.get_expense(old_pending.id).state == ExpenseState.expired
    assert store.get_expense(target_a.id).deleted is False
