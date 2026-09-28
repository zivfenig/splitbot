"""Write tools: propose_expense / propose_correction / propose_delete. They NEVER write confirmed
data: every write runs extractor -> validators -> completeness checks -> policy -> a confirmation
built from the code template -> a PENDING record (idempotent per (chat_id, message_id)). The
confirmation is always produced, whatever the model's confidence. Message text is data."""

import logging
import re
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
    Evidenced,
    GroupConfig,
    KnownMember,
    Member,
    Participants,
    Share,
    Settlement,
    Subcategory,
)
from splitbot.money import display_amount, parse_amount, split_expense
from splitbot.policy import relevant_approvers, required_approval
from splitbot.state import IllegalTransition
from splitbot.store import DuplicateMessage, StateConflict, Store
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
_log = logging.getLogger(__name__)


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
    settlement_id: int | None = None
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
        correction_prompt_version: str = "correct_v1",
        settlement_prompt_version: str = "settle_v1",
        clock: Callable[[], datetime] | None = None,
    ):
        self._store = store
        self._llm = llm
        self._members = members
        self._config = config
        self._prompt_version = prompt_version
        self._correction_prompt_version = correction_prompt_version
        self._settlement_prompt_version = settlement_prompt_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._names = {m.id: m.name for m in members}

    def _extract_correction(
        self,
        text: str,
        sender_id: int,
        *,
        chat_id: int,
        message_id: int,
        target_expense_id: int,
        mode: str,
    ) -> Extraction:
        """Run correction extraction with enough diagnostics to compare two chat flows.

        This logger is configured separately from the HTTP/root log, so model input/output can be
        inspected without accidentally persisting authorization headers or the Telegram bot URL.
        """
        roster = [{"id": member.id, "name": member.name} for member in self._members]
        _log.debug(
            "correction_extraction start mode=%s chat_id=%s message_id=%s sender_id=%s "
            "target_expense_id=%s prompt_version=%s roster=%r input=%r",
            mode,
            chat_id,
            message_id,
            sender_id,
            target_expense_id,
            self._correction_prompt_version,
            roster,
            text,
        )
        extraction = self._extract(
            text, sender_id, operation="correct", prompt_version=self._correction_prompt_version
        )
        extraction = self._normalize_correction_amount(extraction, text)
        parsed = extraction.expense.model_dump(mode="json") if extraction.expense is not None else None
        _log.debug(
            "correction_extraction result mode=%s chat_id=%s message_id=%s sender_id=%s "
            "target_expense_id=%s status=%s extractor_issues=%r parsed=%r raw=%r",
            mode,
            chat_id,
            message_id,
            sender_id,
            target_expense_id,
            extraction.status,
            extraction.issues,
            parsed,
            extraction.llm_calls[-1].text if extraction.llm_calls else None,
        )
        return extraction

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
        redelivery does not ask twice): extractor status needs_clarification, no amount, an amount
        `money.parse_amount` rejects ("1.200", above the cap),
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
        extraction = self._extract(text, sender_id, operation="create")  # outage propagates; nothing is written
        extraction = self._normalize_create_participants(extraction, text, sender_id)
        issues = self._extraction_issues(extraction)
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
        must be given, exist, belong to `chat_id`, be confirmed and not deleted; the sender must
        be one of the relevant approvers of the target (payer or someone with an owed share).
        Anything else -> "needs_clarification" (no record, message marked processed).

        The correction extractor (`correct_v1`, via `_extract_correction`) runs against a
        code-built restatement of the target's CURRENT fields followed by "תיקון: <text>" (same
        mechanism as `revise_pending`), and the proposed replacement is built the same way
        `_revised_expense` builds one: ANY stored financial field may change (amount, currency,
        payer, participants/exact amounts, subcategory, description), not only the amount;
        whatever the correction does not mention is carried forward unchanged from the target,
        never defaulted. Invalid or incomplete extracted fields, or no field actually changing,
        -> "needs_clarification" (no record). The ChangeRequest (kind correction, requested_by =
        sender, required approvers = requester only) is stored with `Store.create_change_request`;
        status "pending_confirmation" ALWAYS (a group of one included: nobody is approved
        automatically; there is no "applied" status). `confirmation_text` starts with "תיקון" and
        must contain the label, BOTH the old and the new total (display amounts) and "לאשר"; its
        exact layout is not prescribed."""
        return self._propose_change(
            ChangeKind.correction, chat_id, message_id, sender_id, text, target_expense_id
        )

    def propose_delete(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str, target_expense_id: int | None
    ) -> Proposal:
        """Like propose_correction, but without invoking the extractor: the Agent already chose
        the delete tool and the ChangeRequest is kind delete. `confirmation_text` contains the label and total
        (display amount) and "לאשר"; it starts with "מחיקה"."""
        return self._propose_change(ChangeKind.delete, chat_id, message_id, sender_id, text, target_expense_id)

    def propose_settlement(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str
    ) -> Proposal:
        """Propose a repayment reported by either party; affects balances only."""
        expiry = pending_expiry()
        if sender_id not in self._names:
            return Proposal(status="needs_clarification", issues=(_UNKNOWN_SENDER,))
        if self._store.is_processed(chat_id, message_id):
            return Proposal(status="duplicate")
        extraction = self._extract(
            text, sender_id, operation="settle", prompt_version=self._settlement_prompt_version
        )
        _log.debug(
            "settlement_extraction result chat_id=%s message_id=%s sender_id=%s "
            "prompt_version=%s status=%s extractor_issues=%r parsed=%r raw=%r",
            chat_id, message_id, sender_id, self._settlement_prompt_version,
            extraction.status, extraction.issues,
            extraction.expense.model_dump(mode="json") if extraction.expense is not None else None,
            extraction.llm_calls[-1].text if extraction.llm_calls else None,
        )
        issues = self._extraction_issues(extraction)
        e = extraction.expense
        amount = None
        from_user = sender_id
        to_user = None
        if not issues:
            if e.amount is None:
                issues.append("no repayment amount was given")
            else:
                try:
                    amount = parse_amount(e.amount.value)
                except ValueError:
                    issues.append("the repayment amount is ambiguous, unreadable or above the limit")
            if e.payer is not None:
                if not isinstance(e.payer.value, KnownMember):
                    issues.append("it is not clear who sent the repayment")
                else:
                    from_user = e.payer.value.id
            refs = [] if e.participants is None or e.participants.value.only is None else e.participants.value.only
            recipients = [ref.id for ref in refs if isinstance(ref, KnownMember) and ref.id != from_user]
            if len(recipients) != 1 or len(recipients) != len({*recipients}):
                issues.append("it is not clear who received the repayment")
            else:
                to_user = recipients[0]
            if sender_id not in (from_user, to_user):
                issues.append("only one of the two settlement parties may report it")
            currency = e.currency.value if e.currency is not None else Currency.ILS
            balances = self._store.balances(chat_id).get(currency, {})
            if to_user is not None and amount is not None:
                maximum = min(-balances.get(from_user, 0), balances.get(to_user, 0))
                if maximum <= 0:
                    issues.append("there is no matching debt from the sender to that member")
                elif amount > maximum:
                    issues.append("the repayment is larger than the matching open debt")
        if issues:
            _log.debug(
                "settlement_validation rejected chat_id=%s message_id=%s sender_id=%s issues=%r",
                chat_id, message_id, sender_id, issues,
            )
            return self._clarify(chat_id, message_id, issues)
        settlement = Settlement(
            chat_id=chat_id, message_id=message_id, from_user=from_user, to_user=to_user,
            amount=amount, currency=currency, requested_by=sender_id, created_at=self._clock(),
        )
        try:
            stored = self._store.create_settlement(settlement)
        except DuplicateMessage:
            return Proposal(status="duplicate")
        return Proposal(
            status="pending_confirmation", settlement_id=stored.id, approvers=(sender_id,),
            confirmation_text=(
                f"החזר חוב: {self._name(from_user)} שילם/ה ל{self._name(to_user)} "
                f"{display_amount(amount)} {_SYMBOLS[currency]} — לאשר?"
            ),
            expires_at=stored.created_at + expiry,
        )

    def revise_pending(
        self, *, chat_id: int, message_id: int, sender_id: int, text: str, target_expense_id: int | None
    ) -> Proposal:
        """A free-text reply to a PENDING (not yet confirmed) expense that corrects it, instead of
        pressing a button ("זה היה 100 ולא 120", "רק מיכל, זיו ומיכל ביחד"). `target_expense_id`
        must come from a Telegram reply to the pending item (there is no other reliable way to
        name it: a pending expense never appears in `search_expenses`, which only returns
        confirmed ones -- callers must never pass a model-guessed id here). Steps 0 and 1 (roster,
        `pending_expiry()` up front, idempotency by (chat_id, message_id) via
        `Store.is_processed`/`mark_processed`) are the same as `propose_expense`; a replay of an
        already-processed message returns "duplicate" with no LLM call, no write (with the
        target's current confirmation text when it can still be found).

        `target_expense_id is None` -> "needs_clarification" ("it is not clear which pending item
        is meant"), message marked processed, no LLM call. Otherwise the target must exist, belong
        to `chat_id`, still be pending_confirmation and not deleted, and be authored by
        `sender_id` (only the sender who created it may revise it this way) -- anything else ->
        "needs_clarification" with a fixed reason, no LLM call, message marked processed.

        Otherwise: build `_pending_restatement(target)` (a code-built, factual one-line summary of
        the target's OWN current fields: description, total+currency, payer name, participant
        names -- never the original raw message, so a second or third revision always restates the
        LATEST state, not a stale one), append "\\n\\nתיקון: <text>" (the free-text correction,
        verbatim), and run that combined string through `extractor.extract(...)` exactly like
        `propose_expense` (an LLMError propagates: nothing is marked processed). This is NOT a
        sparse per-field merge: an isolated correction fragment alone (no amount, no category) is
        NOT sent to the extractor by itself because it contains too few storage fields -- restating the current
        state alongside the correction is what lets the extractor correctly carry forward every
        field the correction does not mention, while still updating whichever ones it does.

        Invalid or incomplete extracted storage fields cause "needs_clarification", with the
        message marked processed and the pending record UNTOUCHED (same
        content, same version, same expiry). Otherwise `_revised_expense(target, extraction, text,
        issues)` builds a full replacement the same way `_build_expense` does (amount, currency,
        payer, participants/exact_amounts, subcategory, description, words), keeping `target`'s
        id/chat_id/message_id/author_id/spent_on; any of `_build_expense`'s usual construction
        issues (no amount, unreadable amount, unsupported/missing currency, unclear payer, who
        shares could not be worked out) apply identically here and leave the pending record
        untouched (message still marked processed).

        The replacement is saved with `Store.revise_pending_expense(target_expense_id, replacement,
        now=clock())`: a `StateConflict` (someone else revised or confirmed it in the meantime) or
        `IllegalTransition` (it expired, or was confirmed/rejected, before this revision landed)
        both become "needs_clarification" (a fixed reason naming the race/expiry), message marked
        processed, nothing written -- this is a real, if rare, race and is never silently retried.
        `mode` (author/auto) is RECOMPUTED from the revised total/category/currency/confidence
        (a revision can cross a policy threshold the original did not). `expires_at` is the
        revised expense's `created_at` (which the store resets to `now`) + `pending_expiry()`: a
        revision always gets a fresh hour, since the sender is actively engaging with it right
        now. `confirmation_text` is `self._confirmation(revised)`, the exact same template
        `propose_expense` uses -- never the model's own wording."""
        expiry = pending_expiry()  # a bad PENDING_EXPIRY_HOURS raises before anything is written
        if sender_id not in self._names:
            return Proposal(status="needs_clarification", issues=(_UNKNOWN_SENDER,))
        existing = self._replay(chat_id, message_id, sender_id)
        if existing:
            return existing
        if target_expense_id is None:
            return self._clarify(chat_id, message_id, ["it is not clear which pending item is meant"])
        try:
            target = self._store.get_expense(target_expense_id)
        except KeyError:
            return self._clarify(chat_id, message_id, ["that pending item does not exist"])
        if target.chat_id != chat_id or target.state != ExpenseState.pending_confirmation or target.deleted:
            return self._clarify(chat_id, message_id, ["that item is no longer pending"])
        if target.author_id != sender_id:
            return self._clarify(chat_id, message_id, ["only the sender who created it can revise it this way"])

        extraction_text = f"{self._pending_restatement(target)}\n\nתיקון: {text}"
        extraction = self._extract_correction(
            extraction_text,
            sender_id,
            chat_id=chat_id,
            message_id=message_id,
            target_expense_id=target_expense_id,
            mode="pending",
        )
        issues = self._extraction_issues(extraction)
        if issues:
            _log.debug("revise_pending: rejected after _extraction_issues: %s", issues)
            return self._clarify(chat_id, message_id, issues)
        built = self._revised_expense(target, extraction, text, issues)  # may append to `issues` in place
        if issues:
            _log.debug("revise_pending: rejected inside _revised_expense: %s", issues)
            return self._clarify(chat_id, message_id, issues)
        replacement, confidence = built
        try:
            revised = self._store.revise_pending_expense(target_expense_id, replacement, now=self._clock())
        except (StateConflict, IllegalTransition) as exc:
            _log.debug("revise_pending: store rejected the revision: %s", exc)
            return self._clarify(chat_id, message_id, ["that item changed before this correction could be applied"])
        _log.debug("revise_pending: SUCCESS, new total=%s", revised.total)
        mode = required_approval(
            self._config,
            category=revised.category,
            total=revised.total,
            currency=revised.currency,
            confidence=confidence,
            amount_in_words=revised.words is not None,
        )
        self._store.mark_processed(chat_id, message_id)
        return Proposal(
            status="pending_confirmation",
            expense_id=revised.id,
            approvers=(sender_id,),
            mode=mode.value,
            confirmation_text=self._confirmation(revised),
            expires_at=revised.created_at + expiry,
        )

    def confirm_expense(self, expense_id: int, user_id: int, approve: bool) -> Expense:
        """The sender's answer to a new expense: `Store.respond_expense` with `now` = the clock."""
        return self._store.respond_expense(expense_id, user_id, approve, now=self._clock())

    def respond_change(self, request_id: int, user_id: int, approve: bool) -> ChangeRequest:
        """A relevant person's answer to a correction/delete: `Store.respond` with `now` = the clock."""
        return self._store.respond(request_id, user_id, approve, now=self._clock())

    # --- internals ------------------------------------------------------------

    def _extract(
        self, text: str, sender_id: int, *, operation: str = "extract", prompt_version: str | None = None
    ) -> Extraction:
        return extract(
            text, sender_id=sender_id, members=self._members, llm=self._llm,
            prompt_version=prompt_version or self._prompt_version, operation=operation,
        )

    @staticmethod
    def _normalize_correction_amount(extraction: Extraction, text: str) -> Extraction:
        """Repair only explicit amount phrases after the correction target is already known."""
        expense = extraction.expense
        if expense is None or expense.amount is not None:
            return extraction
        correction = text.rsplit("תיקון:", 1)[-1]
        match = re.search(
            r"(?:זה\s+(?:היה|עלה)|ה?סכום(?:\s+(?:הוא|היה))?|(?:זה\s+)?עלה)\s*"
            r"(\d+(?:[.,]\d{1,2})?)",
            correction,
        )
        if match is None:
            return extraction
        evidence = match.group(1)
        patched = expense.model_copy(update={
            "amount": Evidenced(value=evidence, evidence=evidence, source="message"),
            "amount_in_words": False,
        })
        return Extraction(
            extraction.status, patched, extraction.issues, extraction.prompt_version, extraction.llm_calls,
            extraction.legacy_message_type,
        )

    def _normalize_create_participants(
        self, extraction: Extraction, text: str, sender_id: int
    ) -> Extraction:
        """Apply a few unambiguous participant phrases in code after LLM extraction.

        This is intentionally narrow: it protects the product's two core invariants without
        attempting to replace language understanding. Unspecified/all-household means everyone;
        an explicit possessive/only phrase naming one unique member means exactly that member.
        """
        expense = extraction.expense
        if expense is None:
            return extraction
        normalized = normalize(text)
        if any(phrase in normalized for phrase in ("כל הבית", "לכולנו", "כולנו", "כולם", "כל חברי הבית")):
            participants = Evidenced(value=Participants(), evidence=None, source="default")
            return Extraction(
                extraction.status, expense.model_copy(update={"participants": participants}),
                extraction.issues, extraction.prompt_version, extraction.llm_calls,
                extraction.legacy_message_type,
            )

        # Telegram display names often contain contact decorations ("❤️ ירדן"). Match both
        # the full normalized name and clean word aliases, but only when an alias is unique in
        # this group so two members named "דני" are never guessed apart.
        aliases_by_member: dict[int, set[str]] = {}
        owners: dict[str, set[int]] = {}
        for member in self._members:
            full = normalize(member.name)
            words = re.findall(r"[^\W_]+", full, flags=re.UNICODE)
            aliases = {full, " ".join(words), *words} - {""}
            aliases_by_member[member.id] = aliases
            for alias in aliases:
                owners.setdefault(alias, set()).add(member.id)

        unique_aliases = {
            member.id: {alias for alias in aliases_by_member[member.id] if owners[alias] == {member.id}}
            for member in self._members
        }

        exact_matches: list[tuple[Member, str]] = []
        with_matches: list[tuple[Member, str]] = []
        sender_and_matches: list[tuple[Member, str]] = []
        for member in self._members:
            for alias in sorted(unique_aliases[member.id], key=len, reverse=True):
                escaped = re.escape(alias)
                if any(re.search(pattern, normalized) for pattern in (
                    rf"(?:^|\s)של\s+{escaped}(?:\s|$)",
                    rf"(?:^|\s)רק\s+(?:של\s+)?{escaped}(?:\s|$)",
                    rf"(?:^|\s)עבור\s+{escaped}\s+בלבד(?:\s|$)",
                )):
                    exact_matches.append((member, alias))
                    break
                if re.search(rf"(?:^|\s)עם\s+{escaped}(?:\s|$)", normalized):
                    with_matches.append((member, alias))
                    break
                # Natural Hebrew such as "לי ולדני" means the sender plus Dani. The leading
                # conjunction is attached to the member name, so it is deliberately handled
                # separately from the more general alias matching below.
                if re.search(rf"(?:^|\s)לי\s+ול{escaped}(?!\w)", normalized):
                    sender_and_matches.append((member, alias))
                    break

        if with_matches:
            # Do not collapse a list such as "עם דני ומיכל" to its first name. In multi-name
            # phrases the extractor's complete set is kept; this guard only repairs one-name
            # phrases such as "עם ירדן".
            mentioned_ids = {
                member.id
                for member in self._members
                for alias in unique_aliases[member.id]
                if re.search(rf"(?:^|\s|ו){re.escape(alias)}(?:\s|$)", normalized)
            }
            if len(mentioned_ids) != 1:
                with_matches = []

        matches = exact_matches or with_matches or sender_and_matches
        if len(matches) == 1:
            member, evidence = matches[0]
            participant_ids = [member.id]
            if with_matches or sender_and_matches:
                # "עם ירדן" / "לי ולירדן" means sender + Yarden, regardless of who paid.
                participant_ids.append(sender_id)
            participant_ids = [member_id for member_id in dict.fromkeys(participant_ids) if member_id is not None]
            participants = Evidenced(
                value=Participants(only=[KnownMember(id=member_id) for member_id in participant_ids]),
                evidence=evidence,
                source="message",
            )
            return Extraction(
                extraction.status, expense.model_copy(update={"participants": participants}),
                extraction.issues, extraction.prompt_version, extraction.llm_calls,
                extraction.legacy_message_type,
            )
        return extraction

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
    def _extraction_issues(extraction: Extraction) -> list[str]:
        if extraction.status != "ok" or extraction.expense is None:
            return list(extraction.issues) or ["the message could not be understood"]
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
        if e.payer is not None and e.payer.source == "message":
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

    def _pending_restatement(self, expense: Expense) -> str:
        """A code-built, factual one-line summary of `expense`'s CURRENT fields, used ONLY as
        context for the extractor call inside `revise_pending` (never shown to a person):
        "הוצאה ממתינה לאישור: <description>, <total> <currency code>, שילם/ה <payer name>,
        משתתפים: <participant names, roster order>." <participant names> lists everyone with an
        owed share > 0; <payer name> is whoever has a paid share > 0 (the sole payer; a revision
        only ever targets a single-payer pending expense, same as `_build_expense`'s output)."""
        payer_id = next(s.user_id for s in expense.shares if s.paid > 0)
        participants = [s.user_id for s in expense.shares if s.owed > 0]
        names = ", ".join(self._name(uid) for uid in participants)
        return (
            f"הוצאה ממתינה לאישור: {expense.description}, {display_amount(expense.total)} "
            f"{expense.currency.value}, שילם/ה {self._name(payer_id)}, משתתפים: {names}."
        )

    def _revised_expense(self, target: Expense, extraction: Extraction, text: str, issues: list[str]):
        """The full replacement Expense for `target`, or None with a fixed reason appended to
        `issues` (mirrors `_build_expense`'s construction and its issue text exactly, applied to
        the extractor's fields, but keeps `target`'s id/chat_id/message_id/author_id/spent_on
        instead of `propose_expense`'s sender/spent_on-param logic). `text` is the COMBINED
        restatement + correction string `revise_pending` built and extracted from (not the bare
        correction fragment): the description/foreign-currency checks below need the description
        to be findable in whatever the extractor actually saw, which is the restatement, not
        necessarily the free-text correction alone. A field the extraction leaves unstated
        (subcategory, description) falls back to `target`'s own current value, not a generic
        default: a revision already has a real prior value worth keeping. Returns (expense,
        confidence) on success, matching `_build_expense`'s return shape."""
        e = extraction.expense
        total = target.total
        if e.amount is not None:
            try:
                total = parse_amount(e.amount.value)
            except ValueError:
                issues.append("the amount is ambiguous, unreadable or above the limit")
                return None
        if e.currency is None and mentions_foreign_currency(text):
            issues.append("the currency is missing or not supported")
            return None
        currency = e.currency.value if e.currency is not None else target.currency
        original_payers = [s.user_id for s in target.shares if s.paid > 0]
        payer_id = original_payers[0] if len(original_payers) == 1 else None
        if e.payer is not None and e.payer.source == "message":
            if not isinstance(e.payer.value, KnownMember):
                issues.append("it is not clear who paid")
                return None
            payer_id = e.payer.value.id
        original_participants = [s.user_id for s in target.shares if s.owed > 0]
        changing_money_shape = (
            total != target.total
            or (e.payer is not None and e.payer.source == "message")
            or e.participants is not None
            or bool(e.exact_amounts)
        )
        if not changing_money_shape:
            shares = target.shares
        elif payer_id is None:
            issues.append("changing an expense with several payers needs one explicit payer")
            return None
        else:
            # If an unequal split's total changes, carrying the old owed amounts forward is
            # impossible. The user must provide the new exact amounts.
            if total != target.total and e.exact_amounts is None and e.participants is None:
                equal_old = split_expense(
                    target.total, payer_id=payer_id, author_id=target.author_id,
                    participants=original_participants,
                )
                if {s.user_id: s.owed for s in equal_old} != {s.user_id: s.owed for s in target.shares}:
                    issues.append("the original split was unequal: give the new exact amounts")
                    return None
            try:
                if e.exact_amounts:
                    exact = {}
                    for item in e.exact_amounts:
                        if not isinstance(item.member, KnownMember) or item.member.id in exact:
                            raise ValueError("exact amounts need each member once")
                        exact[item.member.id] = parse_amount(item.amount)
                    shares = split_expense(total, payer_id=payer_id, author_id=target.author_id, participants=[], exact=exact)
                else:
                    participants = (
                        resolve_participants(e.participants.value, target.author_id, [m.id for m in self._members])
                        if e.participants is not None else original_participants
                    )
                    shares = split_expense(
                        total, payer_id=payer_id, author_id=target.author_id, participants=participants
                    )
            except ValueError:
                issues.append("who shares the expense, or the amounts per person, could not be worked out")
                return None
        subcategory = e.subcategory or target.subcategory
        described = _clean(e.description or "")
        label = described if described and normalize(described) in normalize(_clean(text, cap=len(text))) else target.description
        if (
            total == target.total
            and currency == target.currency
            and shares == target.shares
            and subcategory == target.subcategory
            and label == target.description
        ):
            issues.append("no expense field was changed")
            return None
        expense = Expense(
            id=target.id,
            chat_id=target.chat_id,
            message_id=target.message_id,
            author_id=target.author_id,
            description=label,
            total=total,
            currency=currency,
            subcategory=subcategory,
            shares=shares,
            prompt_version=self._correction_prompt_version,
            spent_on=target.spent_on,
            state=ExpenseState.pending_confirmation,
            version=target.version,
            words=(
                _clean(e.amount.evidence, cap=len(e.amount.evidence))
                if e.amount is not None and e.amount_in_words
                else (target.words if e.amount is None else None)
            ),
        )
        return expense, e.confidence

    def _propose_change(self, kind, chat_id, message_id, sender_id, text, target_expense_id) -> Proposal:
        expiry = pending_expiry()  # a bad PENDING_EXPIRY_HOURS raises before anything is written
        if sender_id not in self._names:
            return Proposal(status="needs_clarification", issues=(_UNKNOWN_SENDER,))
        existing = self._replay(chat_id, message_id, sender_id)
        if existing:
            return Proposal(status="duplicate")
        issues: list[str] = []
        original = self._target(target_expense_id, chat_id, sender_id, issues)
        extraction = None
        proposed = None
        if original is not None and kind == ChangeKind.correction:
            extraction_text = self._pending_restatement(original) + "\n\nתיקון: " + text
            extraction = self._extract_correction(
                extraction_text,
                sender_id,
                chat_id=chat_id,
                message_id=message_id,
                target_expense_id=original.id,
                mode="confirmed",
            )
            issues.extend(self._extraction_issues(extraction))
            if not issues:
                revised = self._revised_expense(original, extraction, text, issues)
                proposed = revised[0] if revised is not None else None
        if issues:
            if kind == ChangeKind.correction:
                _log.debug(
                    "correction_extraction rejected chat_id=%s message_id=%s sender_id=%s "
                    "target_expense_id=%s issues=%r",
                    chat_id,
                    message_id,
                    sender_id,
                    target_expense_id,
                    issues,
                )
            return self._clarify(chat_id, message_id, issues)
        required = [sender_id]
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
