"""Read tools: balances, search, summaries. Everything comes from the ledger, per currency, one chat."""

import json
from datetime import date

import pytest

from splitbot.models import (
    ChangeKind,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    Member,
    Share,
    Subcategory,
)
from splitbot.store import Store
from splitbot.tools.read_tools import TOOL_SPECS, ReadTools, ToolError, Transfer, settlement_transfers

CHAT, OTHER_CHAT = 100, 200
ANN, BEN, CY = 1, 2, 3
MEMBERS = [Member(id=ANN, name="Ann"), Member(id=BEN, name="Ben"), Member(id=CY, name="Cy")]

_next_message = iter(range(1, 1000))


def _seed(store, description, total, currency, subcategory, paid, owed, spent_on, *, chat_id=CHAT, state=None):
    """Save one expense. `paid`/`owed` are {user_id: minor units}. No `state` = save it pending,
    then confirm it (like the real flow); otherwise save it in exactly that state."""
    shares = [Share(user_id=u, paid=paid.get(u, 0), owed=owed.get(u, 0)) for u in sorted(paid.keys() | owed.keys())]
    expense = Expense(
        chat_id=chat_id, message_id=next(_next_message), author_id=ANN, description=description,
        total=total, currency=currency, subcategory=subcategory, shares=shares,
        prompt_version="test", spent_on=spent_on,
        **({"state": state} if state else {}),
    )
    saved = store.save_expense(expense)
    if state is None:
        store.set_state(saved.id, ExpenseState.confirmed)
    return saved


def _delete(store, expense):
    """Soft-delete a confirmed expense: Ann asks, then every required approver votes yes."""
    request = store.create_change_request(ChangeRequest(
        chat_id=expense.chat_id, message_id=next(_next_message), expense_id=expense.id,
        kind=ChangeKind.delete, requested_by=ANN, required_approvers=[ANN, BEN],
    ))
    for user_id in request.required_approvers:
        store.respond(request.id, user_id, True)


def _balances_ledger():
    """Chat 100. ILS: Ann paid a 116.70 dinner for three (38.90 each). USD: Cy paid a 180 hotel
    for three (60 each). Plus things that must NOT count: pending, deleted, another chat."""
    store = Store(":memory:")
    _seed(store, "Sushi dinner", 11670, Currency.ILS, Subcategory.restaurant,
          {ANN: 11670}, {ANN: 3890, BEN: 3890, CY: 3890}, date(2026, 8, 10))
    _seed(store, "Hotel", 18000, Currency.USD, Subcategory.other,
          {CY: 18000}, {ANN: 6000, BEN: 6000, CY: 6000}, date(2026, 9, 1))
    _seed(store, "Sushi pending", 4800, Currency.ILS, Subcategory.restaurant,
          {CY: 4800}, {ANN: 1600, BEN: 1600, CY: 1600}, date(2026, 9, 2), state=ExpenseState.pending_confirmation)
    gone = _seed(store, "Sushi deleted", 3000, Currency.ILS, Subcategory.restaurant,
                 {BEN: 3000}, {ANN: 1000, BEN: 1000, CY: 1000}, date(2026, 9, 3))
    _delete(store, gone)
    _seed(store, "Sushi elsewhere", 9000, Currency.ILS, Subcategory.restaurant,
          {BEN: 9000}, {ANN: 3000, BEN: 3000, CY: 3000}, date(2026, 9, 4), chat_id=OTHER_CHAT)
    return store


def _search_ledger():
    """Chat 100, six live expenses (ILS and USD) + one of each kind that must stay invisible."""
    store = Store(":memory:")
    S = Subcategory
    _seed(store, "Sushi dinner", 11670, Currency.ILS, S.restaurant,
          {ANN: 11670}, {ANN: 3890, BEN: 3890, CY: 3890}, date(2026, 8, 10))
    _seed(store, "Sushi in NYC", 4000, Currency.USD, S.restaurant,
          {ANN: 4000}, {ANN: 2000, BEN: 2000}, date(2026, 8, 25))
    _seed(store, "Hotel", 12000, Currency.USD, S.other,
          {CY: 12000}, {ANN: 4000, BEN: 4000, CY: 4000}, date(2026, 9, 1))
    _seed(store, "Pizza delivery", 8000, Currency.ILS, S.delivery,
          {BEN: 8000}, {ANN: 4000, BEN: 4000}, date(2026, 9, 5))
    _seed(store, "Weekly groceries", 6000, Currency.ILS, S.groceries,
          {ANN: 3000, CY: 3000}, {ANN: 2000, BEN: 2000, CY: 2000}, date(2026, 9, 12))
    _seed(store, "Electricity", 30000, Currency.ILS, S.electricity,
          {BEN: 30000}, {ANN: 10000, BEN: 10000, CY: 10000}, date(2026, 9, 20))
    # invisible: huge totals so any leak also shows up in min_total / summaries
    big = {ANN: 99900}, {ANN: 33300, BEN: 33300, CY: 33300}
    for state in (ExpenseState.pending_confirmation, ExpenseState.rejected, ExpenseState.expired):
        _seed(store, f"Sushi {state.value}", 99900, Currency.ILS, S.restaurant, *big, date(2026, 9, 10), state=state)
    gone = _seed(store, "Sushi deleted", 99900, Currency.ILS, S.restaurant, *big, date(2026, 9, 10))
    _delete(store, gone)
    _seed(store, "Sushi elsewhere", 5000, Currency.ILS, S.restaurant,
          {BEN: 5000}, {ANN: 2500, BEN: 2500}, date(2026, 9, 10), chat_id=OTHER_CHAT)
    return store


def _apply(balances, transfers):
    result = dict(balances)
    for t in transfers:
        result[t.from_user] += t.amount  # the one who owed pays
        result[t.to_user] -= t.amount  # the one who was owed receives
    return result


def _names(result):
    return [e["description"] for e in result["expenses"]]


def _flat(totals):
    """{currency: {key: {"amount", "amount_minor"}}} -> {currency: {key: (amount, amount_minor)}}"""
    return {c: {k: (v["amount"], v["amount_minor"]) for k, v in per.items()} for c, per in totals.items()}


def test_balances_settle_with_the_fewest_transfers_per_currency():
    # (a) settlement_transfers on hand-computed balances (positive = is owed)
    hand_cases = [
        # one debtor, one creditor
        ({1: 500, 2: -500}, [Transfer(2, 1, 500)]),
        # two debtors, one creditor: the larger debt pays first
        ({1: 1000, 2: -300, 3: -700}, [Transfer(3, 1, 700), Transfer(2, 1, 300)]),
        # chain 1->2 (10), 2->3 (20), 3->4 (10) nets to 1:-10 2:-10 3:+10 4:+10: 2 transfers, not 3.
        # All ties, so the lower user id goes first on each side.
        ({1: -10, 2: -10, 3: 10, 4: 10}, [Transfer(1, 3, 10), Transfer(2, 4, 10)]),
        # users at 0 take no part
        ({1: 0, 2: 400, 3: -400, 4: 0}, [Transfer(3, 2, 400)]),
        # nothing to settle
        ({}, []),
        ({1: 0, 2: 0}, []),
    ]
    for balances, expected in hand_cases:
        assert settlement_transfers(balances) == expected, balances
    # same answer whatever the dict order
    assert settlement_transfers({4: 10, 3: 10, 2: -10, 1: -10}) == [Transfer(1, 3, 10), Transfer(2, 4, 10)]

    # applying the transfers brings everyone to 0; amounts > 0; never more than (people - 1) transfers
    for balances in [
        {1: 500, 2: -500},
        {1: 700, 2: -200, 3: 150, 4: -450, 5: -200},
        {1: -3890, 2: 7780, 3: -3890},
        {1: 1, 2: 1, 3: 1, 4: -3},
    ]:
        transfers = settlement_transfers(balances)
        assert all(t.amount > 0 for t in transfers), balances
        assert all(v == 0 for v in _apply(balances, transfers).values()), balances
        assert len(transfers) <= len(balances) - 1, balances

    # balances that do not sum to 0 are a bug upstream: refuse
    with pytest.raises(ValueError):
        settlement_transfers({1: 100, 2: -50})

    # (b) get_balances on a seeded ledger: two currencies, never mixed
    result = ReadTools(_balances_ledger(), CHAT, MEMBERS).get_balances()
    json.dumps(result)  # JSON-serializable
    assert result == {
        "balances": {
            "ILS": {
                "members": [
                    {"user_id": 1, "name": "Ann", "net": "77.80", "net_minor": 7780},
                    {"user_id": 2, "name": "Ben", "net": "-38.90", "net_minor": -3890},
                    {"user_id": 3, "name": "Cy", "net": "-38.90", "net_minor": -3890},
                ],
                "transfers": [
                    {"from": 2, "from_name": "Ben", "to": 1, "to_name": "Ann", "amount": "38.90", "amount_minor": 3890},
                    {"from": 3, "from_name": "Cy", "to": 1, "to_name": "Ann", "amount": "38.90", "amount_minor": 3890},
                ],
            },
            "USD": {
                "members": [
                    {"user_id": 1, "name": "Ann", "net": "-60", "net_minor": -6000},
                    {"user_id": 2, "name": "Ben", "net": "-60", "net_minor": -6000},
                    {"user_id": 3, "name": "Cy", "net": "120", "net_minor": 12000},
                ],
                "transfers": [
                    {"from": 1, "from_name": "Ann", "to": 3, "to_name": "Cy", "amount": "60", "amount_minor": 6000},
                    {"from": 2, "from_name": "Ben", "to": 3, "to_name": "Cy", "amount": "60", "amount_minor": 6000},
                ],
            },
        }
    }
    # (the pending, the deleted and the other chat's expenses would each have changed a number above)

    # a chat with no expenses of its own sees nothing, even on a shared database
    assert ReadTools(_balances_ledger(), 999, MEMBERS).get_balances() == {"balances": {}}
    assert ReadTools(Store(":memory:"), CHAT, MEMBERS).get_balances() == {"balances": {}}


def test_read_tools_answer_from_the_ledger_only_and_never_show_another_chat():
    store = _search_ledger()
    tools = ReadTools(store, CHAT, MEMBERS)

    # --- search_expenses: newest first, live expenses of this chat only
    everything = tools.search_expenses()
    json.dumps(everything)
    assert _names(everything) == [
        "Electricity", "Weekly groceries", "Pizza delivery", "Hotel", "Sushi in NYC", "Sushi dinner",
    ]
    assert everything["count"] == 6

    # documented keys and display strings, checked on one full entry
    sushi = tools.search_expenses(text="dinner")["expenses"][0]
    assert sushi == {
        "id": sushi["id"], "description": "Sushi dinner", "total": "116.70", "total_minor": 11670,
        "currency": "ILS", "category": "eating_out", "subcategory": "restaurant", "spent_on": "2026-08-10",
        "payers": ["Ann"],
        "shares": [
            {"user_id": 1, "name": "Ann", "paid": "116.70", "owed": "38.90"},
            {"user_id": 2, "name": "Ben", "paid": "0", "owed": "38.90"},
            {"user_id": 3, "name": "Cy", "paid": "0", "owed": "38.90"},
        ],
    }
    assert tools.search_expenses(text="hotel")["expenses"][0]["total"] == "120"
    groceries = tools.search_expenses(text="groceries")["expenses"][0]
    assert sorted(groceries["payers"]) == ["Ann", "Cy"]  # several payers -> all names

    # each filter (enum values as strings), with the expected newest-first result
    filters = [
        ({"text": "PIZZA"}, ["Pizza delivery"]),
        ({"text": "sushi"}, ["Sushi in NYC", "Sushi dinner"]),
        ({"category": "eating_out"}, ["Pizza delivery", "Sushi in NYC", "Sushi dinner"]),
        ({"subcategory": "delivery"}, ["Pizza delivery"]),
        ({"payer_id": 3}, ["Weekly groceries", "Hotel"]),
        ({"currency": "USD"}, ["Hotel", "Sushi in NYC"]),
        ({"month": "2026-09"}, ["Electricity", "Weekly groceries", "Pizza delivery", "Hotel"]),
        ({"min_total": "80"}, ["Electricity", "Pizza delivery", "Hotel", "Sushi dinner"]),  # 8000 itself counts
        ({"max_total": "60"}, ["Weekly groceries", "Sushi in NYC"]),  # 6000 itself counts
        ({"min_total": "60", "max_total": "80"}, ["Weekly groceries", "Pizza delivery"]),
        ({"category": "eating_out", "currency": "ILS"}, ["Pizza delivery", "Sushi dinner"]),  # filters are ANDed
        ({"limit": 2}, ["Electricity", "Weekly groceries"]),
        ({"limit": 50}, None),  # the top of the range is allowed
        ({"limit": 1}, ["Electricity"]),
        ({"text": "no such thing"}, []),
    ]
    for kwargs, expected in filters:
        found = tools.search_expenses(**kwargs)
        if expected is not None:
            assert _names(found) == expected, kwargs
        if "limit" not in kwargs:
            assert found["count"] == len(found["expenses"]), kwargs

    # invisible: pending / rejected / expired / deleted (huge totals) and another chat's expense
    assert tools.search_expenses(min_total="500")["count"] == 0
    assert tools.search_expenses(text="elsewhere")["count"] == 0
    elsewhere = ReadTools(store, OTHER_CHAT, MEMBERS).search_expenses()
    assert _names(elsewhere) == ["Sushi elsewhere"]

    # bad arguments are a ToolError, never a silent empty answer
    for kwargs in [
        {"category": "food"},
        {"subcategory": "sushi"},
        {"currency": "GBP"},
        {"month": "2026-13"},
        {"month": "9/2026"},
        {"limit": 0},
        {"limit": 51},
    ]:
        with pytest.raises(ToolError):
            tools.search_expenses(**kwargs)

    # --- spending_summary: hand-computed totals per currency
    ils_by_category = {"eating_out": ("196.70", 19670), "groceries": ("60", 6000), "utilities": ("300", 30000)}
    usd_by_category = {"other": ("120", 12000), "eating_out": ("40", 4000)}
    summaries = [
        ("category", None, {"ILS": ils_by_category, "USD": usd_by_category}),
        ("subcategory", None, {
            "ILS": {"restaurant": ("116.70", 11670), "delivery": ("80", 8000),
                    "groceries": ("60", 6000), "electricity": ("300", 30000)},
            "USD": {"other": ("120", 12000), "restaurant": ("40", 4000)},
        }),
        ("month", None, {
            "ILS": {"2026-08": ("116.70", 11670), "2026-09": ("440", 44000)},
            "USD": {"2026-08": ("40", 4000), "2026-09": ("120", 12000)},
        }),
        # payer: keys are NAMES, and each payer counts what they paid (groceries: Ann 30 + Cy 30)
        ("payer", None, {
            "ILS": {"Ann": ("146.70", 14670), "Ben": ("380", 38000), "Cy": ("30", 3000)},
            "USD": {"Cy": ("120", 12000), "Ann": ("40", 4000)},
        }),
        # month filter
        ("category", "2026-09", {
            "ILS": {"eating_out": ("80", 8000), "groceries": ("60", 6000), "utilities": ("300", 30000)},
            "USD": {"other": ("120", 12000)},
        }),
        ("category", "2026-08", {"ILS": {"eating_out": ("116.70", 11670)}, "USD": {"eating_out": ("40", 4000)}}),
        ("category", "2026-07", {}),  # nothing that month: no currencies at all
    ]
    for by, month, expected in summaries:
        summary = tools.spending_summary(by=by, month=month)
        json.dumps(summary)
        assert summary["by"] == by and summary["month"] == month
        assert _flat(summary["totals"]) == expected, (by, month)
    assert tools.spending_summary(by="category")["month"] is None
    with pytest.raises(ToolError):
        tools.spending_summary(by="colour")

    # --- tool specs exposed to the agent
    assert [spec["function"]["name"] for spec in TOOL_SPECS] == [
        "get_member_statement", "get_balances", "search_expenses", "spending_summary"
    ]
    for spec in TOOL_SPECS:
        assert set(spec) == {"type", "function"} and spec["type"] == "function"
        function = spec["function"]
        assert set(function) == {"name", "description", "parameters"}
        assert isinstance(function["description"], str) and function["description"].strip()
        assert function["parameters"]["type"] == "object"
        assert isinstance(function["parameters"].get("properties", {}), dict)
    json.dumps(TOOL_SPECS)
    required = {s["function"]["name"]: s["function"]["parameters"].get("required", []) for s in TOOL_SPECS}
    assert required == {
        "get_member_statement": [], "get_balances": [],
        "search_expenses": [], "spending_summary": ["by"]
    }
    search_spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "search_expenses")
    assert {"text", "category", "subcategory", "payer_id", "currency", "month", "min_total", "max_total", "limit"} <= set(
        search_spec["function"]["parameters"]["properties"]
    )


def test_payers_with_the_same_name_keep_separate_totals():
    # two roster members called "דני" (ids 2 and 5): both keys carry the id, so they never collapse
    members = [Member(id=1, name="Ann"), Member(id=2, name="דני"), Member(id=3, name="Cy"), Member(id=5, name="דני")]
    store = Store(":memory:")
    _seed(store, "Taxi", 3000, Currency.ILS, Subcategory.other,
          {2: 1000, 5: 2000}, {1: 1000, 2: 1000, 3: 1000}, date(2026, 9, 1))  # the two Danis paid 10.00 and 20.00
    _seed(store, "Groceries", 5000, Currency.ILS, Subcategory.groceries,
          {1: 5000}, {1: 2500, 3: 2500}, date(2026, 9, 2))
    summary = ReadTools(store, CHAT, members).spending_summary(by="payer")
    assert _flat(summary["totals"]) == {
        "ILS": {"Ann": ("50", 5000), "דני (2)": ("10", 1000), "דני (5)": ("20", 2000)},
    }


def _totals_ledger(*totals):
    store = Store(":memory:")
    for i, total in enumerate(totals):
        _seed(store, f"E{total}", total, Currency.ILS, Subcategory.other, {ANN: total}, {ANN: total}, date(2026, 9, 1 + i))
    return store


def test_search_amount_filters_take_plain_amount_strings_and_convert_in_code():
    tools = ReadTools(_totals_ledger(15000, 14999, 3890, 3891), CHAT, MEMBERS)

    def found(**kwargs):
        return sorted(e["total_minor"] for e in tools.search_expenses(**kwargs)["expenses"])

    assert found(min_total="150") == [15000]  # 150 = 15000 minor units, inclusive; 14999 is out
    assert found(max_total="38.90") == [3890]  # inclusive; 3891 is out
    assert found(min_total="38.90", max_total="150") == [3890, 3891, 14999, 15000]  # both bounds inclusive
    assert found(min_total="38.91", max_total="149.99") == [3891, 14999]  # min and max together, edges in
    assert found(min_total="150", max_total="150") == [15000]

    search_spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "search_expenses")
    properties = search_spec["function"]["parameters"]["properties"]
    assert properties["min_total"]["type"] == "string"
    assert properties["max_total"]["type"] == "string"


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "1.200", "", 15000])
@pytest.mark.parametrize("argument", ["min_total", "max_total"])
def test_search_rejects_unreadable_or_ambiguous_amount_filters(argument, bad):
    store = _totals_ledger(15000)
    searched = []
    real_search = store.search_expenses
    store.search_expenses = lambda *a, **k: searched.append(1) or real_search(*a, **k)
    with pytest.raises(ToolError):
        ReadTools(store, CHAT, MEMBERS).search_expenses(**{argument: bad})
    assert searched == []  # rejected before the ledger is touched


def test_member_debt_statement_lists_per_expense_obligations_and_the_net_settlement():
    """A statement preserves the useful "what for?" detail even when debts offset in the net."""
    store = Store(":memory:")
    coffee = _seed(
        store, "Coffee", 3000, Currency.ILS, Subcategory.restaurant,
        {ANN: 3000}, {ANN: 1500, BEN: 1500}, date(2026, 9, 20),
    )
    supplies = _seed(
        store, "Supplies", 4000, Currency.ILS, Subcategory.supplies,
        {BEN: 4000}, {ANN: 2000, BEN: 2000}, date(2026, 9, 21),
    )

    statement = ReadTools(store, CHAT, MEMBERS).get_member_statement(member_id=BEN)

    assert (statement["member_id"], statement["member_name"]) == (BEN, "Ben")
    assert statement["obligations"] == [{
        "expense_id": coffee.id,
        "description": "Coffee",
        "spent_on": "2026-09-20",
        "currency": "ILS",
        "to": ANN,
        "to_name": "Ann",
        "amount": "15",
        "amount_minor": 1500,
    }]
    assert statement["receivables"] == [{
        "expense_id": supplies.id,
        "description": "Supplies",
        "spent_on": "2026-09-21",
        "currency": "ILS",
        "from": ANN,
        "from_name": "Ann",
        "amount": "20",
        "amount_minor": 2000,
    }]
    assert statement["settlements"] == [{
        "currency": "ILS",
        "from": ANN,
        "from_name": "Ann",
        "to": BEN,
        "to_name": "Ben",
        "amount": "5",
        "amount_minor": 500,
    }]


def test_member_debt_statement_is_exposed_as_a_read_tool_with_an_optional_member_id():
    spec = next(spec for spec in TOOL_SPECS if spec["function"]["name"] == "get_member_statement")
    assert spec["function"]["parameters"].get("required", []) == []
    assert spec["function"]["parameters"]["properties"]["member_id"]["type"] == "integer"
