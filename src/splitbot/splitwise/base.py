"""The contract the rest of the app depends on. Nothing outside `splitwise/client.py`
knows about Splitwise HTTP details, so the backend can be swapped (or faked in tests).

Amounts are integers in agorot. Never float.
"""

from typing import Protocol

from pydantic import BaseModel


class BackendError(RuntimeError):
    """The backend refused or failed the request (includes HTTP 200 with `errors`)."""


class Member(BaseModel):
    id: int
    name: str


class Share(BaseModel):
    user_id: int
    paid: int  # agorot
    owed: int  # agorot


class NewExpense(BaseModel):
    group_id: int
    description: str
    total: int  # agorot
    shares: list[Share]
    details: str = ""  # idempotency key goes here (Stage 3)
    currency_code: str = "ILS"


class BackendExpense(BaseModel):
    id: int
    group_id: int | None
    description: str
    total: int  # agorot
    currency_code: str
    details: str
    shares: list[Share]
    deleted: bool = False


class ExpenseBackend(Protocol):
    def get_current_user(self) -> Member: ...

    def get_members(self, group_id: int) -> list[Member]: ...

    def add_expense(self, expense: NewExpense) -> BackendExpense: ...

    def get_expense(self, expense_id: int) -> BackendExpense: ...

    def delete_expense(self, expense_id: int) -> None: ...
