"""Pydantic models: the contracts between the parts.

Amounts are integers in minor units (agorot/cents), never float. The one exception is
`ExtractedExpense.amount`, which is the text the LLM found ("38.90"); money.py parses it.
"""

from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    (validation.resolve_participants): `only` wins over `exclude`, neither = everyone."""

    only: list[MemberRef] | None = Field(default=None, min_length=1)  # "עם X" / "with X"
    exclude: list[MemberRef] = []  # "בלי X" / "without X"


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
    currency: Evidenced[Currency] | None = None
    payer: Evidenced[MemberRef] | None = None
    participants: Evidenced[Participants] | None = None
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
    pending_approval = "pending_approval"
    approved = "approved"
    submitting = "submitting"
    submitted = "submitted"
    rejected = "rejected"
    expired = "expired"
    failed = "failed"


class Expense(BaseModel):
    id: int | None = None
    chat_id: int
    message_id: int
    group_id: int
    author_id: int
    description: str
    total: int  # minor units
    currency: Currency
    subcategory: Subcategory
    shares: list[Share]
    prompt_version: str
    state: ExpenseState = ExpenseState.pending_approval
    splitwise_id: int | None = None

    @property
    def category(self) -> Category:
        return category_of(self.subcategory)


class ApprovalMode(StrEnum):
    author = "author"  # default: the person who reported confirms
    all = "all"  # everyone involved confirms
    auto = "auto"  # opt-in only


class ApprovalRule(BaseModel):
    """e.g. rent=all -> category=rent, mode=all; over_500=all -> min_amount=50000, mode=all."""

    mode: ApprovalMode
    category: Category | None = None
    min_amount: int | None = None  # minor units


class GroupConfig(BaseModel):
    group_id: int
    chat_id: int
    members: list[Member]
    default_mode: ApprovalMode = ApprovalMode.author
    rules: list[ApprovalRule] = []
