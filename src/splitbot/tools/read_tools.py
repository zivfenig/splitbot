"""Read tools for the agent: balances, search, summaries. Read-only, free to call. Every number is
computed in code from the ledger (`Store`), per currency (currencies are never mixed), and only
confirmed, not-deleted expenses of ONE chat are ever visible. Amounts are returned both as
minor-unit integers (`*_minor`) and as display strings ("38.90", "120") for the reader."""

import re
from dataclasses import dataclass

from splitbot.models import Category, Currency, Member, Subcategory
from splitbot.money import display_amount
from splitbot.store import Store

_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_BY = ("category", "subcategory", "month", "payer")


class ToolError(ValueError):
    """A read tool got an argument it cannot use (unknown category/currency/`by`, bad month)."""


@dataclass(frozen=True)
class Transfer:
    from_user: int  # the one who owes
    to_user: int  # the one who is owed
    amount: int  # minor units, > 0


def settlement_transfers(balances: dict[int, int]) -> list[Transfer]:
    """A small set of transfers that settle `balances` (user id -> net minor units, positive = is owed,
    negative = owes). Greedy: repeatedly the largest debtor pays the largest creditor
    min(debt, credit); ties are broken by the lower user id, so the result is deterministic.
    Users with a 0 balance take no part. Every transfer has amount > 0, and applying them brings every
    balance to 0. Raises ValueError when the balances do not sum to 0."""
    if sum(balances.values()) != 0:
        raise ValueError("balances must sum to 0")
    debtors = {user: -amount for user, amount in balances.items() if amount < 0}
    creditors = {user: amount for user, amount in balances.items() if amount > 0}
    transfers = []
    while debtors:
        debtor = min(debtors, key=lambda user: (-debtors[user], user))
        creditor = min(creditors, key=lambda user: (-creditors[user], user))
        amount = min(debtors[debtor], creditors[creditor])
        transfers.append(Transfer(from_user=debtor, to_user=creditor, amount=amount))
        debtors[debtor] -= amount
        creditors[creditor] -= amount
        if debtors[debtor] == 0:
            del debtors[debtor]
        if creditors[creditor] == 0:
            del creditors[creditor]
    return transfers


class ReadTools:
    """`members` gives the display names. All three tools return JSON-serializable dicts."""

    def __init__(self, store: Store, chat_id: int, members: list[Member]):
        self._store = store
        self._chat_id = chat_id
        self._names = {m.id: m.name for m in members}

    def _name(self, user_id: int) -> str:
        return self._names.get(user_id, str(user_id))

    def _payer_key(self, user_id: int) -> str:
        """The name, plus the id in brackets only when another roster member has the same name."""
        name = self._name(user_id)
        shared = sum(1 for other in self._names.values() if other == name) > 1
        return f"{name} ({user_id})" if shared else name

    def get_balances(self) -> dict:
        """{"balances": {<currency>: {"members": [{"user_id", "name", "net", "net_minor"}],
        "transfers": [{"from", "from_name", "to", "to_name", "amount", "amount_minor"}]}}}: only
        currencies that have expenses, members sorted by user id, `transfers` =
        `settlement_transfers` of that currency's balances. "net" is a display string ("-38.90")."""
        result = {}
        for currency, per_user in self._store.balances(self._chat_id).items():
            result[currency.value] = {
                "members": [
                    {"user_id": user, "name": self._name(user), "net": display_amount(net), "net_minor": net}
                    for user, net in sorted(per_user.items())
                ],
                "transfers": [
                    {
                        "from": t.from_user,
                        "from_name": self._name(t.from_user),
                        "to": t.to_user,
                        "to_name": self._name(t.to_user),
                        "amount": display_amount(t.amount),
                        "amount_minor": t.amount,
                    }
                    for t in settlement_transfers(per_user)
                ],
            }
        return {"balances": result}

    def search_expenses(
        self,
        *,
        text: str | None = None,
        category: str | None = None,
        subcategory: str | None = None,
        payer_id: int | None = None,
        currency: str | None = None,
        month: str | None = None,
        min_total: int | None = None,
        max_total: int | None = None,
        limit: int = 20,
    ) -> dict:
        """Wraps `Store.search_expenses` (same filters, newest first). `category`/`subcategory`/
        `currency` are the enum VALUES as strings ("eating_out", "groceries", "ILS"); an unknown one,
        a `month` that is not "YYYY-MM", or a limit outside 1..50 raises ToolError.
        Returns {"count": n, "expenses": [{"id", "description", "total", "total_minor",
        "currency", "category", "subcategory", "spent_on" ("YYYY-MM-DD"), "payers": [names],
        "shares": [{"user_id", "name", "paid", "owed"}]}]} (display strings for the amounts)."""
        if not 1 <= limit <= 50:
            raise ToolError("limit must be between 1 and 50")
        if month is not None:
            _check_month(month)
        found = self._store.search_expenses(
            self._chat_id,
            text=text,
            category=_enum(Category, category, "category"),
            subcategory=_enum(Subcategory, subcategory, "subcategory"),
            payer_id=payer_id,
            currency=_enum(Currency, currency, "currency"),
            month=month,
            min_total=min_total,
            max_total=max_total,
            limit=limit,
        )
        expenses = [
            {
                "id": e.id,
                "description": e.description,
                "total": display_amount(e.total),
                "total_minor": e.total,
                "currency": e.currency.value,
                "category": e.category.value,
                "subcategory": e.subcategory.value,
                "spent_on": e.spent_on.isoformat(),
                "payers": [self._name(s.user_id) for s in e.shares if s.paid > 0],
                "shares": [
                    {"user_id": s.user_id, "name": self._name(s.user_id), "paid": display_amount(s.paid), "owed": display_amount(s.owed)}
                    for s in e.shares
                ],
            }
            for e in found
        ]
        return {"count": len(expenses), "expenses": expenses}

    def spending_summary(self, *, by: str, month: str | None = None) -> dict:
        """Wraps `Store.spending_summary`. `by` in category | subcategory | month | payer (else
        ToolError); `month` "YYYY-MM" or None. Returns {"by": by, "month": month, "totals":
        {<currency>: {<key>: {"amount", "amount_minor"}}}}; for `payer` the key is the user's
        NAME (user id when unknown); when two roster members share a name, both keys carry the id
        ("דני (2)", "דני (5)")."""
        if by not in _BY:
            raise ToolError(f"by must be one of {', '.join(_BY)}")
        if month is not None:
            _check_month(month)
        totals = {}
        for currency, per_key in self._store.spending_summary(self._chat_id, by=by, month=month).items():
            totals[currency.value] = {
                (self._payer_key(int(key)) if by == "payer" else key): {"amount": display_amount(minor), "amount_minor": minor}
                for key, minor in per_key.items()
            }
        return {"by": by, "month": month, "totals": totals}


def _enum(enum_type, value: str | None, label: str):
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        raise ToolError(f"unknown {label}: {value!r}") from None


def _check_month(month: str) -> None:
    if not _MONTH.fullmatch(month):
        raise ToolError("month must look like 2026-09")


# JSON-schema tool specs for OpenAI tool calling (the agent gets these in Stage E): one entry per
# read tool, {"type": "function", "function": {"name": ..., "description": ..., "parameters":
# {...JSON schema, no required arguments except `by` for spending_summary...}}}.
_CATEGORIES = [c.value for c in Category]
_SUBCATEGORIES = [c.value for c in Subcategory]
_CURRENCIES = [c.value for c in Currency]

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_balances",
            "description": "Who owes whom in this chat, per currency: each member's net balance and a small set of "
            "transfers that settle it. Amounts are computed by code from the ledger.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_expenses",
            "description": "Search this chat's confirmed expenses, newest first. All filters are optional and combined with AND.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "case-insensitive text inside the description"},
                    "category": {"type": "string", "enum": _CATEGORIES},
                    "subcategory": {"type": "string", "enum": _SUBCATEGORIES},
                    "payer_id": {"type": "integer", "description": "user id of someone who paid"},
                    "currency": {"type": "string", "enum": _CURRENCIES},
                    "month": {"type": "string", "description": "YYYY-MM"},
                    "min_total": {"type": "integer", "description": "minimum total in minor units (agorot/cents)"},
                    "max_total": {"type": "integer", "description": "maximum total in minor units (agorot/cents)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spending_summary",
            "description": "Total spending of this chat, per currency, grouped by category, subcategory, month or payer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": list(_BY)},
                    "month": {"type": "string", "description": "optional YYYY-MM filter"},
                },
                "required": ["by"],
                "additionalProperties": False,
            },
        },
    },
]
