"""httpx implementation of ExpenseBackend for the Splitwise API (Bearer API key).

Stage 0 scope: auth, timeouts, and the one rule that matters most — HTTP 200 with a
non-empty `errors` is a FAILURE. Retries/backoff/idempotency come in Stage 3.
"""

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from splitbot.splitwise.base import BackendError, BackendExpense, Member, NewExpense, Share

BASE_URL = "https://secure.splitwise.com/api/v3.0/"


def agorot_to_str(agorot: int) -> str:
    sign = "-" if agorot < 0 else ""
    whole, frac = divmod(abs(agorot), 100)
    return f"{sign}{whole}.{frac:02d}"


def str_to_agorot(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_exact())


def _member(raw: dict) -> Member:
    name = " ".join(p for p in (raw.get("first_name"), raw.get("last_name")) if p)
    return Member(id=raw["id"], name=name)


class SplitwiseClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ):
        self._http = http or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    # --- low level -------------------------------------------------------

    def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            response = self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise BackendError(f"Splitwise request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise BackendError(f"Splitwise HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise BackendError("Splitwise returned non-JSON") from exc
        if data.get("errors"):
            raise BackendError(f"Splitwise errors: {data['errors']}")
        if data.get("success") is False:
            raise BackendError("Splitwise reported success=false")
        return data

    # --- ExpenseBackend --------------------------------------------------

    def get_current_user(self) -> Member:
        return _member(self._call("GET", "get_current_user")["user"])

    def find_group_id(self, name: str) -> int:
        groups = self._call("GET", "get_groups")["groups"]
        matches = [g for g in groups if g["name"] == name]
        if len(matches) != 1:
            raise BackendError(f"Expected exactly one group named {name!r}, found {len(matches)}")
        return matches[0]["id"]

    def get_members(self, group_id: int) -> list[Member]:
        data = self._call("GET", f"get_group/{group_id}")
        return [_member(m) for m in data["group"]["members"]]

    def add_expense(self, expense: NewExpense) -> BackendExpense:
        if sum(s.paid for s in expense.shares) != expense.total:
            raise BackendError("Paid shares do not sum to the total")
        if sum(s.owed for s in expense.shares) != expense.total:
            raise BackendError("Owed shares do not sum to the total")
        body: dict = {
            "cost": agorot_to_str(expense.total),
            "description": expense.description,
            "details": expense.details,
            "date": datetime.now(timezone.utc).isoformat(),
            "currency_code": expense.currency_code,
            "group_id": expense.group_id,
        }
        for i, share in enumerate(expense.shares):
            body[f"users__{i}__user_id"] = share.user_id
            body[f"users__{i}__paid_share"] = agorot_to_str(share.paid)
            body[f"users__{i}__owed_share"] = agorot_to_str(share.owed)
        data = self._call("POST", "create_expense", json=body)
        created = data.get("expenses") or []
        if len(created) != 1:
            raise BackendError(f"Expected 1 expense in response, got {len(created)}")
        return self._parse_expense(created[0])

    def get_expense(self, expense_id: int) -> BackendExpense:
        return self._parse_expense(self._call("GET", f"get_expense/{expense_id}")["expense"])

    def delete_expense(self, expense_id: int) -> None:
        # Splitwise answers HTTP 200 even when the delete failed: `success` must be true.
        # Deletes are soft: get_expense still works afterwards, with `deleted_at` set.
        data = self._call("POST", f"delete_expense/{expense_id}")
        if data.get("success") is not True:
            raise BackendError(f"Splitwise did not confirm delete of expense {expense_id}")

    # --- parsing ---------------------------------------------------------

    @staticmethod
    def _parse_expense(raw: dict) -> BackendExpense:
        return BackendExpense(
            id=raw["id"],
            group_id=raw.get("group_id"),
            description=raw["description"],
            total=str_to_agorot(raw["cost"]),
            currency_code=raw["currency_code"],
            details=raw.get("details") or "",
            shares=[
                Share(
                    user_id=u["user_id"],
                    paid=str_to_agorot(u["paid_share"]),
                    owed=str_to_agorot(u["owed_share"]),
                )
                for u in raw.get("users", [])
            ],
            deleted=raw.get("deleted_at") is not None,
        )
