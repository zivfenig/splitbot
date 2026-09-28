"""Telegram wiring: routing, workflow, confirmation buttons. Run with `python -m
splitbot.bot.telegram_bot` (polling; no public URL/webhook needed).

`BotLogic` is the whole routing/decision layer and has NO dependency on the `telegram` package's
own types anywhere in its method signatures: every incoming fact (chat id, sender id and display
name, message id, text, whether the message @-mentions the bot, which Telegram message id it
replies to, if any) is a plain value the caller already extracted from a real `telegram.Update`.
This is what makes it fully unit-testable with fakes, the same way `Agent`/`WriteTools` already
are, with no network. The actual `python-telegram-bot` `Application`/handlers (below `BotLogic` in
this same file, to keep the "one file" layout in CLAUDE.md) are a thin adapter: they parse a real
`Update` into these plain calls, send the resulting message(s)/buttons through the Bot API, and --
for any `OutgoingMessage` whose `record_as` is set -- call `Store.record_bot_message` with the REAL
message id Telegram just assigned (an id `BotLogic` itself never sees, since it does not exist
until after the message is actually sent). Local adapter-to-SQLite flows are covered in
`tests/e2e/test_telegram_workflow.py`; `tests/e2e/scenarios.md` remains the real-network smoke test.

The roster is built automatically, never from a pre-filled config file: `Store.upsert_member` is
called for the sender of every incoming message, and `Store.get_members` is what `Agent`/
`WriteTools`/`GroupConfig` see as "the roster" -- always the group's CURRENT membership, not a
fixed list decided at startup. A group just adds the bot and starts using it; nobody edits a
member list by hand.
"""

import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from splitbot.agent.agent import Agent
from splitbot.config import optional, pending_expiry, router_ignore_threshold
from splitbot.llm.client import ChatLLM, LLMClient
from splitbot.models import ApprovalMode, ChangeKind, Currency, GroupConfig, Member
from splitbot.money import display_amount
from splitbot.router.base import Router, decide
from splitbot.state import IllegalTransition
from splitbot.store import NotRelevantApprover, StateConflict, Store
from splitbot.tools.write_tools import WriteTools

_CALLBACK_ERROR = "לא ניתן לעדכן את הבקשה הזו (אולי כבר נענתה, פגה, או שאת/ה לא מהאנשים הרלוונטיים)."
_log = logging.getLogger(__name__)
_CURRENCY_LABELS = {Currency.ILS: "שקל חדש (₪)", Currency.USD: "דולר ($)", Currency.EUR: "אירו (€)"}
_CATEGORY_LABELS = {
    "utilities": "חשבונות", "rent": "שכירות", "arnona": "ארנונה", "groceries": "סופר",
    "household": "ציוד לבית", "eating_out": "אוכל בחוץ", "other": "אחר",
}
_SUBCATEGORY_LABELS = {
    "electricity": "חשמל", "gas": "גז", "water": "מים", "internet": "אינטרנט",
    "rent": "שכירות", "arnona": "ארנונה", "groceries": "קניות בסופר", "cleaning": "ניקיון",
    "supplies": "ציוד לבית", "restaurant": "מסעדה/בית קפה", "delivery": "משלוח", "other": "אחר",
}


def _configure_correction_debug_log(path: str | None = None) -> RotatingFileHandler:
    """Write correction diagnostics to a bounded file, isolated from HTTP/token logs."""
    destination = Path(path or optional("DEBUG_LOG_PATH", "splitbot-debug.log")).expanduser().resolve()
    logger_names = (
        "splitbot.bot.telegram_bot", "splitbot.tools.write_tools", "splitbot.agent.agent",
    )
    diagnostic_handlers: list[RotatingFileHandler] = []
    for logger_name in logger_names:
        logger = logging.getLogger(logger_name)
        for existing in list(logger.handlers):
            if getattr(existing, "_splitbot_correction_debug", False):
                diagnostic_handlers.append(existing)

    handler = next(
        (existing for existing in diagnostic_handlers if Path(existing.baseFilename) == destination),
        None,
    )
    if handler is None:
        handler = RotatingFileHandler(
            destination,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        handler._splitbot_correction_debug = True  # type: ignore[attr-defined]
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    for logger_name in logger_names:
        logger = logging.getLogger(logger_name)
        for existing in list(logger.handlers):
            if getattr(existing, "_splitbot_correction_debug", False) and existing is not handler:
                logger.removeHandler(existing)
        if handler not in logger.handlers:
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    for existing in set(diagnostic_handlers):
        if existing is not handler:
            existing.close()
    return handler


def _safe_display(text: str, cap: int = 160) -> str:
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        kept.append(" " if category in ("Cc", "Zl", "Zp") else char)
    return " ".join("".join(kept).split())[:cap].strip()


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    buttons: tuple[tuple[str, str], ...] = ()  # (label, callback_data) pairs; () = no buttons
    record_as: tuple[str, int] | None = None  # (kind, target_id) for Store.record_bot_message,
    edit_original: bool = True  # callback result: False keeps the confirmation and its buttons intact
    alert: bool = False  # callback result: show text as a private Telegram alert
    # once the adapter has actually sent this message and knows its real Telegram message id


class BotLogic:
    """`store` is the one shared, long-lived Store. `chat_llm`/`write_llm` are the real
    (or, in tests, fake) LLM clients, built ONCE and reused across every call -- the roster CAN
    change between calls, but the clients themselves do not need to. `router` classifies a plain,
    non-mention, non-reply message (ignore / query / expense; only ignore-vs-pass is thresholded,
        via `config.router_ignore_threshold()` unless `threshold` is given). A passed message goes
        directly to the agent; the router never asks the user for a second confirmation.
        `default_mode` is the
    `GroupConfig` every chat gets (there is no per-chat config file to read one from; "author" is
    the safe default -- see CLAUDE.md's approval-mode rules). `clock` defaults to now in UTC, same
    convention as `Agent`/`WriteTools`."""

    def __init__(
        self,
        store: Store,
        chat_llm: ChatLLM,
        write_llm: LLMClient,
        router: Router,
        *,
        default_mode: ApprovalMode = ApprovalMode.author,
        agent_prompt_version: str = "agent_v4",
        extractor_prompt_version: str = "extract_v3",
        threshold: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._store = store
        self._chat_llm = chat_llm
        self._write_llm = write_llm
        self._router = router
        self._default_mode = default_mode
        self._agent_prompt_version = agent_prompt_version
        self._extractor_prompt_version = extractor_prompt_version
        self._threshold = threshold if threshold is not None else router_ignore_threshold()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def handle_message(
        self,
        *,
        chat_id: int,
        sender_id: int,
        sender_name: str,
        message_id: int,
        text: str,
        is_mention: bool,
        reply_to_telegram_message_id: int | None,
    ) -> list[OutgoingMessage]:
        """One incoming group message -> zero or more messages to post.

        FIRST, always, regardless of what follows: `store.upsert_member(chat_id, sender_id,
        sender_name)` -- the sender is part of the roster from their very first message on,
        before anything else in this call reads it. Then `recent = store.recent_messages(chat_id,
        limit=8)` is read (STRICTLY BEFORE this message is logged, so it never includes itself),
        and `store.log_message(chat_id, sender_name, text)` logs this message for future turns'
        history -- both happen regardless of routing outcome too, same as the registration.

        Routing: `is_mention` or `reply_to_telegram_message_id is not None` -> ALWAYS the agent
        (CLAUDE.md's "@ / reply -> agent" rule; a reply always reaches the agent even if its text
        alone would look ignorable). Otherwise `router.route(text)` + `router.base.decide(result,
        threshold)`: "ignore" -> returns `[]` immediately (no agent call, nothing posted, exactly
        Stage C's point: an ignored message costs nothing beyond the router call itself, and the
        registration/history logging above still happened); "pass" -> the unchanged Agent path
        immediately. Human confirmation is still mandatory for every proposed ledger write.

        Reply target resolution: `reply_to_telegram_message_id` is looked up with
        `Store.get_bot_message(chat_id, that id)`. `None` there (not a message this bot ever
        recorded) falls back to `Store.get_expense_by_message(chat_id, that id)` (a reply to the
        user's OWN original report, not the bot's confirmation). `("expense", target_id)` from
        `get_bot_message` -> `target_id` directly. `("change_request", target_id)` -> the change
        request's OWN `expense_id` (a request that no longer exists falls back to the next step
        rather than raising). If NONE of this resolves anything (including when there was no
        reply at all): the sender's most recent unexpired pending expense is the conversational
        fallback, unless a recent numbered search selection is active. This lets an
        ordinary follow-up ("actually it was 25") work even when Telegram's own reply link never
        got attached (confirmed in practice less reliable than expected -- see the session this
        was added for). It never widens WHO may act: `revise_pending`'s own author-only check
        still applies, and a target found this way is still just a candidate the tools validate.

        Building the agent for THIS call: `members = store.get_members(chat_id)` (the roster as of
        right now, sender included, per the registration above), `config = GroupConfig(chat_id,
        members, default_mode)`, `write_tools = WriteTools(store, write_llm, members, config,
        prompt_version=extractor_prompt_version, clock=clock)`, `agent = Agent(chat_llm, store,
        members, write_tools, prompt_version=agent_prompt_version, clock=clock)` -- a fresh, cheap
        set of wrapper objects per call (no expensive setup in any of their constructors), never
        reused across calls, so a roster that grew since the last message is always reflected.
        Then `agent.run_turn(chat_id=chat_id, sender_id=sender_id, message_id=message_id,
        text=text, reply_target_expense_id=<resolved above>, recent_messages=recent,
        prior_search_expense_ids=<the sender's short-lived search selection>)`.

        Building the reply: if any of `AgentReply.proposals` has `status ==
        "pending_confirmation"` (in practice there is at most one per turn: `run_turn` stops the
        loop as soon as one appears), the single returned `OutgoingMessage` has `text =
        the detailed code-built Telegram confirmation, `buttons = (("✅ אישור",
        "<kind>:<id>:yes"), ("❌ ביטול", "<kind>:<id>:no"))`, and `record_as = (kind, id)`, where `kind`
        is "expense" and `id = proposal.expense_id` when `proposal.change_request_id is None`,
        else `kind` is "change_request" and `id = proposal.change_request_id`. Otherwise (a plain
        answer, an ask, or the safe fallback) the single returned `OutgoingMessage` is just
        `text = reply.text`, no buttons, `record_as = None`. Either way, the text actually sent is
        logged via `store.log_message(chat_id, "bot", text)` before returning, so the next
        turn's history includes the bot's own side of the conversation. Never returns more than
        one `OutgoingMessage` in the agent-routed path (only the "ignore" path can return `[]`,
        which logs nothing further: there is no bot reply to log)."""
        self._store.upsert_member(chat_id, sender_id, sender_name)
        self._store.expire_stale(self._clock(), pending_expiry(), chat_id=chat_id)
        recent = self._store.recent_messages(chat_id, limit=8)
        prior_search_ids = self._store.recent_search_results(chat_id, sender_id, now=self._clock())
        self._store.log_message(chat_id, sender_name, text)

        edit_target = self._store.pending_edit_target(chat_id, sender_id, now=self._clock())
        _log.debug(
            "correction_flow lookup chat_id=%s message_id=%s sender_id=%s pending_edit_target=%s "
            "reply_to_telegram_message_id=%s",
            chat_id,
            message_id,
            sender_id,
            edit_target,
            reply_to_telegram_message_id,
        )
        if edit_target is not None:
            members = self._store.get_members(chat_id)
            config = GroupConfig(chat_id=chat_id, members=members, default_mode=self._default_mode)
            write_tools = WriteTools(
                self._store, self._write_llm, members, config,
                prompt_version=self._extractor_prompt_version, clock=self._clock,
            )
            target = self._store.get_expense(edit_target)
            if target.state.value == "pending_confirmation":
                proposal = write_tools.revise_pending(
                    chat_id=chat_id, message_id=message_id, sender_id=sender_id,
                    text=text, target_expense_id=edit_target,
                )
            else:
                proposal = write_tools.propose_correction(
                    chat_id=chat_id, message_id=message_id, sender_id=sender_id,
                    text=text, target_expense_id=edit_target,
                )
            _log.debug(
                "correction_flow proposal chat_id=%s message_id=%s sender_id=%s target_expense_id=%s "
                "target_state=%s status=%s issues=%r",
                chat_id,
                message_id,
                sender_id,
                edit_target,
                target.state.value,
                proposal.status,
                proposal.issues,
            )
            if proposal.status == "pending_confirmation":
                self._store.clear_pending_edit(chat_id, sender_id)
                message = self._outgoing_for_proposal(proposal, text, members)
                self._store.log_message(chat_id, "bot", message.text)
                return [message]
            response = "לא הצלחתי להבין את התיקון. אפשר לנסח שוב אילו פרטים לשנות."
            self._store.log_message(chat_id, "bot", response)
            return [OutgoingMessage(text=response)]

        # An edit target (checked above) takes priority in the unlikely case a sender somehow
        # has both open at once; each table is independent, so this never actually collides in
        # normal use (each button press only ever sets its own kind of target).
        delete_target = self._store.pending_delete_target(chat_id, sender_id, now=self._clock())
        _log.debug(
            "delete_flow lookup chat_id=%s message_id=%s sender_id=%s pending_delete_target=%s",
            chat_id, message_id, sender_id, delete_target,
        )
        if delete_target is not None:
            members = self._store.get_members(chat_id)
            config = GroupConfig(chat_id=chat_id, members=members, default_mode=self._default_mode)
            write_tools = WriteTools(
                self._store, self._write_llm, members, config,
                prompt_version=self._extractor_prompt_version, clock=self._clock,
            )
            proposal = write_tools.propose_delete(
                chat_id=chat_id, message_id=message_id, sender_id=sender_id,
                text=text, target_expense_id=delete_target,
            )
            _log.debug(
                "delete_flow proposal chat_id=%s message_id=%s sender_id=%s target_expense_id=%s "
                "status=%s issues=%r",
                chat_id, message_id, sender_id, delete_target, proposal.status, proposal.issues,
            )
            # Unlike the edit flow, this always clears on the first attempt, success or not:
            # `propose_delete` extracts nothing from `text` (deletion needs no fields), so a
            # failure here (not confirmed, already deleted, not a relevant approver) is never
            # something retyping the message differently would fix -- leaving the target open
            # would only risk every subsequent message repeating the same failed delete attempt.
            self._store.clear_pending_delete(chat_id, sender_id)
            if proposal.status == "pending_confirmation":
                message = self._outgoing_for_proposal(proposal, text, members)
                self._store.log_message(chat_id, "bot", message.text)
                return [message]
            response = "לא ניתן למחוק את ההוצאה הזו כרגע. אפשר לנסות שוב, או לחפש אותה מחדש."
            self._store.log_message(chat_id, "bot", response)
            return [OutgoingMessage(text=response)]

        if not is_mention and reply_to_telegram_message_id is None:
            result = self._router.route(text)
            if decide(result, self._threshold) == "ignore":
                return []

        return self._run_agent_path(
            chat_id=chat_id, sender_id=sender_id, message_id=message_id, text=text,
            reply_to_telegram_message_id=reply_to_telegram_message_id,
            recent=recent, prior_search_ids=prior_search_ids,
        )

    def _run_agent_path(
        self, *, chat_id: int, sender_id: int, message_id: int, text: str,
        reply_to_telegram_message_id: int | None, recent: list[str], prior_search_ids: list[int],
    ) -> list[OutgoingMessage]:
        """The post-router Agent workflow."""

        reply_target = self._resolve_reply_target(chat_id, reply_to_telegram_message_id)
        _log.debug("handle_message: prior_search_ids=%s (from Store.recent_search_results)", prior_search_ids)
        if reply_target is None and not prior_search_ids:
            reply_target = self._store.latest_pending_expense_for(
                chat_id, sender_id, now=self._clock(), expiry=pending_expiry()
            )
            _log.debug(
                "handle_message: no explicit reply target, latest_pending_expense_for(chat_id=%s, sender_id=%s) -> %s",
                chat_id, sender_id, reply_target,
            )
        _log.debug(
            "handle_message: FINAL reply_target_expense_id=%s prior_search_expense_ids=%s passed to agent.run_turn",
            reply_target, prior_search_ids,
        )
        members = self._store.get_members(chat_id)
        config = GroupConfig(chat_id=chat_id, members=members, default_mode=self._default_mode)
        write_tools = WriteTools(
            self._store, self._write_llm, members, config,
            prompt_version=self._extractor_prompt_version, clock=self._clock,
        )
        agent = Agent(
            self._chat_llm, self._store, members, write_tools,
            prompt_version=self._agent_prompt_version, clock=self._clock,
        )
        reply = agent.run_turn(
            chat_id=chat_id, sender_id=sender_id, message_id=message_id, text=text,
            reply_target_expense_id=reply_target, recent_messages=recent,
            prior_search_expense_ids=prior_search_ids,
        )
        for trace in reply.tool_calls:
            if trace.name == "search_expenses" and "expenses" in trace.result:
                self._store.remember_search_results(
                    chat_id, sender_id, [item["id"] for item in trace.result["expenses"]], now=self._clock()
                )
        pending = next((p for p in reply.proposals if p.status == "pending_confirmation"), None)
        if pending is None:
            response_text = self._format_search_results(reply) or reply.text
            self._store.log_message(chat_id, "bot", response_text)
            search_buttons: tuple[tuple[str, str], ...] = ()
            if reply.tool_calls and all(trace.name == "search_expenses" for trace in reply.tool_calls):
                expenses = reply.tool_calls[-1].result.get("expenses", [])
                search_buttons = tuple(
                    button
                    for index, expense in enumerate(expenses, start=1)
                    for button in ((f"✏️ {index}", f"expense:{expense['id']}:edit"),
                                   (f"🗑️ {index}", f"expense:{expense['id']}:delete"))
                )
            return [OutgoingMessage(text=response_text, buttons=search_buttons)]
        self._store.remember_search_results(chat_id, sender_id, [], now=self._clock())
        message = self._outgoing_for_proposal(pending, text, members)
        self._store.log_message(chat_id, "bot", message.text)
        return [message]

    def _outgoing_for_proposal(self, pending, source_text: str, members: list[Member]) -> OutgoingMessage:
        """Render one validated pending proposal and attach deterministic callback buttons."""
        if pending.settlement_id is not None:
            kind, target_id = "settlement", pending.settlement_id
        elif pending.change_request_id is None:
            kind, target_id = "expense", pending.expense_id
        else:
            kind, target_id = "change_request", pending.change_request_id
        buttons = (("✅ אישור", f"{kind}:{target_id}:yes"), ("❌ ביטול", f"{kind}:{target_id}:no"))
        if kind == "expense":
            buttons += (("✏️ תיקון", f"expense:{target_id}:edit"), ("🗑️ מחיקה", f"expense:{target_id}:delete"))
        confirmation = self._format_confirmation(pending, source_text, members)
        return OutgoingMessage(text=confirmation, buttons=buttons, record_as=(kind, target_id))

    @staticmethod
    def _format_search_results(reply) -> str | None:
        """Render a pure expense search deterministically so its selection numbers are reliable."""
        if not reply.tool_calls or any(trace.name != "search_expenses" for trace in reply.tool_calls):
            return None
        expenses = reply.tool_calls[-1].result.get("expenses")
        if expenses is None:
            return None
        if not expenses:
            return "לא מצאתי הוצאות מתאימות."
        symbols = {"ILS": "₪", "USD": "$", "EUR": "€"}
        lines = ["🔎 מצאתי את ההוצאות הבאות:"]
        for index, expense in enumerate(expenses, start=1):
            symbol = symbols.get(expense["currency"], expense["currency"])
            payers = ", ".join(_safe_display(name, 60) for name in expense["payers"])
            participants = [share for share in expense["shares"] if share["owed"] != "0"]
            participant_names = ", ".join(_safe_display(share["name"], 60) for share in participants)
            split = "\n".join(
                f"   • {_safe_display(share['name'], 60)}: {symbol}{share['owed']}"
                for share in participants
            )
            lines.append(
                f"\n{index}. {_safe_display(expense['description'], 60)}\n"
                f"סכום: {symbol}{expense['total']}\n"
                f"תאריך: {expense['spent_on']}\n"
                f"שילם/ה: {payers}\n"
                f"משתתפים: {participant_names}\n"
                f"חלוקה:\n{split}"
            )
        lines.append("אפשר לכתוב למשל: „תתקן את 2” או „תמחק את 1”.")
        return "\n".join(lines)

    def _format_confirmation(self, proposal, source_text: str, members: list[Member]) -> str:
        """A Telegram-friendly, code-built view of exactly what the pending record contains."""
        names = {member.id: _safe_display(member.name, 60) for member in members}
        source = _safe_display(source_text)
        if proposal.settlement_id is not None:
            settlement = self._store.get_settlement(proposal.settlement_id)
            return (
                "💸 האם לרשום החזר חוב?\n\n"
                f"מאת: {names.get(settlement.from_user, str(settlement.from_user))}\n"
                f"אל: {names.get(settlement.to_user, str(settlement.to_user))}\n"
                f"סכום: {display_amount(settlement.amount)} {_CURRENCY_LABELS[settlement.currency]}\n\n"
                f"הודעת המקור: \"{source}\""
            )
        if proposal.change_request_id is not None:
            request = self._store.get_change_request(proposal.change_request_id)
            original = self._store.get_expense(request.expense_id)
            if request.kind == ChangeKind.delete:
                details = self._expense_details(original, names)
                return (
                    "🗑️ האם למחוק את ההוצאה?\n\n"
                    f"{details}\n\n"
                    f"בקשת המשתמש: \"{source}\""
                )
            proposed = request.proposed
            return (
                "✏️ האם לעדכן את ההוצאה?\n\n"
                "לפני:\n"
                f"{self._expense_details(original, names)}\n\n"
                "אחרי:\n"
                f"{self._expense_details(proposed, names)}\n"
                f"קטגוריה: {_CATEGORY_LABELS[proposed.category.value]}\n"
                f"תת־קטגוריה: {_SUBCATEGORY_LABELS[proposed.subcategory.value]}\n\n"
                f"בקשת המשתמש: \"{source}\""
            )

        expense = self._store.get_expense(proposal.expense_id)
        payers = [names.get(share.user_id, str(share.user_id)) for share in expense.shares if share.paid > 0]
        participants = [names.get(share.user_id, str(share.user_id)) for share in expense.shares if share.owed > 0]
        split = "\n".join(
            f"• {names.get(share.user_id, str(share.user_id))} — {display_amount(share.owed)} {_CURRENCY_LABELS[expense.currency]}"
            for share in expense.shares if share.owed > 0
        )
        heading = "✏️ האם לעדכן את ההצעה?" if expense.version > 0 else "🧾 האם להוסיף הוצאה חדשה?"
        return (
            f"{heading}\n\n"
            f"תיאור: {_safe_display(expense.description, 60)}\n"
            f"סכום: {display_amount(expense.total)} {_CURRENCY_LABELS[expense.currency]}\n"
            f"שילם/ה: {', '.join(payers)}\n"
            f"משתתפים: {', '.join(participants)}\n"
            f"קטגוריה: {_CATEGORY_LABELS[expense.category.value]}\n"
            f"תת־קטגוריה: {_SUBCATEGORY_LABELS[expense.subcategory.value]}\n"
            f"חלוקה:\n{split}\n\n"
            f"הודעת המקור: \"{source}\""
        )

    @staticmethod
    def _expense_people_details(expense, names: dict[int, str]) -> str:
        currency = _CURRENCY_LABELS[expense.currency]
        payers = [names.get(share.user_id, str(share.user_id)) for share in expense.shares if share.paid > 0]
        sharing = [share for share in expense.shares if share.owed > 0]
        participants = [names.get(share.user_id, str(share.user_id)) for share in sharing]
        split = "\n".join(
            f"• {names.get(share.user_id, str(share.user_id))}: {display_amount(share.owed)} {currency}"
            for share in sharing
        )
        return (
            f"שילם/ה: {', '.join(payers)}\n"
            f"משתתפים: {', '.join(participants)}\n"
            f"חלוקה:\n{split}"
        )

    @classmethod
    def _expense_details(cls, expense, names: dict[int, str]) -> str:
        return (
            f"תיאור: {_safe_display(expense.description, 60)}\n"
            f"סכום: {display_amount(expense.total)} {_CURRENCY_LABELS[expense.currency]}\n"
            f"תאריך: {expense.spent_on.isoformat()}\n"
            f"{cls._expense_people_details(expense, names)}"
        )

    def _resolve_reply_target(self, chat_id: int, reply_to_telegram_message_id: int | None) -> int | None:
        if reply_to_telegram_message_id is None:
            _log.debug("resolve_reply_target: no reply_to_telegram_message_id given")
            return None
        found = self._store.get_bot_message(chat_id, reply_to_telegram_message_id)
        _log.debug(
            "resolve_reply_target: chat_id=%s reply_to=%s bot_messages lookup -> %s",
            chat_id, reply_to_telegram_message_id, found,
        )
        if found is not None:
            kind, target_id = found
            if kind == "expense":
                return target_id
            try:
                return self._store.get_change_request(target_id).expense_id
            except KeyError:
                _log.debug("resolve_reply_target: change_request %s no longer exists", target_id)
                return None
        # Not a reply to one of the bot's own messages: it may be a reply to the ORIGINAL message
        # that created the expense (the user replying to their own report, not the confirmation).
        original = self._store.get_expense_by_message(chat_id, reply_to_telegram_message_id)
        if original is not None:
            _log.debug("resolve_reply_target: reply matched the ORIGINAL message of expense %s", original.id)
            return original.id
        _log.debug("resolve_reply_target: reply_to=%s matched nothing at all", reply_to_telegram_message_id)
        return None

    def handle_callback(self, *, chat_id: int, sender_id: int, callback_data: str) -> OutgoingMessage:
        """A ✓/✗ button press -> one acknowledgement message, NEVER the model's text (no model is
        ever involved in a button press, and this call does not touch the roster at all: it goes
        straight to the Store, the same way `WriteTools.confirm_expense`/`respond_change` do
        internally, since neither needs the member list). `callback_data` is "<kind>:<id>:<yes|no>"
        exactly as `handle_message` built it. Malformed data (wrong shape, `kind` not "expense"/
        "change_request", `id`/action not parseable) -> a fixed error message, nothing changed.

        "expense" -> `store.respond_expense(id, sender_id, approve, now=clock())`; "change_request"
        -> `store.respond(id, sender_id, approve, now=clock())` (`approve` = the action is "yes").
        `NotRelevantApprover` (this button is not for `sender_id` to press), `IllegalTransition`
        (already answered, or expired), `StateConflict` (a concurrent race lost), and an unknown
        id (`KeyError`) all become the SAME fixed error message, never a crash and never leaking
        which exact reason it was (a person who is not a relevant approver should not learn
        that from the error text). On success: a fixed, short, code-built acknowledgement --
        "✓ אושר" when `approve`, "✗ בוטל" otherwise -- never the model's wording (there is no
        model wording to begin with: buttons never call the LLM).

        The sender's pending edit-target and pending delete-target (see `Store.pending_edit_target`
        / `pending_delete_target`) are each cleared ONLY when this callback answered THAT SAME
        expense: "expense" compares `target_id` directly; "change_request" compares
        `store.respond`'s returned `expense_id` (the change request's own target, not the
        request's id); "settlement" never matches (a settlement has no associated expense id) and
        so never clears either. An unrelated button press -- approving or rejecting some other
        expense while an edit or a delete is pending on a different one -- leaves that target
        untouched, so the sender's next free-text message still lands on the expense they pressed
        ✏️ תיקון or 🗑️ מחיקה for."""
        parts = callback_data.split(":")
        parsed = self._parse_callback(callback_data)
        if parsed is None:
            return OutgoingMessage(text=_CALLBACK_ERROR, edit_original=False, alert=True)
        kind, target_id, action = parsed
        if action in ("edit", "delete"):
            try:
                expense = self._store.get_expense(target_id)
                if kind != "expense" or expense.chat_id != chat_id:
                    raise KeyError(target_id)
            except KeyError:
                return OutgoingMessage(text=_CALLBACK_ERROR, edit_original=False, alert=True)
            if action == "edit":
                self._store.begin_pending_edit(chat_id, sender_id, target_id, now=self._clock())
            else:
                # Deterministic, like the edit flow: the sender's NEXT message -- whatever it
                # says -- triggers `propose_delete` on THIS target directly in `handle_message`,
                # bypassing the router and the Agent's own tool choice entirely (see
                # `pending_delete_target`). `propose_delete` extracts nothing from the message
                # text, so unlike edit there is no "wording" for the sender to get right.
                self._store.begin_pending_delete(chat_id, sender_id, target_id, now=self._clock())
            instruction = (
                "כתוב/י עכשיו את התיקון המבוקש." if action == "edit"
                else "שלח/י כל הודעה כדי לאשר בקשה למחיקת ההוצאה הזו."
            )
            _log.debug(
                "handle_callback: action=%s chat_id=%s sender_id=%s target_id=%s "
                "remembered_search_results=%s instruction=%r",
                action, chat_id, sender_id, target_id,
                [target_id] if action == "delete" else None, instruction,
            )
            # Button presses never went through log_message before (handle_message is the only
            # caller) -- meaning the bot's own instruction text was invisible to the next turn's
            # `recent_messages` context, unlike every other bot reply. Logged here for the same
            # reason every other reply is: a vague follow-up ("תמחק את 1") relies partly on that
            # natural-language history to resolve, on top of the structured Selection-mapping.
            self._store.log_message(chat_id, "bot", instruction)
            return OutgoingMessage(text=instruction, edit_original=False)
        approve = action == "yes"
        answered_expense_id: int | None = None
        try:
            if kind == "expense":
                self._store.respond_expense(target_id, sender_id, approve, now=self._clock())
                answered_expense_id = target_id
            elif kind == "settlement":
                self._store.respond_settlement(target_id, sender_id, approve, now=self._clock())
            else:
                updated = self._store.respond(target_id, sender_id, approve, now=self._clock())
                answered_expense_id = updated.expense_id
        except (NotRelevantApprover, IllegalTransition, StateConflict, KeyError):
            return OutgoingMessage(text=_CALLBACK_ERROR, edit_original=False, alert=True)
        # Only clear a pending edit-/delete-target when THIS callback answered that same expense:
        # an unrelated approval/rejection must not silently cancel an in-progress ✏️ תיקון or
        # 🗑️ מחיקה flow on a different expense (a settlement callback never matches: it has no
        # expense id at all).
        if answered_expense_id is not None:
            if self._store.pending_edit_target(chat_id, sender_id, now=self._clock()) == answered_expense_id:
                self._store.clear_pending_edit(chat_id, sender_id)
            if self._store.pending_delete_target(chat_id, sender_id, now=self._clock()) == answered_expense_id:
                self._store.clear_pending_delete(chat_id, sender_id)
        buttons = ()
        if approve and kind == "expense":
            buttons = (("✏️ תיקון", f"expense:{target_id}:edit"), ("🗑️ מחיקה", f"expense:{target_id}:delete"))
        ack_text = "✓ אושר" if approve else "✗ בוטל"
        _log.debug(
            "handle_callback: kind=%s chat_id=%s sender_id=%s target_id=%s approve=%s ack=%r",
            kind, chat_id, sender_id, target_id, approve, ack_text,
        )
        self._store.log_message(chat_id, "bot", ack_text)
        return OutgoingMessage(text=ack_text, buttons=buttons)

    @staticmethod
    def _parse_callback(callback_data: str) -> tuple[str, int, str] | None:
        parts = callback_data.split(":")
        if len(parts) != 3:
            return None
        kind, raw_id, action = parts
        if kind not in ("expense", "change_request", "settlement") or action not in ("yes", "no", "edit", "delete"):
            return None
        try:
            target_id = int(raw_id)
        except ValueError:
            return None
        return kind, target_id, action


# --- the actual python-telegram-bot wiring ------------------------------------------------------
# Thin adapter: parses a real `telegram.Update` into BotLogic's plain calls, sends the result(s)
# through the Bot API, and records the sent message's REAL id for reply targeting. The local
# adapter workflow is covered with real Update objects and fake network boundaries in tests/e2e;
# tests/e2e/scenarios.md still verifies the real Telegram network integration.

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from splitbot.config import require
from splitbot.llm.client import OpenAIClient
from splitbot.router.jev_router import JevRouter


def _is_mention(update: Update, bot_username: str | None) -> bool:
    message = update.message
    if message is None or not bot_username or not message.entities:
        return False
    needle = f"@{bot_username}".casefold()
    return any(
        entity.type == "mention" and message.text[entity.offset:entity.offset + entity.length].casefold() == needle
        for entity in message.entities
    )


def _keyboard(buttons: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows
    ])


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None or update.effective_user is None:
        return
    bot_logic: BotLogic = context.bot_data["bot_logic"]
    store: Store = context.bot_data["store"]
    chat_id = message.chat_id
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    _log.debug(
        "on_message: chat_id=%s message_id=%s sender=%s reply_to_message=%s reply_to_id=%s text=%r",
        chat_id, message.message_id, update.effective_user.id,
        message.reply_to_message is not None, reply_to, message.text,
    )
    outgoing = bot_logic.handle_message(
        chat_id=chat_id,
        sender_id=update.effective_user.id,
        sender_name=update.effective_user.full_name or update.effective_user.username or str(update.effective_user.id),
        message_id=message.message_id,
        text=message.text,
        is_mention=_is_mention(update, context.bot.username),
        reply_to_telegram_message_id=reply_to,
    )
    for item in outgoing:
        sent = await message.reply_text(item.text, reply_markup=_keyboard(item.buttons))
        if item.record_as is not None:
            store.record_bot_message(chat_id, sent.message_id, *item.record_as)
            _log.debug(
                "on_message: recorded bot_messages chat_id=%s telegram_message_id=%s kind=%s target_id=%s",
                chat_id, sent.message_id, *item.record_as,
            )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None or query.message is None:
        return
    bot_logic: BotLogic = context.bot_data["bot_logic"]
    ack = bot_logic.handle_callback(
        chat_id=query.message.chat_id, sender_id=update.effective_user.id, callback_data=query.data,
    )
    if not ack.edit_original:
        await query.answer(ack.text if ack.alert else None, show_alert=ack.alert)
        if not ack.alert:
            await query.message.reply_text(ack.text)
        return
    await query.answer()
    await query.edit_message_text(ack.text, reply_markup=_keyboard(ack.buttons))
    if ack.record_as is not None:
        store: Store = context.bot_data["store"]
        store.record_bot_message(query.message.chat_id, query.message.message_id, *ack.record_as)


def build_application(
    *,
    store: Store | None = None,
    chat_llm: ChatLLM | None = None,
    write_llm: LLMClient | None = None,
    router: Router | None = None,
) -> Application:
    """Wires one long-lived `Store` (file-backed, `config.db_path()`), one real `Agent` model
    client (`config.require("AGENT_MODEL")`) and one real extractor client (`OPENAI_MODEL`, via
    `OpenAIClient.from_env()`), and the Jev router, into a `BotLogic` shared by every update.
    Every argument can be overridden (used by `main` mostly for documentation; real callers should
    not need to)."""
    from splitbot.config import db_path

    database_path = db_path()
    store = store or Store(database_path)
    chat_llm = chat_llm or OpenAIClient.from_env(model=require("AGENT_MODEL"))
    write_llm = write_llm or OpenAIClient.from_env()
    router = router or JevRouter.from_env()
    bot_logic = BotLogic(store, chat_llm, write_llm, router)

    application = Application.builder().token(require("TELEGRAM_BOT_TOKEN")).build()
    application.bot_data["store"] = store
    application.bot_data["bot_logic"] = bot_logic
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    application.add_handler(CallbackQueryHandler(_on_callback))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).setLevel(logging.DEBUG)  # this module's own reply-resolution trace
    _configure_correction_debug_log()
    build_application().run_polling()


if __name__ == "__main__":
    main()
