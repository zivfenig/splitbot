"""Pure validators for LLM output. No network, no LLM.

Order of use: check member references (real IDs, no ambiguity) -> resolve participants.
"""

import re
import unicodedata

from splitbot.models import Currency, ExtractedExpense, KnownMember, MemberRef, Participants
from splitbot.money import parse_amount

_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
# Foreign currency written in the message: symbols, codes, and the words for dollar/euro/pound.
# A heuristic, not a proof: it only stops the common case of a foreign currency hidden by the
# default ILS. Hebrew words allow one prefix letter ("בדולר") but must end at a word boundary,
# so "אירוע" (event) does not count as "אירו" (euro).
_FOREIGN_CURRENCY = re.compile(
    r"[$€£]"
    r"|(?<!\w)(?:usd|eur|gbp|dollars?|euros?|pounds?)(?!\w)"
    r"|(?<!\w)[בלהמשכו]?(?:דולר(?:ים)?|אירו|יורו|פאונד(?:ים)?)(?!\w)"
)


def check_members(extracted: ExtractedExpense, member_ids: list[int]) -> list[str]:
    """Every member the LLM named must be a real group member and unambiguous.
    Returns issues; an empty list means OK. Any issue -> the bot asks."""
    refs: list[tuple[str, MemberRef]] = []
    if extracted.payer:
        refs.append(("payer", extracted.payer.value))
    if extracted.participants:
        refs += [("participants.only", r) for r in extracted.participants.value.only or []]
        refs += [("participants.exclude", r) for r in extracted.participants.value.exclude]
    refs += [("exact_amounts", p.member) for p in extracted.exact_amounts or []]

    issues = []
    for where, ref in refs:
        if isinstance(ref, KnownMember):
            if ref.id not in member_ids:
                issues.append(f"{where}: unknown member id {ref.id}")
            continue
        issues.append(f"{where}: ambiguous member, candidates {ref.candidates}: ask which one")
        issues += [f"{where}: unknown member id {c}" for c in ref.candidates if c not in member_ids]
    return issues


def _ids(refs: list[MemberRef], member_ids: list[int]) -> set[int]:
    ids = set()
    for ref in refs:
        if not isinstance(ref, KnownMember):
            raise ValueError("ambiguous member reference: the bot must ask first")
        if ref.id not in member_ids:
            raise ValueError(f"unknown member id {ref.id}")
        ids.add(ref.id)
    return ids


def resolve_participants(participants: Participants, author_id: int, member_ids: list[int]) -> list[int]:
    """Final list of member IDs sharing the expense, in group order.

    `only` present -> exactly `only` minus `exclude`; otherwise everyone minus `exclude`.
    Neither -> everyone. Paying does not imply participating: "the pizza of Dani" can be paid
    by the author while only Dani owes a share.
    The result can be EMPTY (everyone excluded): the caller must then ask, not split.
    """
    if participants.only is not None:
        chosen = _ids(participants.only, member_ids)
    else:
        chosen = set(member_ids)
    chosen -= _ids(participants.exclude, member_ids)
    return [m for m in member_ids if m in chosen]


# --- grounding: is what the LLM says really in the message? -------------------
# Limit: evidence proves the text exists, not that the interpretation is right.


def normalize(text: str) -> str:
    """Light normalization: NFKC, lowercase, no niqqud/diacritics, collapsed whitespace."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


def _evidence_issues(name: str, evidence: str | None, source: str, message: str) -> list[str]:
    if source != "message":
        return []
    if not evidence or not normalize(evidence):
        return [f"{name}: source is message but there is no evidence"]
    if normalize(evidence) not in message:
        return [f"{name}: evidence not found in the message"]
    return []


def _amount_issues(name: str, amount: str, evidence: str, message: str) -> list[str]:
    """The evidence holds exactly one number, it equals the amount, and it is a whole number
    in the message (not a slice of a longer one, e.g. "40" inside "240")."""
    tokens = _NUMBER.findall(evidence)
    if len(tokens) != 1:
        return [f"{name}: evidence must contain exactly one numeric token, found {len(tokens)}"]
    try:
        from_evidence, stated = parse_amount(tokens[0]), parse_amount(amount)
    except ValueError:
        return [f"{name}: cannot read the amount or its evidence"]
    if from_evidence != stated:
        return [f"{name}: {amount} does not match the number in its evidence ({tokens[0]})"]
    if tokens[0] not in _NUMBER.findall(message):
        return [f"{name}: {tokens[0]} is only part of a longer number in the message"]
    return []


def check_grounding(extracted: ExtractedExpense, message: str, *, author_id: int) -> list[str]:
    """Issues for anything not backed by the message. Empty list = grounded. Any issue -> ask.

    `amount_in_words`: when True, the amount's evidence must be non-blank and found in the message
    and its source must be "message"; the "exactly one numeric token" check is skipped (code
    cannot verify a conversion, so a human confirms it) and mixed digits-and-words such as
    "2 אלף" or "1.5K" count as words. `amount.value` must still be a positive amount that
    `money.parse_amount` reads (so the confirmation can show it), else an issue naming
    `amount`. This is NOT a clarification by itself: no issue is raised just because the flag
    is True. When the flag is False, digitless evidence is an issue, as before.

    `refers_to` (which expense a correction/delete means): when present, its source must be
    "message" and its evidence must be non-blank and found in the message. There is no
    message-type-gated rule here any more (action selection is the Agent's tool choice, not
    something the extractor reports): a null `refers_to` is always fine, including on a
    correction or delete (a Telegram reply, or a search-result pick, can identify the target
    instead). Every issue about it names `refers_to`.
    """
    text = normalize(message)
    issues: list[str] = []

    if extracted.amount:
        a = extracted.amount
        if a.source != "message":
            issues.append("amount: cannot be a default, it must come from the message")
        issues += _evidence_issues("amount", a.evidence, a.source, text)
        if a.evidence and a.source == "message":
            if extracted.amount_in_words:
                # Code cannot check a conversion ("מאה" -> "100"): a human confirms it (policy).
                # It only has to be a readable positive amount, so the confirmation can show it.
                try:
                    parse_amount(a.value)
                except ValueError:
                    issues.append("amount: the converted amount is not a readable positive number")
            else:
                issues += _amount_issues("amount", a.value, a.evidence, text)

    if extracted.currency:
        c = extracted.currency
        if c.value != Currency.ILS and c.source != "message":
            issues.append("currency: a non-ILS currency needs evidence from the message")
        issues += _evidence_issues("currency", c.evidence, c.source, text)
        if c.value == Currency.ILS and c.source == "default" and mentions_foreign_currency(message):
            issues.append("currency: the message seems to name a foreign currency but ILS is a default")

    if extracted.payer:
        p = extracted.payer
        if p.source == "default" and not (isinstance(p.value, KnownMember) and p.value.id == author_id):
            issues.append("payer: a default payer must be the author")
        issues += _evidence_issues("payer", p.evidence, p.source, text)

    if extracted.participants:
        pt = extracted.participants
        if pt.source == "default" and (pt.value.only is not None or pt.value.exclude):
            issues.append("participants: a default must mean everyone")
        issues += _evidence_issues("participants", pt.evidence, pt.source, text)

    if extracted.refers_to:
        r = extracted.refers_to
        if r.source != "message":
            issues.append("refers_to: must come from the message")
        issues += _evidence_issues("refers_to", r.evidence, r.source, text)

    for i, person in enumerate(extracted.exact_amounts or []):
        name = f"exact_amounts[{i}]"
        issues += _evidence_issues(name, person.evidence, "message", text)
        issues += _amount_issues(name, person.amount, person.evidence, text)
    return issues


def mentions_foreign_currency(message: str) -> bool:
    """True when the message names a foreign currency: a symbol ($ € £), a code (USD, EUR, GBP) or
    the English/Hebrew word for dollar, euro or pound (a one-letter Hebrew prefix such as "ב" is
    allowed; "אירוע" and "אירוח" are NOT the euro). Heuristic, same rule `check_grounding` uses."""
    visible = "".join(c for c in message if unicodedata.category(c) != "Cf")  # no hiding it in zero-width characters
    return _FOREIGN_CURRENCY.search(normalize(visible)) is not None
