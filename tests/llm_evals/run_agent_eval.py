"""Agent eval harness: `python -m tests.llm_evals.run_agent_eval [--yes]`.

Real LLM calls happen ONLY in `main()` with `--yes` (both the agent's own chat calls and, for
cases that reach a write tool, the extractor's calls). Everything else is pure or takes an
injected client (unit tests use a scripted fake). The dataset is verified by the human (Ziv) and
is never edited by code; this module only loads, seeds, runs and grades it.

Every case pretends "today" is `EVAL_TODAY` (2026-09-15): both the seeded ledger's dates and the
agent's own clock use it, so "this month" / "the previous month" mean the same thing to the
seed and to the model.

Dataset file (in `datasets/`): `agent_cases.jsonl` + `rosters.json` (shared with the other
evals). One JSON object per line:
  {"id", "roster": "A", "sender": "<name>", "message": "...", "reply_to": "<seed id>" | null,
   "seed": [{"id", "description", "amount": "<plain amount>", "currency": "ILS"|"USD"|"EUR"
             (default ILS), "payer": "<name>", "participants": "everyone" | ["<name>", ...],
             "subcategory": "<Subcategory value>",
             "month": "current" | "previous" | "two_months_ago", "days_ago": <int>}, ...],
   "outcome": {"kind": "read_answer" | "pending_expense" | "pending_delete" |
               "pending_correction" | "no_action_explained" | "no_confirmed_write", ...},
   "notes": "..."}
A seed item gives EITHER "month" (a relative month name, resolved against `EVAL_TODAY`; the day
of month is picked by `seed_world`, not by the dataset) OR "days_ago" (an exact day offset from
`EVAL_TODAY`, for a scenario that names a specific relative day); never both.

`outcome.kind` fields:
  - "read_answer": "tools" (an ordered list of {"name", "arguments": {...} | absent}) OR
    "tools_any_of" (a list of alternative "tools"-shaped sequences, any one of which is
    accepted); "forbidden_tools" (tool names that must not be called, IN ADDITION to the three
    write tools, which are always forbidden for this kind); "expected_numbers" (plain amount
    strings that must all appear, BY MAGNITUDE (see `numbers_present`), in the final reply
    text; may be empty); "expected_attributions" (optional: {"<roster name>": "<plain
    magnitude>", ...}), for a question whose answer names SEVERAL people with DIFFERENT
    numbers (e.g. "who owes whom"), where a magnitude-only check cannot tell a right answer
    from one that swapped who owes what. The reply text is split into clauses on "," / "."
    /newlines; a name's expected magnitude must equal the SUM of every number found across
    every clause that mentions that name (not just appear in ONE such clause: a settlement
    answer may legitimately split one person's total across several transfer lines when they
    owe, or are owed by, more than one other person). See `attributions_present`'s own
    docstring for the exact rule. Only the SIZE of each number is checked, never its sign
    (`agent._value`'s parser, same rule as `numbers_present`): natural phrasing conveys
    direction with words like "owes" / "is owed", not a minus sign. A name never mentioned
    anywhere, or whose mentioning clauses do not sum to its magnitude, fails.
    Tool-name comparisons (both "forbidden_tools"/the three write tools, and every "tools" /
    "tools_any_of" step's "name") are done CASEFOLDED, so "Search_Expenses" cannot dodge a
    forbidden-tools check meant to catch "search_expenses".
    A "tools" sequence (or one alternative of "tools_any_of") matches when it is an ORDERED
    SUBSEQUENCE of the turn's actual tool calls: the named tools must occur, in that relative
    order, somewhere among the calls the model actually made (extra calls elsewhere, e.g. an
    extra harmless read, do not break the match). A step's "name" must equal the actual call's
    name; when "arguments" is given, every key in it must be present in that actual call's
    arguments with an equal value after substitution (below); the actual call may carry extra
    argument keys the step does not mention. `forbidden_tools` (and the three write tools) are
    checked across ALL of the turn's calls, regardless of order.
    An "arguments" value of "<current>" / "<previous>" / "<two_months_ago>" is a placeholder
    for that case's resolved "YYYY-MM", substituted before comparing to the model's actual
    call; every other argument is compared for equality after that substitution.
    A fallback reply (`AgentReply.fallback_reason` is not None) always fails a "read_answer"
    case, even one whose "expected_numbers" is empty: a safe fallback is never a real answer.
  - "pending_expense": "expected_total" (plain amount); "expected_participants" (list of names,
    or null to skip the check); "expected_shares" (name -> plain amount, or null to skip).
  - "pending_delete" / "pending_correction": "target" (a seed id); "pending_correction" also has
    "expected_new_total" (plain amount).
  - "no_action_explained": no extra fields. Checks that NEITHER a new expense NOR a new change
    request exists after the turn, and the seeded ledger (balances, live expenses) is unchanged.
  - "no_confirmed_write": "forbidden_total" (plain amount), "forbidden_payer" (name). Checks
    that no CONFIRMED, not-deleted expense with that total and that payer exists after the turn
    (a PENDING one is fine: this kind allows a normal confirmation flow, it only forbids an
    unconfirmed write reaching the ledger).
"""

import argparse
import calendar
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal

from splitbot.agent.agent import Agent, ToolTrace, _value, _values
from splitbot.config import price_for, prices_fingerprint, require
from splitbot.llm.client import ChatLLM, LLMClient, OpenAIClient
from splitbot.models import ApprovalMode, ChangeKind, Currency, Expense, ExpenseState, GroupConfig, Member, Subcategory
from splitbot.money import parse_amount, split_expense
from splitbot.store import Store
from splitbot.tools.write_tools import WriteTools

_WRITE_TOOLS = ("propose_expense", "propose_correction", "propose_delete")
_MONTH_PLACEHOLDERS = {"<current>": "current", "<previous>": "previous", "<two_months_ago>": "two_months_ago"}
_OUTCOME_KINDS = (
    "read_answer", "pending_expense", "pending_delete", "pending_correction",
    "no_action_explained", "no_confirmed_write",
)

DATASETS_DIR = Path(__file__).parent / "datasets"
RESULTS_DIR = Path(__file__).parent / "results"
EVAL_TODAY = date(2026, 9, 15)
CHAT_ID = 1


class DatasetError(ValueError):
    """A dataset row is not usable (the data is never "fixed" by code)."""


@dataclass(frozen=True)
class SeedItem:
    id: str
    description: str
    amount: str
    currency: Currency
    payer_id: int
    participant_ids: list[int]  # already resolved from "everyone" / names, in roster order
    subcategory: Subcategory
    month: Literal["current", "previous", "two_months_ago"] | None
    days_ago: int | None


@dataclass(frozen=True)
class Case:
    id: str
    members: list[Member]
    sender_id: int
    message: str
    reply_to: str | None  # a seed id, or None
    seed: list[SeedItem]
    outcome: dict  # kind-specific fields, exactly as documented above (kept as a plain dict)
    notes: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str]  # empty when passed; each a short, human-readable reason
    reply_text: str
    fallback_reason: str | None
    tool_calls: list[dict]  # [{"name", "arguments"}, ...] in call order, for the gallery
    proposal_statuses: list[str]  # Proposal.status of every write tool call this turn, in order
    cost_usd: Decimal  # the turn's agent-model cost only (WriteTools' extractor cost is separate)
    steps: int


def load_cases(datasets_dir: Path = DATASETS_DIR) -> list[Case]:
    """Read `agent_cases.jsonl` + `rosters.json` from `datasets_dir`, in file order.

    Raises DatasetError when: the roster is unknown; a name (sender, a seed's payer or a
    participant) is not on that roster; a seed's currency/subcategory is not a known value;
    a seed gives both "month" and "days_ago", or neither; `reply_to` or an outcome "target"
    names a seed id that is not in this case's own `seed` list; `outcome.kind` is not one of the
    six documented kinds, or is missing a field its kind requires; an id is duplicated in the
    file.
    """
    rosters = _read_json(datasets_dir / "rosters.json")
    rows = [json.loads(line) for line in (datasets_dir / "agent_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    cases: list[Case] = []
    seen: set[str] = set()
    for row in rows:
        case_id = row.get("id", "?")
        if case_id in seen:
            raise DatasetError(f"{case_id}: duplicate id")
        seen.add(case_id)
        cases.append(_to_case(row, rosters))
    return cases


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"cannot read {path.name}: {type(exc).__name__}") from None


def _to_case(row: dict, rosters: dict) -> Case:
    case_id = row.get("id", "?")
    roster_name = row.get("roster")
    if roster_name not in rosters:
        raise DatasetError(f"{case_id}: unknown roster {roster_name!r}")
    members = [Member(**m) for m in rosters[roster_name]]
    by_name = {m.name: m.id for m in members}

    def resolve(name: str) -> int:
        if name not in by_name:
            raise DatasetError(f"{case_id}: unknown member name {name!r} for roster {roster_name!r}")
        return by_name[name]

    seed_ids = {s["id"] for s in row.get("seed", [])}
    seed: list[SeedItem] = []
    for s in row.get("seed", []):
        has_month, has_days = "month" in s, "days_ago" in s
        if has_month == has_days:
            raise DatasetError(f"{case_id}/{s.get('id')}: give exactly one of \"month\" or \"days_ago\"")
        participants = s["participants"]
        participant_ids = [m.id for m in members] if participants == "everyone" else [resolve(n) for n in participants]
        try:
            currency = Currency(s.get("currency", "ILS"))
            subcategory = Subcategory(s["subcategory"])
        except ValueError as exc:
            raise DatasetError(f"{case_id}/{s.get('id')}: {exc}") from None
        seed.append(SeedItem(
            id=s["id"], description=s["description"], amount=s["amount"], currency=currency,
            payer_id=resolve(s["payer"]), participant_ids=participant_ids, subcategory=subcategory,
            month=s.get("month"), days_ago=s.get("days_ago"),
        ))

    reply_to = row.get("reply_to")
    if reply_to is not None and reply_to not in seed_ids:
        raise DatasetError(f"{case_id}: reply_to {reply_to!r} is not one of this case's own seed ids")

    outcome = row["outcome"]
    kind = outcome.get("kind")
    if kind not in _OUTCOME_KINDS:
        raise DatasetError(f"{case_id}: unknown outcome kind {kind!r}")
    required = {
        "read_answer": (),  # "tools" xor "tools_any_of" checked separately
        "pending_expense": ("expected_total",),
        "pending_delete": ("target",),
        "pending_correction": ("target", "expected_new_total"),
        "no_action_explained": (),
        "no_confirmed_write": ("forbidden_total", "forbidden_payer"),
    }[kind]
    for field_name in required:
        if field_name not in outcome:
            raise DatasetError(f"{case_id}: outcome kind {kind!r} needs {field_name!r}")
    if kind == "read_answer" and "tools" not in outcome and "tools_any_of" not in outcome:
        raise DatasetError(f"{case_id}: outcome kind \"read_answer\" needs \"tools\" or \"tools_any_of\"")
    target = outcome.get("target")
    if target is not None and target not in seed_ids:
        raise DatasetError(f"{case_id}: outcome target {target!r} is not one of this case's own seed ids")

    return Case(
        id=case_id, members=members, sender_id=resolve(row["sender"]), message=row["message"],
        reply_to=reply_to, seed=seed, outcome=outcome, notes=row.get("notes", ""),
    )


def seed_world(store: Store, case: Case, *, today: date = EVAL_TODAY) -> dict[str, int]:
    """Save every `case.seed` item into `store` as a CONFIRMED expense of `CHAT_ID`, using
    `money.split_expense` for the shares (payer = the seed's payer, participants = its
    resolved list; leftover minor units follow `split_expense`'s own rule). Returns
    {seed id: the saved expense's id}.

    Every seed in a "current"/"previous"/"two_months_ago" bucket gets a distinct day of that
    month, assigned in the order it appears among seeds sharing that same bucket in this case
    (5, 8, 11, ... for "current"; 10, 13, 16, ... for the other two buckets): the exact day is
    an implementation choice, never a fact from the scenario text, and does not affect any
    check (it only has to land in the right month, or `today - days_ago` when the seed gives
    that instead). The day is ALWAYS clamped to a real day of that month (never past its last
    day), and for "current" additionally clamped to stay BELOW `today`'s own day (never on or
    after "today", so a seed is always safely in the past): with few same-bucket seeds this
    keeps every day distinct; a dataset crowded enough to exhaust the room below the cap could
    see two same-bucket seeds share a day (still the right month, and each still gets its own
    expense id) rather than raise an error. Seeds are saved in `case.seed`'s list order, so
    their store ids are assigned in that same order (the first seed gets the lowest id).
    """
    bases = {"current": 5, "previous": 10, "two_months_ago": 10}
    counters = {"current": 0, "previous": 0, "two_months_ago": 0}
    ids: dict[str, int] = {}
    for index, item in enumerate(case.seed):
        if item.days_ago is not None:
            spent_on = today - timedelta(days=item.days_ago)
        else:
            day = bases[item.month] + 3 * counters[item.month]
            counters[item.month] += 1
            month_start = _shift_month(today, {"current": 0, "previous": -1, "two_months_ago": -2}[item.month])
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            cap = min(days_in_month, today.day - 1) if item.month == "current" else days_in_month
            spent_on = month_start.replace(day=min(day, max(1, cap)))
        total = parse_amount(item.amount)
        shares = split_expense(total, payer_id=item.payer_id, author_id=item.payer_id, participants=item.participant_ids)
        saved = store.save_expense(Expense(
            chat_id=CHAT_ID, message_id=1000 + index, author_id=item.payer_id, description=item.description,
            total=total, currency=item.currency, subcategory=item.subcategory, shares=shares,
            prompt_version="seed", spent_on=spent_on,
            created_at=datetime.combine(spent_on, time(12, 0), tzinfo=timezone.utc),
            state=ExpenseState.confirmed,
        ))
        ids[item.id] = saved.id
    return ids


def _shift_month(d: date, months: int) -> date:
    """The 1st of the month `months` away from `d` (0 = `d`'s own month, -1 = the previous one)."""
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _resolve_month(bucket: Literal["current", "previous", "two_months_ago"], today: date) -> str:
    """"YYYY-MM" for that bucket relative to `today` (current = today's month)."""
    offset = {"current": 0, "previous": -1, "two_months_ago": -2}[bucket]
    return _shift_month(today, offset).strftime("%Y-%m")


def run_case(
    case: Case,
    *,
    chat: ChatLLM,
    write_llm: LLMClient,
    clock: Callable[[], "datetime"] | None = None,
    prompt_version: str = "agent_v4",
) -> CaseResult:
    """Seed a fresh in-memory Store for `case`, build a real `WriteTools` (using `write_llm` as
    its extractor client, prompt_version "extract_v2") and a real `Agent` (using `chat`,
    `prompt_version`, default "agent_v4", the current runtime default), both clocked at
    `EVAL_TODAY` noon UTC unless `clock` is given,
    call `run_turn` with the case's message/sender and `reply_to`'s resolved expense id (from
    `seed_world`), then grade the outcome per its `kind` (see the module docstring) against the
    resulting `AgentReply` and the store's state before/after the turn. Never raises for a
    grading failure (that becomes an entry in `CaseResult.failures`); an `LLMError` from either
    client is NOT caught here (it propagates, same as `run_evals.run_split`: no partial file).
    `message_id` is fixed at 1 for every case (each case gets its own fresh Store, so there is
    no cross-case idempotency to worry about).
    """
    store = Store(":memory:")
    ids = seed_world(store, case)
    config = GroupConfig(chat_id=CHAT_ID, members=case.members, default_mode=ApprovalMode.author)
    write_tools = WriteTools(store, write_llm, case.members, config, prompt_version="extract_v2",
                              clock=clock or _eval_clock)
    agent = Agent(chat, store, case.members, write_tools, prompt_version=prompt_version,
                  clock=clock or _eval_clock, max_cost_usd=Decimal("1"))
    balances_before = store.balances(CHAT_ID)
    reply_target = ids.get(case.reply_to) if case.reply_to else None
    reply = agent.run_turn(chat_id=CHAT_ID, sender_id=case.sender_id, message_id=1, text=case.message,
                           reply_target_expense_id=reply_target)
    failures = _grade(case, reply, ids, store, balances_before)
    return CaseResult(
        case_id=case.id, passed=not failures, failures=failures, reply_text=reply.text,
        fallback_reason=reply.fallback_reason,
        tool_calls=[{"name": t.name, "arguments": t.arguments} for t in reply.tool_calls],
        proposal_statuses=[p.status for p in reply.proposals], cost_usd=reply.cost_usd, steps=reply.steps,
    )


def _eval_clock() -> datetime:
    return datetime.combine(EVAL_TODAY, time(12, 0), tzinfo=timezone.utc)


def _grade(case: Case, reply, ids: dict[str, int], store: Store, balances_before: dict) -> list[str]:
    kind = case.outcome["kind"]
    return {
        "read_answer": _grade_read_answer,
        "pending_expense": _grade_pending_expense,
        "pending_delete": _grade_pending_change,
        "pending_correction": _grade_pending_change,
        "no_action_explained": _grade_no_action,
        "no_confirmed_write": _grade_no_confirmed_write,
    }[kind](case, reply, ids, store, balances_before)


def _grade_read_answer(case: Case, reply, ids, store, balances_before) -> list[str]:
    outcome = case.outcome
    failures = []
    if reply.fallback_reason is not None:
        failures.append(f"the reply fell back ({reply.fallback_reason}) instead of answering")
    forbidden = {name.casefold() for name in (*_WRITE_TOOLS, *outcome.get("forbidden_tools", []))}
    hit = sorted({t.name for t in reply.tool_calls if t.name.casefold() in forbidden})
    if hit:
        failures.append(f"forbidden tool(s) called: {', '.join(hit)}")
    sequences = outcome.get("tools_any_of") or [outcome["tools"]]
    if not any(_matches_sequence(seq, reply.tool_calls, EVAL_TODAY) for seq in sequences):
        failures.append("the expected tool call sequence was not found")
    missing = numbers_present(reply.text, outcome.get("expected_numbers", []))
    if missing:
        failures.append(f"expected number(s) not in the answer: {', '.join(missing)}")
    if outcome.get("expected_attributions"):
        failures += attributions_present(reply.text, outcome["expected_attributions"])
    return failures


def attributions_present(text: str, expected: dict[str, str]) -> list[str]:
    """Failures for `numbers_are_grounded`-style names that must each be tied to their OWN
    magnitude, not just present somewhere in the text (see the module docstring's
    "expected_attributions"): `text` split into clauses on "," / "." / newlines; a name's
    expected magnitude must equal the SUM of every number found in every clause that mentions
    that name (not just appear in ONE such clause): a settlement answer may legitimately split
    one person's total across several transfer lines when they owe, or are owed by, more than
    one other person (e.g. "X pays Y 140" and "X pays Z 20" for an X who owes 160 in total), and
    each of those lines still correctly mentions X. Summing still catches a real regression
    (a swapped or invented number changes the sum), it just does not force the whole amount
    onto a single line."""
    # A structured Telegram block intentionally puts names and the amount on separate
    # lines. Keep those lines together; blank lines still separate distinct transfers.
    clauses = re.split(r"[,．.]|\n\s*\n", text)
    failures = []
    for name, amount in expected.items():
        wanted = _value(amount)
        mentioning = [c for c in clauses if name in c]
        if not mentioning:
            failures.append(f"{name} is never mentioned in the answer")
            continue
        total = sum((v for c in mentioning for v in _values(c) if v is not None), Decimal(0))
        if wanted is None or abs(total) != abs(wanted):
            failures.append(f"{name}'s amount ({amount}) is not attributed to them in the answer")
    return failures


def _matches_sequence(steps: list[dict], calls: list, today: date) -> bool:
    index = 0
    for call in calls:
        if index >= len(steps):
            break
        step = steps[index]
        if call.name.casefold() == step["name"].casefold() and _args_match(step.get("arguments"), call.arguments, today):
            index += 1
    return index == len(steps)


def _args_match(expected: dict | None, actual: dict, today: date) -> bool:
    if expected is None:
        return True
    for key, value in expected.items():
        wanted = _resolve_month(_MONTH_PLACEHOLDERS[value], today) if value in _MONTH_PLACEHOLDERS else value
        if actual.get(key) != wanted:
            return False
    return True


def _grade_pending_expense(case: Case, reply, ids, store, balances_before) -> list[str]:
    outcome = case.outcome
    expense_id = len(case.seed) + 1
    try:
        expense = store.get_expense(expense_id)
    except KeyError:
        return ["no pending expense was created"]
    failures = []
    if expense.state != ExpenseState.pending_confirmation:
        failures.append(f"the expense is {expense.state.value}, not pending_confirmation")
    if expense.total != parse_amount(outcome["expected_total"]):
        failures.append(f"total is {expense.total}, expected {parse_amount(outcome['expected_total'])}")
    if outcome.get("expected_participants") is not None:
        wanted = {_id_of(case, name) for name in outcome["expected_participants"]}
        got = {s.user_id for s in expense.shares if s.owed > 0}
        if got != wanted:
            failures.append(f"participants are {sorted(got)}, expected {sorted(wanted)}")
    if outcome.get("expected_shares") is not None:
        wanted = {_id_of(case, name): parse_amount(amount) for name, amount in outcome["expected_shares"].items()}
        got = {s.user_id: s.owed for s in expense.shares if s.user_id in wanted}
        if got != wanted:
            failures.append(f"shares are {got}, expected {wanted}")
    return failures


def _id_of(case: Case, name: str) -> int:
    return next(m.id for m in case.members if m.name == name)


def _grade_pending_change(case: Case, reply, ids, store, balances_before) -> list[str]:
    outcome = case.outcome
    try:
        request = store.get_change_request(1)
    except KeyError:
        return ["no pending change request was created"]
    failures = []
    wanted_kind = ChangeKind.delete if case.outcome["kind"] == "pending_delete" else ChangeKind.correction
    if request.kind != wanted_kind:
        failures.append(f"change request kind is {request.kind.value}, expected {wanted_kind.value}")
    if request.expense_id != ids[outcome["target"]]:
        failures.append("the change request targets the wrong expense")
    if request.state != ExpenseState.pending_confirmation:
        failures.append(f"the change request is {request.state.value}, not pending_confirmation")
    if wanted_kind == ChangeKind.correction and request.proposed is not None:
        if request.proposed.total != parse_amount(outcome["expected_new_total"]):
            failures.append(f"proposed total is {request.proposed.total}, expected {parse_amount(outcome['expected_new_total'])}")
    target = store.get_expense(ids[outcome["target"]])
    if target.state != ExpenseState.confirmed or target.deleted:
        failures.append("the target expense was already changed: it must wait for approval, even a group of one")
    return failures


def _grade_no_action(case: Case, reply, ids, store, balances_before) -> list[str]:
    failures = []
    try:
        store.get_expense(len(case.seed) + 1)
        failures.append("a new expense was created")
    except KeyError:
        pass
    try:
        store.get_change_request(1)
        failures.append("a new change request was created")
    except KeyError:
        pass
    if store.balances(CHAT_ID) != balances_before:
        failures.append("the ledger's balances changed")
    return failures


def _grade_no_confirmed_write(case: Case, reply, ids, store, balances_before) -> list[str]:
    """Scans the WHOLE ledger (`Store.search_expenses` already returns only confirmed,
    not-deleted expenses), not just a slot a fresh write might land in: a forbidden expense
    could equally already be sitting in the seeded ledger, not only one this turn created."""
    outcome = case.outcome
    total = parse_amount(outcome["forbidden_total"])
    payer_id = _id_of(case, outcome["forbidden_payer"])
    matches = store.search_expenses(CHAT_ID, payer_id=payer_id, min_total=total, max_total=total)
    if matches:
        return [f"a CONFIRMED expense of {total} to {outcome['forbidden_payer']}'s credit exists"]
    return []


def numbers_present(text: str, expected: list[str]) -> list[str]:
    """Plain amounts (as in a dataset row) not found in `text`, by MAGNITUDE (absolute value),
    reusing `agent.numbers_are_grounded`'s number parsing for the tokens found in `text`; empty
    when every expected number is present. This is deliberately looser than
    `numbers_are_grounded`'s sign-aware comparison: a balance's sign is normally conveyed by the
    surrounding words ("owes" vs "is owed"), not folded into the figure itself, so every
    `expected_numbers` entry in the dataset is an unsigned magnitude and is matched as one."""
    present = {abs(v) for v in _values(text) if v is not None}
    return [token for token in expected if (value := _value(token)) is None or abs(value) not in present]


@dataclass(frozen=True)
class Estimate:
    calls: int  # a rough count of agent-chat calls (cases * up to MAX_STEPS+1) plus extractor calls
    cost_usd_low: Decimal | None
    cost_usd_high: Decimal | None


def estimate_run(
    cases: list[Case],
    *,
    price_in_per_mtok: Decimal | None,
    price_out_per_mtok: Decimal | None,
) -> Estimate:
    """A ROUGH estimate, like `run_evals.estimate_run`: assumes every case uses between 1 and
    `MAX_STEPS + 1` agent-chat calls, and one extra extractor call (two if retried) for cases
    whose `outcome.kind` is a write kind (pending_expense/pending_delete/pending_correction/
    no_confirmed_write). Prices are per 1M tokens; None makes both bounds None."""
    from splitbot.agent.agent import MAX_STEPS

    write_kinds = {"pending_expense", "pending_delete", "pending_correction", "no_confirmed_write"}
    calls = sum(MAX_STEPS + 1 + (2 if c.outcome["kind"] in write_kinds else 0) for c in cases)
    if price_in_per_mtok is None or price_out_per_mtok is None:
        return Estimate(calls, None, None)
    million = Decimal(1_000_000)
    low = (Decimal(calls) * 1200 * price_in_per_mtok + Decimal(calls) * 150 * price_out_per_mtok) / million
    high = (Decimal(calls) * 2500 * price_in_per_mtok + Decimal(calls) * 400 * price_out_per_mtok) / million
    return Estimate(calls, low, high)


def build_report(results: list[CaseResult]) -> dict:
    """{"passed": n, "failed": n, "failing_ids": [...], "total_cost_usd": str,
    "total_steps": n}. `failing_ids` is sorted, in case id order (not run order)."""
    return {
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "failing_ids": [r.case_id for r in results if not r.passed],
        "total_cost_usd": format(sum((r.cost_usd for r in results), Decimal(0)), "f"),
        "total_steps": sum(r.steps for r in results),
    }


def build_gallery(cases: list[Case], results: list[CaseResult]) -> str:
    """Markdown, one `### <case id>` section per case in dataset order: the message, the sender
    name, PASS or FAIL (with `failures` listed when it fails), the reply text, the tool calls
    (name + arguments) in order, and the cost. A final line gives the passed/failed totals. Pure
    text; this is for Ziv to read, not graded by anything else."""
    by_id = {r.case_id: r for r in results}
    lines = ["# Agent eval gallery", ""]
    for case in cases:
        r = by_id[case.id]
        sender = next((m.name for m in case.members if m.id == case.sender_id), str(case.sender_id))
        lines.append(f"### {case.id}")
        lines.append(f"- sender: {sender}")
        lines.append(f"- message: {case.message}")
        lines.append(f"- status: {'PASS' if r.passed else 'FAIL'}")
        for failure in r.failures:
            lines.append(f"  - {failure}")
        lines.append(f"- reply: {r.reply_text}")
        calls_text = ", ".join(f"{t['name']}({t['arguments']})" for t in r.tool_calls) or "(none)"
        lines.append(f"- tool calls: {calls_text}")
        lines.append(f"- cost: ${r.cost_usd}")
        lines.append("")
    passed = sum(1 for r in results if r.passed)
    lines.append(f"{passed}/{len(results)} passed, {len(results) - passed} failed")
    return "\n".join(lines)


def _default_run_stamp() -> str:
    """The REAL wall-clock time this eval actually executed, for the result filename -- distinct
    from `today`/`EVAL_TODAY`, which is the FIXED simulated date the scenarios pretend is "today"
    (seeded expenses, the agent's own clock, month-relative wording). Two runs on the same real
    day, or even two runs of the very same `--repeat 5` command minutes apart, get different
    filenames, so a later run never silently overwrites an earlier one's evidence -- which is
    exactly what happened before this existed: every run wrote to the same
    `..._2026-09-15_consistency_5x.json` regardless of when it was actually run, because that
    stamp came from `EVAL_TODAY`, not the real clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def run_all(
    *,
    chat_factory: Callable[[], ChatLLM],
    write_llm_factory: Callable[[], LLMClient] | None = None,
    datasets_dir: Path = DATASETS_DIR,
    results_dir: Path = RESULTS_DIR,
    prices_path: Path | None = None,
    today: date = EVAL_TODAY,
    run_label: str | None = None,
    prompt_version: str = "agent_v4",
    run_stamp: str | None = None,
) -> Path:
    """Load the cases IN FILE ORDER and run every one with `run_case` (agent prompt
    `prompt_version`, default "agent_v4", the current runtime default), calling `chat_factory()`
    and `write_llm_factory()`
    exactly ONCE PER CASE (a fresh client from each factory per case, so one case's failure
    never taints another's token/cost accounting; `write_llm_factory` defaults to
    `chat_factory` when omitted, i.e. one real client for both roles), and write
    `<results_dir>/agent_<model>_<prompt_version>_<run_stamp>[_<run_label>].json` (creating the
    folder) plus the gallery at the same stem with `_gallery.md`. `run_stamp` (default
    `_default_run_stamp()`: the real UTC time of this call, "YYYY-MM-DDTHHMMSSZ") is what makes
    the filename unique per actual execution -- it is NOT `today`/`EVAL_TODAY`, which stays the
    fixed simulated scenario date and never appears in the filename at all any more. `run_label`,
    when given, is appended to BOTH file stems (e.g. "r1") so the several runs inside one
    `run_consistency` call (which share one `run_stamp`) don't collide with each other either.
    Including the prompt prevents one version's evaluation from overwriting another version's
    evidence. Returns the result file's path.

    The JSON has: "date" (the simulated `today`, unchanged meaning), "run_at" (`run_stamp`: when
    this actually ran), "model" (the first call's model), "prompt_version", "prices"/
    "prices_sha" (as in `run_evals.run_split`), "n_cases", "report" (`build_report`'s dict),
    and "cases": {case_id: {"message", "sender", "outcome_kind", "passed", "failures",
    "reply_text", "tool_calls", "cost_usd"}}."""
    cases = load_cases(datasets_dir)
    write_factory = write_llm_factory or chat_factory
    clock = lambda: datetime.combine(today, time(12, 0), tzinfo=timezone.utc)  # noqa: E731
    run_stamp = run_stamp or _default_run_stamp()
    results: list[CaseResult] = []
    model: str | None = None
    for case in cases:
        chat = chat_factory()
        write_llm = write_factory()
        result = run_case(case, chat=chat, write_llm=write_llm, clock=clock, prompt_version=prompt_version)
        results.append(result)
        if model is None:
            model = getattr(chat, "model", None)

    by_case = {c.id: c for c in cases}
    result_json = {
        "date": today.isoformat(),
        "run_at": run_stamp,
        "model": model,
        "prompt_version": prompt_version,
        "prices": _price_record(model, prices_path),
        "prices_sha": prices_fingerprint(prices_path),
        "n_cases": len(cases),
        "report": build_report(results),
        "cases": {
            r.case_id: {
                "message": by_case[r.case_id].message,
                "sender": next(m.name for m in by_case[r.case_id].members if m.id == by_case[r.case_id].sender_id),
                "outcome_kind": by_case[r.case_id].outcome["kind"],
                "passed": r.passed,
                "failures": r.failures,
                "reply_text": r.reply_text,
                "tool_calls": r.tool_calls,
                "cost_usd": format(r.cost_usd, "f"),
            }
            for r in results
        },
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    safe_model = (model or "unknown").replace("/", "_")
    safe_prompt = prompt_version.replace("/", "_")
    stem = f"agent_{safe_model}_{safe_prompt}_{run_stamp}" + (f"_{run_label}" if run_label else "")
    path = results_dir / f"{stem}.json"
    path.write_text(json.dumps(result_json, ensure_ascii=False, indent=2), encoding="utf-8")
    gallery_path = results_dir / f"{stem}_gallery.md"
    gallery_path.write_text(build_gallery(cases, results), encoding="utf-8")
    return path


def _price_record(model: str | None, prices_path: Path | None) -> dict | None:
    price = price_for(model, prices_path) if model else None
    if price is None:
        return None
    return {"in": format(price.in_per_mtok, "f"), "out": format(price.out_per_mtok, "f")}


def run_consistency(
    *,
    chat_factory: Callable[[], ChatLLM],
    write_llm_factory: Callable[[], LLMClient] | None = None,
    runs: int = 5,
    datasets_dir: Path = DATASETS_DIR,
    results_dir: Path = RESULTS_DIR,
    prices_path: Path | None = None,
    today: date = EVAL_TODAY,
    prompt_version: str = "agent_v4",
    run_stamp: str | None = None,
) -> Path:
    """Calls `run_all` `runs` times (agent prompt `prompt_version`, default "agent_v4", the
    current runtime default; `run_label` "r1".."r<runs>",
    so every individual run's own
    result file and gallery are kept, independently inspectable), all sharing ONE `run_stamp`
    (default `_default_run_stamp()`, computed once here: the real UTC time of THIS call), and
    writes ONE combined report: `<results_dir>/agent_<model>_<prompt_version>_<run_stamp>_consistency_<runs>x.json`,
    plus a combined summary at the same stem with `_gallery.md` (`build_consistency_summary`).
    `run_stamp` -- not `today`/`EVAL_TODAY`, the fixed simulated scenario date, which never
    appears in the filename -- is what makes each real execution's files unique, so running this
    again (e.g. after a prompt edit) never silently overwrites the previous evidence. Returns the
    combined report's path.

    The JSON has: "date" (the simulated `today`), "run_at" (`run_stamp`), "model", "runs",
    "n_cases", "total_cost_usd" (summed over every run),
    "per_case": {case_id: {"outcome_kind", "passed": k, "total": runs, "pass_rate": k / runs}},
    in dataset file order, and "overall_pass_rate" (total passes over `n_cases * runs`).
    `runs` must be >= 1; raises ValueError otherwise, before any call is made."""
    if runs < 1:
        raise ValueError("runs must be >= 1")
    run_stamp = run_stamp or _default_run_stamp()
    cases = load_cases(datasets_dir)
    per_case = {c.id: {"outcome_kind": c.outcome["kind"], "passed": 0, "total": 0} for c in cases}
    total_cost = Decimal(0)
    model: str | None = None
    for i in range(1, runs + 1):
        path = run_all(
            chat_factory=chat_factory, write_llm_factory=write_llm_factory, datasets_dir=datasets_dir,
            results_dir=results_dir, prices_path=prices_path, today=today, run_label=f"r{i}",
            prompt_version=prompt_version, run_stamp=run_stamp,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        model = model or data["model"]
        total_cost += Decimal(data["report"]["total_cost_usd"])
        for case_id, case_data in data["cases"].items():
            per_case[case_id]["total"] += 1
            if case_data["passed"]:
                per_case[case_id]["passed"] += 1
    for entry in per_case.values():
        entry["pass_rate"] = entry["passed"] / entry["total"]
    total_passes = sum(entry["passed"] for entry in per_case.values())

    result_json = {
        "date": today.isoformat(),
        "run_at": run_stamp,
        "model": model,
        "prompt_version": prompt_version,
        "runs": runs,
        "n_cases": len(cases),
        "per_case": per_case,
        "overall_pass_rate": total_passes / (len(cases) * runs),
        "total_cost_usd": format(total_cost, "f"),
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    safe_model = (model or "unknown").replace("/", "_")
    safe_prompt = prompt_version.replace("/", "_")
    stem = f"agent_{safe_model}_{safe_prompt}_{run_stamp}_consistency_{runs}x"
    combined_path = results_dir / f"{stem}.json"
    combined_path.write_text(json.dumps(result_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (results_dir / f"{stem}_gallery.md").write_text(build_consistency_summary(cases, per_case, runs), encoding="utf-8")
    return combined_path


def build_consistency_summary(cases: list[Case], per_case: dict, runs: int) -> str:
    """Markdown, one line per case in dataset order: "<id> (<outcome_kind>): <k>/<runs> passed",
    plus a final line "<n scenarios that passed every run>/<n_cases> scenarios passed all
    <runs> runs"."""
    lines = []
    all_pass = 0
    for case in cases:
        info = per_case[case.id]
        lines.append(f"{case.id} ({info['outcome_kind']}): {info['passed']}/{runs} passed")
        if info["passed"] == runs:
            all_pass += 1
    lines.append(f"{all_pass}/{len(cases)} scenarios passed all {runs} runs")
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    chat_factory: Callable[[], ChatLLM] | None = None,
    write_llm_factory: Callable[[], LLMClient] | None = None,
    today: date = EVAL_TODAY,
    run_stamp: str | None = None,
) -> int:
    """CLI: `--yes` (default: dry run), `--repeat N` (default 1: the standard way to run this
    for real is `--repeat 5`, for a consistency read rather than a single pass/fail).

    The agent's own model (and, since `write_llm_factory` defaults to `chat_factory`, the
    extractor's too, unless a separate `write_llm_factory` is given) is `AGENT_MODEL`, read at
    CALL time via `config.require`, never hardcoded: a missing/blank value raises ConfigError
    before anything else happens, on both the dry-run and `--yes` paths.

    Without `--yes`: load the cases (DatasetError -> print + return 2), print
    "<n> cases, up to <estimate.calls> calls, model <name>" (the estimate scaled by `--repeat`)
    and the rough cost estimate (or "unknown" when the model is not in config/prices.json),
    print "Dry run only. Add --yes to make the real calls.", and return 0 WITHOUT building any
    client or calling any LLM (neither factory is ever called on this path, and AGENT_MODEL is
    still read, so a missing one is caught even on a dry run).
    With `--yes` and `--repeat 1` (the default): build the client(s) (`chat_factory`/
    `write_llm_factory`, default both `lambda: OpenAIClient.from_env(model=<AGENT_MODEL>)`), run
    `run_all`, print the pass/fail counts, the total cost and the result file path, and return 0.
    With `--repeat N > 1`: run `run_consistency(runs=N, ...)` instead, print
    `build_consistency_summary`'s text and the combined report's path, and return 0.
    `main` reads the module-level DATASETS_DIR/RESULTS_DIR at CALL time (tests monkeypatch
    them, same as `run_evals.main`)."""
    parser = argparse.ArgumentParser(prog="python -m tests.llm_evals.run_agent_eval")
    parser.add_argument("--yes", action="store_true", help="really call the LLM (costs money)")
    parser.add_argument("--repeat", type=int, default=1, help="run the whole suite N times for a consistency read")
    parser.add_argument("--prompt", default="agent_v4", help="agent prompt version to use (default: agent_v4)")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(DATASETS_DIR)
    except DatasetError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    model_name = require("AGENT_MODEL")
    if not args.yes:
        price = price_for(model_name)
        estimate = estimate_run(
            cases, price_in_per_mtok=price.in_per_mtok if price else None,
            price_out_per_mtok=price.out_per_mtok if price else None,
        )
        print(f"{len(cases)} cases, up to {estimate.calls * args.repeat} calls, model {model_name}")
        if estimate.cost_usd_low is None:
            print(f"estimated cost: unknown (model {model_name!r} is not listed in config/prices.json)")
        else:
            print(f"estimated cost (rough): ${estimate.cost_usd_low * args.repeat:.4f} to "
                  f"${estimate.cost_usd_high * args.repeat:.4f}")
        print("Dry run only. Add --yes to make the real calls.")
        return 0

    default_factory = lambda: OpenAIClient.from_env(model=model_name)  # noqa: E731
    if args.repeat > 1:
        path = run_consistency(
            chat_factory=chat_factory or default_factory, write_llm_factory=write_llm_factory,
            runs=args.repeat, datasets_dir=DATASETS_DIR, results_dir=RESULTS_DIR, today=today,
            prompt_version=args.prompt, run_stamp=run_stamp,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        per_case_cases = [c for c in load_cases(DATASETS_DIR)]
        print(build_consistency_summary(per_case_cases, data["per_case"], args.repeat))
        print(f"cost ${data['total_cost_usd']}")
        print(f"result file: {path}")
        return 0

    path = run_all(
        chat_factory=chat_factory or default_factory,
        write_llm_factory=write_llm_factory,
        datasets_dir=DATASETS_DIR, results_dir=RESULTS_DIR, today=today,
        prompt_version=args.prompt, run_stamp=run_stamp,
    )
    report = json.loads(path.read_text(encoding="utf-8"))["report"]
    print(f"{report['passed']} passed, {report['failed']} failed | cost ${report['total_cost_usd']}")
    print(f"result file: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
