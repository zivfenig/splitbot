"""Money: pure functions on integers in minor units (agorot/cents). Never float.

ILS, USD and EUR all have 2 decimals, so there is no currency argument yet.
Anything ambiguous raises ValueError: the bot asks, it never guesses.
"""

import re

from splitbot.models import Share

# "1,200" / "1,200.50": comma = thousands (exactly 3 digits after it)
_THOUSANDS = re.compile(r"[0-9]{1,3}(,[0-9]{3})+(\.[0-9]{1,2})?")
# "38.90" / "240"
_PLAIN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
# "38,90" / "38,9": comma + 1-2 digits = decimal
_COMMA_DECIMAL = re.compile(r"[0-9]+,[0-9]{1,2}")


def parse_amount(text: str) -> int:
    """User-written amount ("38.90", "38,90", "1,200") -> minor units. Must be positive."""
    s = text.strip()
    if _THOUSANDS.fullmatch(s):
        s = s.replace(",", "")
    elif _COMMA_DECIMAL.fullmatch(s):
        s = s.replace(",", ".")
    elif not _PLAIN.fullmatch(s):
        raise ValueError(f"cannot read amount {text!r}")
    whole, _, frac = s.partition(".")
    minor = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if minor <= 0:
        raise ValueError("amount must be positive")
    return minor


def format_amount(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 100)
    return f"{sign}{whole}.{frac:02d}"


def _equal_owed(total: int, payer_id: int, participants: list[int]) -> dict[int, int]:
    if not participants:
        raise ValueError("nobody to split with")
    if len(set(participants)) != len(participants):
        raise ValueError("duplicate participants")
    base, leftover = divmod(total, len(participants))
    owed = {p: base for p in participants}
    # Leftover goes to the payer if they take part, else to the first participant.
    owed[payer_id if payer_id in owed else participants[0]] += leftover
    return owed


def _exact_owed(total: int, author_id: int, exact: dict[int, int]) -> dict[int, int]:
    if any(amount <= 0 for amount in exact.values()):
        raise ValueError("stated amounts must be positive")
    remainder = total - sum(exact.values())
    if author_id in exact:
        if remainder != 0:
            raise ValueError("stated amounts do not add up to the total")
        return dict(exact)
    if remainder < 0:
        raise ValueError("stated amounts are more than the total")
    return {**exact, author_id: remainder} if remainder else dict(exact)


def split_expense(
    total: int,
    *,
    payer_id: int,
    author_id: int,
    participants: list[int],
    exact: dict[int, int] | None = None,
) -> list[Share]:
    """Shares that always sum exactly to `total`. `exact` (member id -> minor units) wins
    over `participants`; without it the split is equal."""
    if total <= 0:
        raise ValueError("total must be positive")
    owed = _exact_owed(total, author_id, exact) if exact else _equal_owed(total, payer_id, participants)
    ids = list(owed) if payer_id in owed else [*owed, payer_id]
    return [Share(user_id=i, paid=total if i == payer_id else 0, owed=owed.get(i, 0)) for i in ids]
