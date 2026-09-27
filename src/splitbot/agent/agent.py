"""The agent: a tool-calling LLM loop with guardrails. The model chooses tools; the tools enforce the
rules; the code decides everything else. The model never sees or supplies identity, message text or
money arithmetic for a write, and a write can only reach the ledger as a PENDING record through
`WriteTools` (extractor -> validators -> confirmation -> ledger)."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from splitbot.config import agent_cost_cap
from splitbot.llm.client import ChatLLM, LLMError
from splitbot.llm.extractor import load_prompt
from splitbot.models import Member
from splitbot.store import Store
from splitbot.tools.read_tools import TOOL_SPECS, ReadTools, ToolError
from splitbot.tools.write_tools import Proposal, WriteTools

MAX_STEPS = 5  # tool calls executed per turn
FALLBACK_TEXT = "לא הצלחתי לענות על זה בצורה בטוחה. אפשר לנסח שוב או לשאול אחרת?"

_NUMBER = re.compile(r"(?<!\w)-?\d+(?:[.,]\d+)*")
_TARGET_PARAMETER = {
    "type": "object",
    "properties": {"target_expense_id": {"type": "integer", "description": "id of the expense, when it is not a reply"}},
    "additionalProperties": False,
}
_WRITE_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "propose_expense",
            "description": "Record the user's message as a NEW expense. Only proposes it: the user must confirm.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_correction",
            "description": "Propose changing the amount of an existing expense (the one replied to, or one found by search).",
            "parameters": _TARGET_PARAMETER,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete",
            "description": "Propose deleting ONE existing expense (the one replied to, or one found by search).",
            "parameters": _TARGET_PARAMETER,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise_pending",
            "description": "Revise a PENDING (not yet confirmed) expense with a free-text correction, replied to "
            "directly (never one found by search: a pending expense is never a search result).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
_WRITE_TOOLS = {spec["function"]["name"] for spec in _WRITE_SPECS}
_ERROR_UNKNOWN = {"error": "unknown tool"}
_ERROR_ARGUMENTS = {"error": "the arguments were not accepted"}
_ERROR_TARGET = {"error": "the target must be the replied-to expense or an expense found by search_expenses in this turn"}


@dataclass(frozen=True)
class ToolTrace:
    """One tool call the agent executed (or refused) in a turn."""

    name: str
    arguments: dict  # exactly what the model sent
    result: dict  # what the tool returned to the model; {"error": "<fixed text>"} when refused or failed


@dataclass(frozen=True)
class AgentReply:
    text: str  # what the bot posts; never empty
    tool_calls: tuple[ToolTrace, ...]  # in execution order
    proposals: tuple[Proposal, ...]  # results of the write tools, in order
    steps: int  # tool calls executed (refused ones count)
    cost_usd: Decimal  # sum of the agent model calls of this turn (unknown costs count as 0)
    fallback_reason: str | None  # None for a normal reply, else "max_steps" | "cost_cap" | "ungrounded" | "llm_error"


def numbers_are_grounded(answer: str, sources: list[str]) -> bool:
    """True when every number written in `answer` also appears in one of the `sources` texts.
    Numbers are compared by VALUE (Decimal, never float), not by spelling: "1,200", "₪1200",
    "1200.00" and "1200" are the same number; a comma followed by exactly three digits is a
    thousands separator, any other comma is a decimal separator ("38,90" = 38.90). A number
    is any run of digits with optional separators inside it; a "2026-09" is two numbers, 2026 and 9
    (a leading zero does not matter). An answer with no numbers is grounded. A number that
    appears in a source only as a slice of a longer number does NOT count ("40" is not in "240").
    SIGN is part of the value: "-38.90" and "38.90" are different numbers, so a negative balance
    never grounds a claim of the positive amount or vice versa. A "-" is only a sign when nothing
    right before it is a letter or digit: "2026-09" and the Hebrew "ו-50" ("and 50") keep their
    hyphen as punctuation, not a sign, exactly as before."""
    values = _values(answer)
    if any(value is None for value in values):
        return False
    known = {value for source in sources for value in _values(source) if value is not None}
    return all(value in known for value in values)


def _values(text: str) -> list[Decimal | None]:
    return [_value(token) for token in _NUMBER.findall(text)]


def _value(token: str) -> Decimal | None:
    """The number a token stands for; None when it cannot be read (e.g. "1,2,3")."""
    try:
        if "," in token and "." in token:  # "1,200.00": the commas are thousands separators
            return Decimal(token.replace(",", ""))
        if "," in token:
            head, *tail = token.split(",")
            if all(len(part) == 3 for part in tail):  # "1,200" and "1,200,000": thousands
                return Decimal(head + "".join(tail))
            if len(tail) == 1:  # "38,90": a decimal comma
                return Decimal(f"{head}.{tail[0]}")
            return None
        return Decimal(token)
    except InvalidOperation:
        return None


_CURRENCY_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€"}


def _balances_sentence(get_balances_result: dict) -> str:
    """The code-rendered answer to a pure balance question, from `ReadTools.get_balances`'s OWN
    result (`{"balances": {<currency>: {"members": [...], "transfers": [...]}}}`), NEVER touched
    by the model (see `run_turn`, point 6): one line per settlement transfer, across every
    currency that has one, in the result's own key order:
        "<from_name> משלם/ת ל<to_name>: <amount> <symbol>"
    (`amount` is the transfer's own display string; `symbol` falls back to the currency code
    itself when it is not one of ILS/USD/EUR), lines joined with "\\n". A currency with no
    transfers contributes nothing. When there are no transfers in ANY currency (nothing is owed
    to anyone), the fixed sentence "אין חובות פתוחים כרגע." is returned instead."""
    lines = []
    for currency, data in get_balances_result["balances"].items():
        symbol = _CURRENCY_SYMBOLS.get(currency, currency)
        for transfer in data["transfers"]:
            lines.append(f"{transfer['from_name']} משלם/ת ל{transfer['to_name']}: {transfer['amount']} {symbol}")
    return "\n".join(lines) if lines else "אין חובות פתוחים כרגע."


class Agent:
    """`llm` is a ChatLLM (tests script it; the real one is OpenAIClient). `store` and `members` (the
    roster) feed the read tools; `write_tools` is the WriteTools of the same store (it owns the
    extractor, the validators and the confirmation template). `prompt_version` names a file in
    prompts/ (default "agent_v1", loaded with `splitbot.llm.extractor.load_prompt`). `clock` returns
    the current timezone-aware datetime (default: now in UTC). `max_cost_usd` defaults to
    `config.agent_cost_cap()`: the env var AGENT_MAX_COST_USD, a positive number of dollars, default
    0.02; it is read in the constructor and a bad value raises `config.ConfigError` there."""

    def __init__(
        self,
        llm: ChatLLM,
        store: Store,
        members: list[Member],
        write_tools: WriteTools,
        *,
        prompt_version: str = "agent_v1",
        clock: Callable[[], datetime] | None = None,
        max_cost_usd: Decimal | None = None,
    ):
        self._llm = llm
        self._store = store
        self._members = members
        self._write_tools = write_tools
        self._prompt_version = prompt_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_cost = max_cost_usd if max_cost_usd is not None else agent_cost_cap()

    def run_turn(
        self,
        *,
        chat_id: int,
        sender_id: int,
        message_id: int,
        text: str,
        reply_target_expense_id: int | None = None,
    ) -> AgentReply:
        """One user message -> one reply.

        Messages sent to the model: [system, user]. The system message is the prompt file followed
        by a code-built context block: today's date (from the clock, "YYYY-MM-DD"), the roster
        ("<id> <name>" per member), the sender (id and name) and the replied-to expense id or "none".
        The user message is `text` and nothing else: message text is data and only ever appears in
        the "user" message. Tools offered: the three read tools (`read_tools.TOOL_SPECS`) and four
        write tools: `propose_expense` (no parameters), `propose_correction` and `propose_delete`
        (one optional integer parameter `target_expense_id`), and `revise_pending` (no parameters
        at all: its target is ALWAYS `reply_target_expense_id`, never a model-supplied id -- a
        pending expense is never a `search_expenses` result, so there is no other way to name one).

        Loop: call `llm.chat(messages, tools)`; the reply's `message` is appended to the history.
        1. Text reply (no tool calls): the turn ends with that text, after the grounding check below.
        2. Tool calls: they run in order; each result is appended as
        {"role": "tool", "tool_call_id": <id>, "content": <json.dumps(result, ensure_ascii=False)>}.
           - A call that would be the 6th executed one ends the turn: fallback "max_steps".
           - Unknown tool name, arguments the tool does not accept, or a `ToolError` from a read tool
             are NOT crashes: the result is {"error": "<fixed text>"} (never the model's text) and
             the loop continues. They count as steps.
           - Read tools run on `ReadTools(store, chat_id, members)` with the model's arguments.
           - `propose_expense` calls `write_tools.propose_expense(chat_id=, message_id=, sender_id=,
             text=)` with values from THIS method's arguments; any argument the model sends is
             ignored. `propose_correction` / `propose_delete` do the same and take the target from
             `reply_target_expense_id` when it is given (the model's `target_expense_id` is then
             ignored); otherwise the model's `target_expense_id` is accepted only when that id
             appeared as an "id" in a `search_expenses` result of THIS turn, else the result is an
             error and nothing is proposed. `revise_pending` ALWAYS takes its target from
             `reply_target_expense_id`; with no reply target, the result is the same fixed error
             and nothing is revised (there is no `search_expenses` fallback for it at all). The
             result the model sees for a write is {"status", "issues"} (no confirmation text).
        3. After each model call, the turn's cost is the sum of `cost_usd` (None counts as 0). If it
           exceeds `max_cost_usd`, the turn ends with fallback "cost_cap" BEFORE any of that
           reply's tool calls run. An `LLMError` from the client ends the turn with "llm_error".
        4. As soon as the executed tool calls of one model reply include a write tool that returned
           status "pending_confirmation", the loop stops (no further model call) and the reply text
           is the `confirmation_text` of each such proposal joined by "\n", verbatim: the model's own
           wording is never used for a confirmation.
        5. Grounding of a model text: `numbers_are_grounded(text, sources)` with the JSON of every
           tool result of this turn plus the user's `text` (a number the user wrote is not invented).
           If it fails: fallback "ungrounded".
        6. A pure balance question: if `get_balances` is the ONLY tool ever called during the
           whole turn (once or several times; no other tool, read or write), the final reply is
           NOT the model's own text at all: it is `_balances_sentence` of the LAST `get_balances`
           call's own result, rendered entirely by code (same principle as a confirmation: the
           model narrating a debtor/creditor pairing from a balances table is exactly the kind of
           arithmetic-adjacent phrasing that must not be left to it). This is a normal reply, not
           a fallback (`fallback_reason` stays None), and it is never grounding-checked: it is
           correct by construction, since it comes only from that call's own real result. As soon
           as any OTHER tool is called during the turn (even once, even before or after
           `get_balances`), this rule no longer applies and the model's own text is used as usual.
        Every fallback returns `FALLBACK_TEXT`, `fallback_reason` set, and everything the turn did
        so far in `tool_calls` / `proposals`. Nothing here ever raises for a model mistake."""
        reads = ReadTools(self._store, chat_id, self._members)
        messages = [
            {"role": "system", "content": self._system_prompt(sender_id, reply_target_expense_id)},
            {"role": "user", "content": text},
        ]
        tools = [*TOOL_SPECS, *_WRITE_SPECS]
        traces: list[ToolTrace] = []
        proposals: list[Proposal] = []
        sources: list[str] = []
        found_ids: set[int] = set()
        cost = Decimal(0)
        steps = 0
        only_balances = True  # true as long as every tool called so far, if any, was get_balances
        balances_result: dict | None = None  # the LAST get_balances call's own result, if any

        def reply(reply_text: str, reason: str | None = None) -> AgentReply:
            return AgentReply(reply_text, tuple(traces), tuple(proposals), steps, cost, reason)

        try:
            while True:
                result = self._llm.chat(messages, tools)
                cost += result.cost_usd or Decimal(0)
                if cost > self._max_cost:
                    return reply(FALLBACK_TEXT, "cost_cap")
                if not result.tool_calls:
                    if only_balances and balances_result is not None:
                        return reply(_balances_sentence(balances_result))
                    answer = (result.content or "").strip()
                    if not answer:
                        return reply(FALLBACK_TEXT, "llm_error")
                    if not numbers_are_grounded(answer, [*sources, text]):
                        return reply(FALLBACK_TEXT, "ungrounded")
                    return reply(answer)
                messages.append(result.message)
                confirmations: list[str] = []
                for call in result.tool_calls:
                    if steps == MAX_STEPS:
                        return reply(FALLBACK_TEXT, "max_steps")
                    steps += 1
                    output, proposal = self._execute(
                        call.name, call.arguments, reads, found_ids,
                        chat_id=chat_id, sender_id=sender_id, message_id=message_id, text=text,
                        reply_target=reply_target_expense_id,
                    )
                    if proposal is not None:
                        proposals.append(proposal)
                        output = {"status": proposal.status, "issues": list(proposal.issues)}
                        if proposal.status == "pending_confirmation" and proposal.confirmation_text:
                            confirmations.append(proposal.confirmation_text)
                    if call.name == "get_balances" and "balances" in output:
                        balances_result = output
                    else:
                        only_balances = False
                    traces.append(ToolTrace(call.name, call.arguments, output))
                    encoded = json.dumps(output, ensure_ascii=False)
                    sources.append(encoded)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": encoded})
                if confirmations:
                    return reply("\n".join(confirmations))
        except LLMError:
            return reply(FALLBACK_TEXT, "llm_error")

    # --- internals ------------------------------------------------------------

    def _system_prompt(self, sender_id: int, reply_target: int | None) -> str:
        names = {m.id: m.name for m in self._members}
        roster = "\n".join(f"{m.id} {m.name}" for m in self._members)
        return (
            f"{load_prompt(self._prompt_version)}\n\n"
            "Context (from the bot, not from the user):\n"
            f"Today: {self._clock().date().isoformat()}\n"
            f"Members:\n{roster}\n"
            f"Sender: {sender_id} {names.get(sender_id, 'unknown')}\n"
            f"Replied-to expense id: {reply_target if reply_target is not None else 'none'}"
        )

    def _execute(self, name, arguments, reads, found_ids, *, chat_id, sender_id, message_id, text, reply_target):
        """(result the model sees, Proposal or None). Never raises for a bad call."""
        if name in _WRITE_TOOLS:
            common = dict(chat_id=chat_id, message_id=message_id, sender_id=sender_id, text=text)
            if name == "propose_expense":
                return {}, self._write_tools.propose_expense(**common)
            if name == "revise_pending":
                # a pending expense is never a search result: the reply is the ONLY valid target,
                # never a model-supplied id, even if one were somehow sent.
                if reply_target is None:
                    return dict(_ERROR_TARGET), None
                return {}, self._write_tools.revise_pending(**common, target_expense_id=reply_target)
            target = reply_target
            if target is None:
                asked = arguments.get("target_expense_id")
                if isinstance(asked, int) and not isinstance(asked, bool) and asked in found_ids:
                    target = asked
            if target is None:
                return dict(_ERROR_TARGET), None
            propose = self._write_tools.propose_correction if name == "propose_correction" else self._write_tools.propose_delete
            return {}, propose(**common, target_expense_id=target)
        method = {
            "get_balances": reads.get_balances,
            "search_expenses": reads.search_expenses,
            "spending_summary": reads.spending_summary,
        }.get(name)
        if method is None:
            return dict(_ERROR_UNKNOWN), None
        try:
            output = method(**arguments)
        except (ToolError, TypeError):
            return dict(_ERROR_ARGUMENTS), None
        if name == "search_expenses":
            found_ids.update(e["id"] for e in output["expenses"])
        return output, None
