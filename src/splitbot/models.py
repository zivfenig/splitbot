"""Pydantic models: the contracts between the parts.

Amounts are integers in minor units (agorot/cents), never float. The one exception is
`ExtractedExpense.amount`, which is the text the LLM found ("38.90"); money.py parses it.
"""

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

T = TypeVar("T")


class Strict(BaseModel):
    """LLM-facing models: unknown keys fail loudly instead of being silently dropped."""

    model_config = ConfigDict(extra="forbid")


class Currency(StrEnum):
    ILS = "ILS"
    USD = "USD"
    EUR = "EUR"


class Subcategory(StrEnum):
    electricity = "electricity"
    gas = "gas"
    water = "water"
    internet = "internet"
    rent = "rent"
    arnona = "arnona"
    groceries = "groceries"
    cleaning = "cleaning"
    supplies = "supplies"
    restaurant = "restaurant"
    delivery = "delivery"
    other = "other"


class Category(StrEnum):
    utilities = "utilities"
    rent = "rent"
    arnona = "arnona"
    groceries = "groceries"
    household = "household"
    eating_out = "eating_out"
    other = "other"


# The main category is always derived from the subcategory, so they can never contradict.
SUBCATEGORY_TO_CATEGORY: dict[Subcategory, Category] = {
    Subcategory.electricity: Category.utilities,
    Subcategory.gas: Category.utilities,
    Subcategory.water: Category.utilities,
    Subcategory.internet: Category.utilities,
    Subcategory.rent: Category.rent,
    Subcategory.arnona: Category.arnona,
    Subcategory.groceries: Category.groceries,
    Subcategory.cleaning: Category.household,
    Subcategory.supplies: Category.household,
    Subcategory.restaurant: Category.eating_out,
    Subcategory.delivery: Category.eating_out,
    Subcategory.other: Category.other,
}


def category_of(subcategory: Subcategory) -> Category:
    return SUBCATEGORY_TO_CATEGORY[subcategory]


class MessageType(StrEnum):
    new = "new"
    correction = "correction"
    delete = "delete"
    chat = "chat"


class Confidence(StrEnum):
    # Enum, not a float: LLM self-reported numbers are poorly calibrated and policy
    # only needs "low -> strictest".
    high = "high"
    medium = "medium"
    low = "low"


# --- members --------------------------------------------------------------


class Member(BaseModel):
    id: int
    name: str


class KnownMember(Strict):
    kind: Literal["known"] = "known"
    id: int


class Ambiguous(Strict):
    """The LLM could not tell which member was meant (e.g. two Danis): the bot asks."""

    kind: Literal["ambiguous"] = "ambiguous"
    candidates: list[int] = Field(min_length=2)

    @field_validator("candidates")
    @classmethod
    def candidates_are_distinct(cls, ids: list[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise ValueError("candidates must be distinct")
        return ids


# One named person: a known ID, or "ambiguous" with candidates.
MemberRef = Annotated[KnownMember | Ambiguous, Field(discriminator="kind")]


class Participants(Strict):
    """What the message says about who shares the expense. The LLM reports; CODE decides
    (validation.resolve_participants): (author + `only`) - `exclude`, or everyone - `exclude`
    without `only`; neither = everyone."""

    only: list[MemberRef] | None = Field(default=None, min_length=1)  # "עם X" / "with X"
    exclude: list[MemberRef] = []  # "בלי X" / "without X"


class PersonAmount(Strict):
    """One stated amount: "דני 50". `amount` is text as written; money.py parses it."""

    member: MemberRef
    amount: str
    evidence: str


# --- extraction (LLM output, untrusted until validated) --------------------


class Evidenced(Strict, Generic[T]):
    """A value plus the exact text it came from. `source: "default"` = not in the message."""

    value: T
    evidence: str | None = None
    source: Literal["message", "default"]


class ExtractedExpense(Strict):
    message_type: MessageType
    confidence: Confidence
    amount: Evidenced[str] | None = None  # text as written, e.g. "38.90"; money.py parses it
    # True when the amount was written in words or shorthand ("מאתיים", "2 אלף", "1.5K") and the
    # LLM converted it to digits in `amount.value`; `amount.evidence` is then the words as written.
    # Code cannot verify that conversion, so a human always confirms it (see policy).
    amount_in_words: bool = Field(default=False, strict=True)
    currency: Evidenced[Currency] | None = None
    payer: Evidenced[MemberRef] | None = None
    participants: Evidenced[Participants] | None = None
    exact_amounts: list[PersonAmount] | None = Field(default=None, min_length=1)  # else equal split
    # correction/delete only: the words that identify the target expense ("240", "הפיצה של אתמול")
    refers_to: Evidenced[str] | None = None
    subcategory: Subcategory | None = None
    description: str | None = None  # free text, kept as written

    @property
    def category(self) -> Category | None:
        return category_of(self.subcategory) if self.subcategory else None


# --- internal records -----------------------------------------------------


class Share(BaseModel):
    user_id: int
    paid: int  # minor units
    owed: int  # minor units


class ExpenseState(StrEnum):
    """Shared by expenses and change requests (corrections/deletes)."""

    pending_confirmation = "pending_confirmation"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Expense(BaseModel):
    """One ledger record. The chat is the group: `chat_id` scopes every ledger query."""

    id: int | None = None
    chat_id: int
    message_id: int
    author_id: int
    description: str
    total: int  # minor units
    currency: Currency
    subcategory: Subcategory
    shares: list[Share]
    prompt_version: str
    spent_on: date
    created_at: datetime = Field(default_factory=_utc_now)  # timezone-aware, for the 48h expiry
    state: ExpenseState = ExpenseState.pending_confirmation
    deleted: bool = False  # soft delete: the row stays, but no balance/search/summary counts it
    words: str | None = None  # the words the amount was written in, when the LLM converted it

    @model_validator(mode="after")
    def shares_sum_exactly_to_total(self) -> "Expense":
        if self.total <= 0:
            raise ValueError("total must be positive")
        if any(s.paid < 0 or s.owed < 0 for s in self.shares):
            raise ValueError("shares cannot be negative")
        if len({s.user_id for s in self.shares}) != len(self.shares):
            raise ValueError("each member can appear only once in the shares")
        if sum(s.paid for s in self.shares) != self.total:
            raise ValueError("paid shares must sum to the total")
        if sum(s.owed for s in self.shares) != self.total:
            raise ValueError("owed shares must sum to the total")
        return self

    @property
    def category(self) -> Category:
        return category_of(self.subcategory)


class ApprovalMode(StrEnum):
    author = "author"  # default: the sender's explicit approval
    auto = "auto"  # opt-in: still shows the confirmation, commits after a grace window


class ApprovalRule(BaseModel):
    """e.g. rent=all -> category=rent, mode=all; over_500=all -> min_amount=50000, mode=all.
    The threshold is inclusive (>=). Both category and min_amount set = both must match (AND)."""

    mode: ApprovalMode
    category: Category | None = None
    min_amount: int | None = None  # minor units


class ChangeKind(StrEnum):
    correction = "correction"
    delete = "delete"


class ChangeRequest(BaseModel):
    """A correction or delete of a confirmed expense. It changes the ledger only after ALL the
    relevant people approved (see policy.relevant_approvers), the requester included: nobody is
    approved automatically, everyone presses approve after seeing the exact numbers."""

    id: int | None = None
    chat_id: int
    message_id: int  # the message that asked for the change (idempotency key with chat_id)
    expense_id: int
    kind: ChangeKind
    requested_by: int
    proposed: Expense | None = None  # correction: the full replacement expense; delete: None
    required_approvers: list[int]
    approvals: dict[int, bool] = {}  # user id -> True (✓) / False (✗)
    created_at: datetime = Field(default_factory=_utc_now)
    state: ExpenseState = ExpenseState.pending_confirmation
    version: int = 0  # managed by the store: bumped on every write, used for compare-and-swap

    @model_validator(mode="after")
    def is_consistent(self) -> "ChangeRequest":
        if (self.kind == ChangeKind.correction) != (self.proposed is not None):
            raise ValueError("a correction needs `proposed`; a delete has none")
        if not self.required_approvers or len(set(self.required_approvers)) != len(self.required_approvers):
            raise ValueError("required_approvers must be a non-empty list of distinct user ids")
        if self.requested_by not in self.required_approvers:
            raise ValueError("only a relevant person may request a change")
        if not set(self.approvals) <= set(self.required_approvers):
            raise ValueError("approvals can only come from the required approvers")
        return self


class GroupConfig(BaseModel):
    chat_id: int
    members: list[Member]
    default_mode: ApprovalMode = ApprovalMode.author
    rules: list[ApprovalRule] = []
