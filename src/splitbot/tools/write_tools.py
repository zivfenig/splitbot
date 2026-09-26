"""Write tools: propose_expense / propose_correction / propose_delete. They NEVER write confirmed
data: every write runs extractor -> validators -> completeness checks -> policy -> a confirmation
built from the code template -> a PENDING record (idempotent per (chat_id, message_id)). The
confirmation is always produced, whatever the model's confidence. Message text is data."""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from splitbot.config import pending_expiry
from splitbot.llm.client import LLMClient
from splitbot.llm.extractor import Extraction, extract
from splitbot.models import (
    Ambiguous,
    Category,
    ChangeKind,
    Confidence,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    GroupConfig,
    KnownMember,
    Member,
    MessageType,
    Participants,
    Share,
    Subcategory,
)
from splitbot.money import display_amount, parse_amount, split_expense
from splitbot.policy import relevant_approvers, required_approval
from splitbot.store import DuplicateMessage, Store
from splitbot.validation import mentions_foreign_currency, normalize, resolve_participants

_CATEGORY_LABELS = {
    Category.utilities: "חשבונות",
    Category.rent: "שכירות",
    Category.arnona: "ארנונה",
    Category.groceries: "סופר",
    Category.household: "ציוד לבית",
    Category.eating_out: "אוכל בחוץ",
    Category.other: "אחר",
}
_SYMBOLS = {Currency.ILS: "₪", Currency.USD: "$", Currency.EUR: "€"}
_MAX_LABEL_CHARS = 60
_UNKNOWN_SENDER = "the sender is not a member of this group"


def _clean(text: str, cap: int = _MAX_LABEL_CHARS) -> str:
    """Text from a message or the roster, made safe to show: format characters (bidi overrides and
    marks, zero-width characters: Cf) are removed; control characters and line/paragraph separators
    (Cc, Zl, Zp) become a space; whitespace runs collapse; the result is stripped and capped."""
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        kept.append(" " if category in ("Cc", "Zl", "Zp") else char)
    return " ".join("".join(kept).split())[:cap].strip()


@dataclass(frozen=True)
class Proposal:
    status: Literal["pending_confirmation", "needs_clarification", "duplicate"]
    expense_id: int | None = None  # the pending new expense, or the target of a correction/delete
    change_request_id: int | None = None
    approvers: tuple[int, ...] = ()  # who must approve: new expense = (sender,); change = the relevant people
    mode: Literal["author", "auto"] | None = None  # new expenses only (policy)
    confirmation_text: str | None = None  # from the code template; None when there is nothing to confirm
    issues: tuple[str, ...] = ()  # why clarification is needed (fixed reasons, never message text)
    expires_at: datetime | None = None  # created_at + config.pending_expiry()


class WriteTools:
    """`llm` is an LLMClient (tests use a scripted fake). `config` is the group's GroupConfig (roster
    and approval rules); `members` the roster. `clock` returns the current timezone-aware
    datetime (default: now in UTC): it stamps `created_at`, `spent_on` (its date, unless given)
    and `expires_at`."""

    def __init__(
        self,
        store: Store,
        llm: LLMClient,
        members: list[Member],
        config: GroupConfig,
        *,
        prompt_version: str = "extract_v2",
        clock: Callable[[], datetime] | None = None,
    ):
        self._store = store
        self._llm = llm
        self._members = members
        self._config = config
        self._prompt_version = prompt_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._names = {m.id: m.name for m in members}

    def propose_expense(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str, spent_on: date | None = None
    ) -> Proposal:
        """0. `sender_id` must be on the roster (`members`), else "needs_clarification" with a fixed
        issue, NO LLM call, no write, and the message is NOT marked processed (in all three propose
        tools). `config.pending_expiry()` is read up front: a bad PENDING_EXPIRY_HOURS raises
        ConfigError BEFORE any write.
        1. Idempotency: if (chat_id, message_id) was already processed, return status
        "duplicate" (with the existing expense id and its confirmation text when it produced
        one), with NO LLM call and no write. 2. `extractor.extract(text, ...)`; an LLMError
        propagates. 3. Anything that is not a clean new expense returns "needs_clarification"
        with fixed-text `issues` and NO record (the message is still marked processed, so a
        redelivery does not ask twice): extractor status needs_clarification, message_type other
        than "new", no amount, an amount `money.parse_amount` rejects ("1.200", above the cap),
        an empty participant list, exact amounts that do not add up / repeat a member,
        or any other ValueError from the money code. 4. Otherwise build the Expense with the
        money code (currency ILS by default (a null currency with low confidence, or a null
        currency while `validation.mentions_foreign_currency(text)`, asks instead), payer = sender unless the message names another,
        participants from `validation.resolve_participants`, `money.split_expense`, the
        subcategory or "other"), state pending_confirmation, `created_at` = clock(),
        `prompt_version` recorded, and save it (`Store.save_expense`; a concurrent second save of
        the same message raises DuplicateMessage inside and is reported as "duplicate").
        5. mode = `policy.required_approval(config, category, total, currency, confidence,
        amount_in_words)`; approvers = (sender_id,); `confirmation_text` = the template below;
        status "pending_confirmation".

        Confirmation template (Hebrew, all values from the validated data, never written by the
        LLM): "<label>: <names>, <total> <symbol> (<shares>) — לאשר?" where <label> is the
        extractor's description ONLY if it occurs in the message text (compared with
        `validation.normalize`), otherwise the fixed Hebrew label of the MAIN category
        (utilities "חשבונות", rent "שכירות", arnona "ארנונה", groceries "סופר", household "ציוד
        לבית", eating_out "אוכל בחוץ", other "אחר"); <names> = the roster names of everyone with
        an owed share > 0 in roster order ("זיו", "זיו ומיכל", "זיו, דני ומיכל"); <total> and
        <shares> use `money.display_amount` (shares joined with "/", same order as the names);
        <symbol> is ₪ for ILS, $ for USD, € for EUR. Every piece of text that comes from the message or
        the roster (the label, the words, the member names) is CLEANED before it is shown: all Unicode
        control and format characters (categories Cc, Cf, Zl, Zp: bidi overrides and marks, zero-width
        characters, newlines) are removed, whitespace runs collapse to one space, the result is
        stripped and capped at 60 characters (an empty result falls back to the category label). If the payer is not the sender, append
        " · שילם/ה: <payer name>". If the amount was written in words, append the words:
        ' · הסכום הומר מ"<words>"' (the evidence)."""
        expiry = pending_expiry()  # a bad PENDING_EXPIRY_HOURS raises before anything is written
        if sender_id not in self._names:
            return Proposal(status="needs_clarification", issues=(_UNKNOWN_SENDER,))
        existing = self._replay(chat_id, message_id, sender_id)
        if existing:
            return existing
        extraction = self._extract(text, sender_id)  # an LLMError propagates: nothing is marked processed
        issues = self._extraction_issues(extraction, MessageType.new)
        built = None if issues else self._build_expense(extraction, chat_id, message_id, sender_id, text, spent_on, issues)
        if issues:
            return self._clarify(chat_id, message_id, issues)
        expense, confidence = built
        try:
            saved = self._store.save_expense(expense)
        except DuplicateMessage:
            return self._replay(chat_id, message_id, sender_id) or Proposal(status="duplicate")
        mode = required_approval(
            self._config,
            category=saved.category,
            total=saved.total,
            currency=saved.currency,
            confidence=confidence,
            amount_in_words=saved.words is not None,
        )
        return Proposal(
            status="pending_confirmation",
            expense_id=saved.id,
            approvers=(sender_id,),
            mode=mode.value,
            confirmation_text=self._confirmation(saved),
            expires_at=saved.created_at + expiry,
        )

    def propose_correction(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str, target_expense_id: int | None
    ) -> Proposal:
        """Same steps 1-2 as propose_expense. The target (from a Telegram reply or a user pick)
        must be given, exist, belong to `chat_id`, be confirmed and not deleted; the extractor's
        message_type must be "correction" with a new amount; the original must have exactly one
        payer; the sender must be one of the relevant approvers of the target (payer or someone
        with an owed share). Anything else -> "needs_clarification" (no record, message marked
        processed). Otherwise the proposed replacement keeps the original participants and payer
        with the new total split by `money.split_expense`; the ChangeRequest (kind correction,
        requested_by = sender, required approvers = `policy.relevant_approvers(original)`) is
        stored with `Store.create_change_request`; status "pending_confirmation" ALWAYS (a group of
        one included: nobody is approved automatically; there is no "applied" status).
        A correction also asks (needs_clarification, no record) when the message carries anything
        it does not apply: a currency different from the original's, a named payer, named
        participants (only/exclude), exact per-person amounts; or when the original was not an
        equal split. The proposed replacement's `words` is set only when the new amount was written
        in words, never copied from the original. `confirmation_text` starts with
        "תיקון" and must contain the label, BOTH the old and the new total (display amounts) and
        "לאשר"; its exact layout is not prescribed."""
        return self._propose_change(
            ChangeKind.correction, chat_id, message_id, sender_id, text, target_expense_id
        )

    def propose_delete(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str, target_expense_id: int | None
    ) -> Proposal:
        """Like propose_correction but the extractor's message_type must be "delete" and the
        ChangeRequest is kind delete. `confirmation_text` must contain the label, the total
        (display amount) and "לאשר"; it starts with "מחיקה"."""
        return self._propose_change(ChangeKind.delete, chat_id, message_id, sender_id, text, target_expense_id)

    def confirm_expense(self, expense_id: int, user_id: int, approve: bool) -> Expense:
        """The sender's answer to a new expense: `Store.respond_expense` with `now` = the clock."""
        return self._store.respond_expense(expense_id, user_id, approve, now=self._clock())

    def respond_change(self, request_id: int, user_id: int, approve: bool) -> ChangeRequest:
        """A relevant person's answer to a correction/delete: `Store.respond` with `now` = the clock."""
        return self._store.respond(request_id, user_id, approve, now=self._clock())

    # --- internals ------------------------------------------------------------

    def _extract(self, text: str, sender_id: int) -> Extraction:
        return extract(
            text, sender_id=sender_id, members=self._members, llm=self._llm, prompt_version=self._prompt_version
        )

    def _replay(self, chat_id: int, message_id: int, sender_id: int) -> Proposal | None:
        """A message that was already handled: no LLM call, no write. Returns the confirmation again
        when it produced a new expense."""
        if not self._store.is_processed(chat_id, message_id):
            return None
        expense = self._store.get_expense_by_message(chat_id, message_id)
        if expense is None:
            return Proposal(status="duplicate")
        return Proposal(
            status="duplicate",
            expense_id=expense.id,
            approvers=(expense.author_id,),
            confirmation_text=self._confirmation(expense),
        )

    def _clarify(self, chat_id: int, message_id: int, issues: list[str]) -> Proposal:
        self._store.mark_processed(chat_id, message_id)  # a redelivery must not ask twice
        return Proposal(status="needs_clarification", issues=tuple(issues))

    @staticmethod
    def _extraction_issues(extraction: Extraction, wanted: MessageType) -> list[str]:
        if extraction.status != "ok" or extraction.expense is None:
            return list(extraction.issues) or ["the message could not be understood"]
        if extraction.expense.message_type != wanted:
            return [f"the message is not a {wanted.value}"]
        return []

    def _build_expense(self, extraction, chat_id, message_id, sender_id, text, spent_on, issues):
        """The Expense from validated extraction fields and the money code, or None with a fixed
        reason appended to `issues`."""
        e = extraction.expense
        if e.amount is None:
            issues.append("no amount was given")
            return None
        try:
            total = parse_amount(e.amount.value)
        except ValueError:
            issues.append("the amount is ambiguous, unreadable or above the limit")
            return None
        # A null currency means either "not mentioned" (ILS by default) or "not supported" (the model
        # may still say medium or high confidence): never turn an unsupported currency into shekels.
        if e.currency is None and (e.confidence == Confidence.low or mentions_foreign_currency(text)):
            issues.append("the currency is missing or not supported")
            return None
        currency = e.currency.value if e.currency is not None else Currency.ILS
        payer_id = sender_id
        if e.payer is not None:
            if not isinstance(e.payer.value, KnownMember):
                issues.append("it is not clear who paid")
                return None
            payer_id = e.payer.value.id
        try:
            if e.exact_amounts:
                exact = {}
                for item in e.exact_amounts:
                    if not isinstance(item.member, KnownMember) or item.member.id in exact:
                        raise ValueError("exact amounts need each member once")
                    exact[item.member.id] = parse_amount(item.amount)
                shares = split_expense(total, payer_id=payer_id, author_id=sender_id, participants=[], exact=exact)
            else:
                participants = resolve_participants(
                    e.participants.value if e.participants else Participants(),
                    sender_id,
                    [m.id for m in self._members],
                )
                shares = split_expense(total, payer_id=payer_id, author_id=sender_id, participants=participants)
        except ValueError:
            issues.append("who shares the expense, or the amounts per person, could not be worked out")
            return None
        subcategory = e.subcategory or Subcategory.other
        category_label = _CATEGORY_LABELS[Expense.model_construct(subcategory=subcategory).category]
        described = _clean(e.description or "")
        label = described if described and normalize(described) in normalize(_clean(text, cap=len(text))) else category_label
        now = self._clock()
        expense = Expense(
            chat_id=chat_id,
            message_id=message_id,
            author_id=sender_id,
            description=label,
            total=total,
            currency=currency,
            subcategory=subcategory,
            shares=shares,
            prompt_version=self._prompt_version,
            spent_on=spent_on or now.date(),
            created_at=now,
            state=ExpenseState.pending_confirmation,
            words=_clean(e.amount.evidence, cap=len(e.amount.evidence)) if e.amount_in_words else None,
        )
        return expense, e.confidence

    def _propose_change(self, kind, chat_id, message_id, sender_id, text, target_expense_id) -> Proposal:
        expiry = pending_expiry()  # a bad PENDING_EXPIRY_HOURS raises before anything is written
        if sender_id not in self._names:
            return Proposal(status="needs_clarification", issues=(_UNKNOWN_SENDER,))
        existing = self._replay(chat_id, message_id, sender_id)
        if existing:
            return Proposal(status="duplicate")
        extraction = self._extract(text, sender_id)
        wanted = MessageType.correction if kind == ChangeKind.correction else MessageType.delete
        issues = self._extraction_issues(extraction, wanted)
        original = None if issues else self._target(target_expense_id, chat_id, sender_id, issues)
        proposed = None
        if original is not None and kind == ChangeKind.correction:
            proposed = self._corrected(original, extraction, text, issues)
        if issues:
            return self._clarify(chat_id, message_id, issues)
        required = relevant_approvers(original)
        request = ChangeRequest(
            chat_id=chat_id,
            message_id=message_id,
            expense_id=original.id,
            kind=kind,
            requested_by=sender_id,
            proposed=proposed,
            required_approvers=required,
            created_at=self._clock(),
        )
        try:
            stored = self._store.create_change_request(request)
        except DuplicateMessage:
            return Proposal(status="duplicate")
        except ValueError:
            return self._clarify(chat_id, message_id, ["the expense can no longer be changed"])
        return Proposal(
            status="pending_confirmation",
            expense_id=original.id,
            change_request_id=stored.id,
            approvers=tuple(required),
            confirmation_text=self._change_confirmation(kind, original, proposed),
            expires_at=stored.created_at + expiry,
        )

    def _target(self, target_id, chat_id, sender_id, issues) -> Expense | None:
        if target_id is None:
            issues.append("it is not clear which expense is meant")
            return None
        try:
            original = self._store.get_expense(target_id)
        except KeyError:
            issues.append("that expense does not exist")
            return None
        if original.chat_id != chat_id or original.state != ExpenseState.confirmed or original.deleted:
            issues.append("that expense cannot be changed")
            return None
        if sender_id not in relevant_approvers(original):
            issues.append("only someone who paid or shares that expense can change it")
            return None
        return original

    @staticmethod
    def _corrected(original: Expense, extraction: Extraction, text: str, issues: list[str]) -> Expense | None:
        e = extraction.expense
        payers = [s.user_id for s in original.shares if s.paid > 0]
        if len(payers) != 1:
            issues.append("a correction of an expense with several payers is not supported")
            return None
        if e.amount is None:
            issues.append("no new amount was given")
            return None
        # A correction only changes the amount. Anything else the message says (another currency, a
        # payer, participants, exact amounts) would be ignored silently, so ask instead.
        unsupported_currency = e.currency is None and (e.confidence == Confidence.low or mentions_foreign_currency(text))
        named_currency = e.currency is not None and e.currency.source == "message"
        named_payer = e.payer is not None and e.payer.source == "message"
        named_participants = e.participants is not None and e.participants.source == "message"
        if (
            (named_currency and e.currency.value != original.currency)
            or unsupported_currency
            or named_payer
            or named_participants
            or e.exact_amounts
        ):
            issues.append("a correction can only change the amount: the message says more than that")
            return None
        participants = [s.user_id for s in original.shares if s.owed > 0]
        try:
            total = parse_amount(e.amount.value)
            shares = split_expense(total, payer_id=payers[0], author_id=original.author_id, participants=participants)
            equal_original = split_expense(
                original.total, payer_id=payers[0], author_id=original.author_id, participants=participants
            )
        except ValueError:
            issues.append("the new amount is ambiguous, unreadable or above the limit")
            return None
        if {s.user_id: s.owed for s in equal_original} != {s.user_id: s.owed for s in original.shares}:
            issues.append("the original was not split equally: a correction cannot re-split it")
            return None
        words = _clean(e.amount.evidence, cap=len(e.amount.evidence)) if e.amount_in_words else None
        return Expense.model_validate(
            {**original.model_dump(), "id": None, "total": total, "shares": shares, "words": words}
        )

    # --- confirmation templates (code, never the LLM) -----------------------------

    def _name(self, user_id: int) -> str:
        return _clean(self._names.get(user_id, str(user_id)))

    def _confirmation(self, expense: Expense) -> str:
        order = {m.id: index for index, m in enumerate(self._members)}
        sharing = sorted((s for s in expense.shares if s.owed > 0), key=lambda s: (order.get(s.user_id, len(order)), s.user_id))
        names = [self._name(s.user_id) for s in sharing]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " ו" + names[-1]
        shares = "/".join(display_amount(s.owed) for s in sharing)
        text = f"{_clean(expense.description)}: {joined}, {display_amount(expense.total)} {_SYMBOLS[expense.currency]} ({shares})"
        payers = [s.user_id for s in expense.shares if s.paid > 0]
        if payers != [expense.author_id]:
            text += " · שילם/ה: " + ", ".join(self._name(user) for user in payers)
        if expense.words is not None:
            text += f' · הסכום הומר מ"{_clean(expense.words)}"'
        return text + " — לאשר?"

    def _change_confirmation(self, kind: ChangeKind, original: Expense, proposed: Expense | None) -> str:
        symbol = _SYMBOLS[original.currency]
        if kind == ChangeKind.delete:
            return f"מחיקה: {_clean(original.description)}, {display_amount(original.total)} {symbol} — לאשר?"
        return (
            f"תיקון: {_clean(original.description)}: {display_amount(original.total)} → "
            f"{display_amount(proposed.total)} {symbol} — לאשר?"
        )
