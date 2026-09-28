"""LLM eval harness: `python -m tests.llm_evals.run_evals --split dev|test [--prompt extract_v1]`.

Real LLM calls happen ONLY in `main()` with `--yes`; everything else is pure or takes an
injected `LLMClient` (unit tests use tests.fakes.FakeLLM). The datasets are verified by the
human and are never edited by code. Nothing here reads .env or prints secrets.

Dataset files (in `datasets/`): `extraction_dev.jsonl`, `extraction_test.jsonl` and
`rosters.json` (roster name -> [{"id": int, "name": str}, ...]). One JSON object per line:
  {"id", "split": "dev"|"test", "roster": "A", "sender_id": 1, "message": "...",
   "source": "user", "verified": true,
   "expected": {...an ExtractedExpense payload, including "amount_in_words"...},
   "scoring": {"subcategory_any_of": [..] | null, "expect_low": bool,
               "score_confidence": bool, "score_description": bool},
   "notes": "..."}
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Literal

from pydantic import ValidationError

from splitbot.config import optional, price_for, prices_fingerprint
from splitbot.llm.client import LLMClient, OpenAIClient
from splitbot.llm.extractor import Extraction, extract, load_prompt
from splitbot.models import (
    Ambiguous,
    Confidence,
    ExtractedExpense,
    KnownMember,
    Member,
    MessageType,
    Participants,
    Subcategory,
    category_of,
)
from splitbot.money import parse_amount
from splitbot.validation import check_grounding, check_members, normalize, resolve_participants

DATASETS_DIR = Path(__file__).parent / "datasets"
RESULTS_DIR = Path(__file__).parent / "results"
RUNS_PER_CASE = 3


class DatasetError(ValueError):
    """A dataset row or file is not usable (the data is never "fixed" by code)."""


class LeakageError(ValueError):
    """An eval message appears inside the prompt file (few-shot leakage)."""


class ExpectedExtraction(ExtractedExpense):
    """Historical action label plus the current storage-only extraction fields."""

    message_type: MessageType


@dataclass(frozen=True)
class Case:
    id: str
    split: str  # "dev" | "test"
    roster: str  # key in rosters.json
    sender_id: int
    message: str
    members: list[Member]  # the roster's members
    expected: ExpectedExtraction  # historical label + human-verified storage fields
    accepted_subcategories: list[Subcategory] | None  # scoring.subcategory_any_of
    expect_low: bool

    @property
    def language(self) -> Literal["he", "en", "mixed", "other"]:
        """From the letters in `message`: Hebrew letters only -> "he", Latin letters only ->
        "en", both -> "mixed", neither -> "other"."""
        return language_of(self.message)

    @property
    def expected_is_ask(self) -> bool:
        """True when `check_members` / `check_grounding` (author = sender_id) find issues in
        the EXPECTED answer itself, i.e. the pipeline is supposed to ask (an ambiguous member,
        an amount code rejects). Such rows are scored on `asks` instead of grounding."""
        issues = check_members(self.expected, [m.id for m in self.members])
        issues += check_grounding(self.expected, self.message, author_id=self.sender_id)
        return bool(issues)


def language_of(message: str) -> Literal["he", "en", "mixed", "other"]:
    """Same rule as `Case.language`, for a bare message."""
    hebrew = any("\u0590" <= c <= "\u05ff" for c in message)
    latin = re.search(r"[A-Za-z]", message) is not None
    if hebrew and latin:
        return "mixed"
    return "he" if hebrew else "en" if latin else "other"


def load_cases(split: Literal["dev", "test"], datasets_dir: Path = DATASETS_DIR) -> list[Case]:
    """Read `extraction_<split>.jsonl` + `rosters.json` from `datasets_dir`, in file order.

    Raises DatasetError when: a row is not `"verified": true`; its roster is unknown; its
    `expected` does not validate as `ExtractedExpense`; an id is duplicated inside the file;
    a row's `split` field differs from the requested split; or (when the other split's file
    exists in `datasets_dir`) an id or a message text appears in both splits.
    """
    rosters = _read_json(datasets_dir / "rosters.json")
    rows = _read_rows(datasets_dir / f"extraction_{split}.jsonl")
    cases: list[Case] = []
    seen: set[str] = set()
    for row in rows:
        case_id = row.get("id", "?")
        if case_id in seen:
            raise DatasetError(f"{case_id}: duplicate id in the {split} file")
        seen.add(case_id)
        cases.append(_to_case(row, split, rosters))

    other = "test" if split == "dev" else "dev"
    other_path = datasets_dir / f"extraction_{other}.jsonl"
    if other_path.exists():
        other_rows = _read_rows(other_path)
        other_ids = {r.get("id") for r in other_rows}
        other_texts = {normalize(str(r.get("message", ""))) for r in other_rows}
        for case in cases:
            if case.id in other_ids or normalize(case.message) in other_texts:
                raise DatasetError(f"{case.id}: also appears in the {other} split")
    return cases


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"cannot read {path.name}: {type(exc).__name__}") from None


def _read_rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"cannot read {path.name}: {type(exc).__name__}") from None
    rows = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise DatasetError(f"{path.name} line {number}: not valid JSON") from None
    return rows


def _to_case(row: dict, split: str, rosters: dict) -> Case:
    case_id = row.get("id", "?")
    if row.get("verified") is not True:
        raise DatasetError(f"{case_id}: not verified by the human")
    if row.get("split") != split:
        raise DatasetError(f"{case_id}: split is {row.get('split')!r}, expected {split!r}")
    if row.get("roster") not in rosters:
        raise DatasetError(f"{case_id}: unknown roster {row.get('roster')!r}")
    try:
        expected = ExpectedExtraction.model_validate(row["expected"])
        scoring = row["scoring"]
        any_of = scoring.get("subcategory_any_of")
        return Case(
            id=case_id,
            split=split,
            roster=row["roster"],
            sender_id=row["sender_id"],
            message=row["message"],
            members=[Member(**m) for m in rosters[row["roster"]]],
            expected=expected,
            accepted_subcategories=[Subcategory(x) for x in any_of] if any_of else None,
            expect_low=bool(scoring.get("expect_low", False)),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise DatasetError(f"{case_id}: the row does not match the model ({type(exc).__name__})") from None


# --- scoring ----------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    field: str
    expected: str  # short readable text
    actual: str  # short readable text


@dataclass(frozen=True)
class RunScore:
    fields: dict[str, bool]  # ONLY the scored fields (a field that is not scored is absent)
    failures: list[Failure]  # one per False field
    full_correct: bool  # all(fields.values())
    predicted_type: str | None  # the reply's message_type; None when nothing could be parsed
    grounding_failure: bool | None  # None on ask rows and when nothing was parsed
    signature: tuple  # canonical, hashable: equal for two runs that gave the same result
    cost_usd: Decimal | None  # sum over extraction.llm_calls; None if any call's cost is None
    latency_s: float  # sum over extraction.llm_calls
    n_calls: int = 1  # len(extraction.llm_calls): the LLM calls behind this run (1, or 2 after a retry)


def score_run(case: Case, extraction: Extraction) -> RunScore:
    """Score one extraction against the human-verified `case.expected`.

    If `extraction.expense is None` (nothing parsed): every scored field is False,
    `predicted_type` is None, `grounding_failure` is None. Otherwise, scored fields are:
      - "type": always. actual.message_type == expected.message_type.
      - "amount": when expected.amount is set. Equal after `money.parse_amount` (so "412,50"
        equals "412.50"); if either value cannot be parsed, equal after `.strip()`.
      - "amount_in_words": on EVERY row where expected.amount is set (to catch a wrong flag):
        actual.amount_in_words == expected.amount_in_words.
      - "currency": when expected.currency is set. Same value.
      - "payer": when expected.payer is set. Same member (known: same id; ambiguous: same set
        of candidate ids). Source and evidence are not compared.
      - "participants": when expected.participants is set. Compared by MEANING: both are
        resolved with `validation.resolve_participants(author=case.sender_id, member ids of
        the roster)` and the resulting lists must be equal (so `only [4]` equals `only [1,4]`
        for sender 1). If either side has an ambiguous reference or cannot be resolved,
        compare `only` and `exclude` as sets of member references instead.
      - "exact_amounts": on EVERY row whose expected message_type is "new" (an invented one
        must fail): the set of (member reference, parsed amount) pairs must be equal;
        None/empty on both sides is equal.
      - "subcategory": when expected.subcategory is set. Actual must be in
        `case.accepted_subcategories` (or equal to expected when that is None).
      - "category": when expected.subcategory is set (the MAIN category, derived in code with
        `models.category_of`): the actual subcategory's main category must be one of the main
        categories of the accepted subcategories (so restaurant vs delivery are both
        "eating_out"). Field order: right after "subcategory".
      - "refers_to": when expected.refers_to is set. Equal after `validation.normalize`.
      - "confidence": on EVERY row. If `case.expect_low`: actual confidence must be low;
        otherwise it must NOT be low. High vs medium is never scored. Description is never
        scored.
      - "asks": ONLY when `case.expected_is_ask`: `extraction.status == "needs_clarification"`.
    `n_calls` = `len(extraction.llm_calls)`.
    `grounding_failure` = `bool(extraction.issues)` for rows that are not ask rows and where an
    expense was parsed; None otherwise. `signature` covers status, type, parsed amount (or
    stripped text), in-words flag, currency, payer, participants (resolved when possible),
    exact amounts, subcategory, normalized refers_to and whether confidence is low.
    """
    actual = extraction.expense
    fields: dict[str, bool] = {}
    failures: list[Failure] = []
    for name, ok, expected_text, actual_text in _checks(case, extraction):
        fields[name] = ok
        if not ok:
            failures.append(Failure(name, expected_text, actual_text))

    calls = extraction.llm_calls
    cost = None if any(c.cost_usd is None for c in calls) else sum((c.cost_usd for c in calls), Decimal(0))
    return RunScore(
        fields=fields,
        failures=failures,
        full_correct=all(fields.values()),
        predicted_type=(
            getattr(actual, "message_type", None).value
            if getattr(actual, "message_type", None) is not None
            else extraction.legacy_message_type
        ),
        grounding_failure=None if actual is None or case.expected_is_ask else bool(extraction.issues),
        signature=_signature(case, extraction),
        cost_usd=cost,
        latency_s=sum(c.latency_s for c in calls),
        n_calls=len(calls),
    )


def _amount_key(value: str) -> tuple:
    try:
        return ("minor", parse_amount(value))
    except ValueError:
        return ("text", value.strip())


def _ref_key(ref) -> tuple:
    return ("known", ref.id) if isinstance(ref, KnownMember) else ("ambiguous", frozenset(ref.candidates))


def _ref_text(ref) -> str:
    return f"member {ref.id}" if isinstance(ref, KnownMember) else f"ambiguous {sorted(ref.candidates)}"


def _participants_view(p: Participants, case: Case) -> tuple[tuple | None, tuple]:
    """(resolved member ids or None, raw form). Raw = the `only` / `exclude` reference sets."""
    raw = (
        frozenset(_ref_key(r) for r in p.only) if p.only is not None else None,
        frozenset(_ref_key(r) for r in p.exclude),
    )
    refs = (p.only or []) + p.exclude
    if any(isinstance(r, Ambiguous) for r in refs):
        return None, raw
    try:
        return tuple(resolve_participants(p, case.sender_id, [m.id for m in case.members])), raw
    except ValueError:
        return None, raw


def _participants_text(p: Participants, case: Case) -> str:
    resolved, _ = _participants_view(p, case)
    if resolved is not None:
        return f"{list(resolved)}"
    only = "none" if p.only is None else [_ref_text(r) for r in p.only]
    return f"only={only} exclude={[_ref_text(r) for r in p.exclude]}"


def _exact_key(expense: ExtractedExpense) -> frozenset:
    return frozenset((_ref_key(x.member), _amount_key(x.amount)) for x in expense.exact_amounts or [])


def _exact_text(expense: ExtractedExpense) -> str:
    return ", ".join(f"{_ref_text(x.member)}={x.amount}" for x in expense.exact_amounts or []) or "none"


def _checks(case: Case, extraction: Extraction) -> list[tuple[str, bool, str, str]]:
    """(field, correct, expected text, actual text) for every scored field, in a fixed order."""
    exp, act = case.expected, extraction.expense
    nothing = "(nothing parsed)"
    out: list[tuple[str, bool, str, str]] = []

    def add(field: str, ok: bool, expected_text: str, actual_text: str) -> None:
        out.append((field, act is not None and ok, expected_text, actual_text if act is not None else nothing))

    predicted_type = (
        getattr(act, "message_type", None).value
        if getattr(act, "message_type", None) is not None
        else extraction.legacy_message_type
    )
    # Action accuracy is retained only for archived v1/v2 evals. The v3 runtime contract
    # measures action choice in Agent evals, not in extraction evals.
    if predicted_type is not None or extraction.prompt_version in {"extract_v1", "extract_v2"}:
        add("type", predicted_type == exp.message_type.value, exp.message_type.value,
            predicted_type or nothing)
    if exp.amount is not None:
        got = act.amount.value if act and act.amount else "none"
        add("amount", bool(act and act.amount) and _amount_key(got) == _amount_key(exp.amount.value),
            exp.amount.value, got)
        add("amount_in_words", bool(act) and act.amount_in_words == exp.amount_in_words,
            str(exp.amount_in_words), str(act.amount_in_words) if act else nothing)
    if exp.currency is not None:
        got = act.currency.value.value if act and act.currency else "none"
        add("currency", got == exp.currency.value.value, exp.currency.value.value, got)
    if exp.payer is not None:
        ok = bool(act and act.payer) and _ref_key(act.payer.value) == _ref_key(exp.payer.value)
        add("payer", ok, _ref_text(exp.payer.value), _ref_text(act.payer.value) if act and act.payer else "none")
    if exp.participants is not None:
        ok = False
        if act and act.participants:
            e_res, e_raw = _participants_view(exp.participants.value, case)
            a_res, a_raw = _participants_view(act.participants.value, case)
            ok = e_res == a_res if (e_res is not None and a_res is not None) else e_raw == a_raw
        add("participants", ok, _participants_text(exp.participants.value, case),
            _participants_text(act.participants.value, case) if act and act.participants else "none")
    if exp.message_type == MessageType.new:
        add("exact_amounts", bool(act) and _exact_key(act) == _exact_key(exp), _exact_text(exp),
            _exact_text(act) if act else nothing)
    if exp.subcategory is not None:
        accepted = case.accepted_subcategories or [exp.subcategory]
        add("subcategory", bool(act) and act.subcategory in accepted, "/".join(str(x) for x in accepted),
            act.subcategory.value if act and act.subcategory else "none")
        accepted_categories = {category_of(x) for x in accepted}
        actual_category = category_of(act.subcategory) if act and act.subcategory else None
        add("category", actual_category in accepted_categories, "/".join(sorted(str(c) for c in accepted_categories)),
            str(actual_category) if actual_category else "none")
    if exp.refers_to is not None:
        ok = bool(act and act.refers_to) and normalize(act.refers_to.value) == normalize(exp.refers_to.value)
        add("refers_to", ok, exp.refers_to.value, act.refers_to.value if act and act.refers_to else "none")
    is_low = bool(act) and act.confidence == Confidence.low
    add("confidence", bool(act) and is_low == case.expect_low, "low" if case.expect_low else "not low",
        act.confidence.value if act else nothing)
    if case.expected_is_ask:
        add("asks", extraction.status == "needs_clarification", "needs_clarification", extraction.status)
    return out


def _signature(case: Case, extraction: Extraction) -> tuple:
    act = extraction.expense
    if act is None:
        return ("unparsed", extraction.status)
    resolved, raw = _participants_view(act.participants.value, case) if act.participants else (None, None)
    return (
        extraction.status,
        _amount_key(act.amount.value) if act.amount else None,
        act.amount_in_words,
        act.currency.value if act.currency else None,
        _ref_key(act.payer.value) if act.payer else None,
        resolved if resolved is not None else raw,
        _exact_key(act),
        act.subcategory,
        normalize(act.refers_to.value) if act.refers_to else None,
        act.confidence == Confidence.low,
    )


# --- report -----------------------------------------------------------------


def build_report(cases: list[Case], runs: dict[str, list[RunScore]]) -> dict:
    """`runs` maps case id -> its RunScores (normally 3). Returns a JSON-serializable dict:

      {"overall": SECTION,
       "by_language": {"he": SECTION, "en": SECTION, ...},   # only languages present
       "per_case": {case_id: {"language": str, "runs": int, "full_correct_runs": int,
                              "consistent": bool, "cost_usd": str | None,
                              "latency_s": float}},          # latency = mean over its runs
       "failures": [{"case_id", "run": int (1-based), "field", "expected", "actual"}, ...]}

    SECTION = {"n_cases": int, "n_runs": int,
               "type": {"accuracy": float,
                        "false_expense_rate": float | None,   # chat rows whose predicted_type
                                                              # is not "chat" (incl. None)
                        "missed_expense_rate": float | None}, # non-chat rows predicted "chat"
               "extraction": {"full_case_accuracy": float,
                              "per_field_accuracy": {field: float}},  # only fields scored
               "grounding_failure_rate": float | None,  # over runs whose value is not None
               "consistency": float,   # share of CASES whose runs all have equal `signature`
               "cost_usd": str | None,  # total as a decimal string; None if any run's is None
               "n_calls": int,  # sum of the runs' n_calls
               "cost_usd_per_call": str | None,  # total cost / n_calls
               "cost_usd_per_correct": str | None,  # total cost / number of full_correct runs
               "latency_s": {"p50": float, "p95": float}}  # nearest-rank over per-run latency
    Accuracies and rates are pooled over all runs, floats in [0, 1]; a rate is None when its
    denominator is 0. `failures` is sorted by case id, then run number. The two per-cost
    figures are None when the total cost is unknown (None), when n_calls is 0 (per call) or when
    no run is fully correct (per correct); otherwise the division rounded to 8 decimal places
    (ROUND_HALF_UP) and written as a plain decimal string.
    """
    by_id = {c.id: c for c in cases}
    ids = [c.id for c in cases if c.id in runs]
    languages = sorted({by_id[i].language for i in ids})
    per_case = {}
    for case_id in ids:
        scores = runs[case_id]
        per_case[case_id] = {
            "language": by_id[case_id].language,
            "runs": len(scores),
            "full_correct_runs": sum(r.full_correct for r in scores),
            "consistent": len({r.signature for r in scores}) == 1,
            "cost_usd": _cost_text(scores),
            "latency_s": sum(r.latency_s for r in scores) / len(scores),
        }
    failures = [
        {"case_id": case_id, "run": number, "field": f.field, "expected": f.expected, "actual": f.actual}
        for case_id in sorted(ids)
        for number, score in enumerate(runs[case_id], start=1)
        for f in score.failures
    ]
    return {
        "overall": _section(ids, by_id, runs),
        "by_language": {lang: _section([i for i in ids if by_id[i].language == lang], by_id, runs) for lang in languages},
        "per_case": per_case,
        "failures": failures,
    }


def _mean(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _total_cost(scores: list[RunScore]) -> Decimal | None:
    if any(r.cost_usd is None for r in scores):
        return None
    return sum((r.cost_usd for r in scores), Decimal(0))


def _cost_text(scores: list[RunScore]) -> str | None:
    total = _total_cost(scores)
    return None if total is None else format(total, "f")


def _quotient(total: Decimal | None, count: int) -> str | None:
    """total / count rounded to 8 decimals (half up), as a plain decimal string."""
    if total is None or count == 0:
        return None
    return format((total / count).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    rank = -(-percent * len(ordered) // 100)  # ceil(percent * n / 100), nearest-rank
    return ordered[max(rank, 1) - 1]


def _section(ids: list[str], by_id: dict[str, Case], runs: dict[str, list[RunScore]]) -> dict:
    pairs = [(by_id[i], r) for i in ids for r in runs[i]]
    chat = [r for c, r in pairs if c.expected.message_type == MessageType.chat]
    other = [r for c, r in pairs if c.expected.message_type != MessageType.chat]
    per_field: dict[str, list[bool]] = {}
    for _, r in pairs:
        for name, ok in r.fields.items():
            per_field.setdefault(name, []).append(ok)
    grounding = [r.grounding_failure for _, r in pairs if r.grounding_failure is not None]
    scores = [r for _, r in pairs]
    latencies = [r.latency_s for r in scores]
    return {
        "n_cases": len(ids),
        "n_runs": len(pairs),
        "type": {
            "accuracy": _mean([r.fields["type"] for r in scores if "type" in r.fields]),
            "false_expense_rate": _mean([r.predicted_type != "chat" for r in chat]),
            "missed_expense_rate": _mean([r.predicted_type == "chat" for r in other]),
        },
        "extraction": {
            "full_case_accuracy": _mean([r.full_correct for r in scores]),
            "per_field_accuracy": {name: _mean(flags) for name, flags in per_field.items()},
        },
        "grounding_failure_rate": _mean(grounding),
        "consistency": _mean([len({r.signature for r in runs[i]}) == 1 for i in ids]),
        "cost_usd": _cost_text(scores),
        "n_calls": sum(r.n_calls for r in scores),
        "cost_usd_per_call": _quotient(_total_cost(scores), sum(r.n_calls for r in scores)),
        "cost_usd_per_correct": _quotient(_total_cost(scores), sum(r.full_correct for r in scores)),
        "latency_s": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95)} if latencies else {"p50": 0.0, "p95": 0.0},
    }


# --- prompt leakage, estimate, run ------------------------------------------


def check_no_leakage(cases: list[Case], prompt_text: str) -> None:
    """Raise LeakageError (naming the case ids) if any case message, after
    `validation.normalize`, appears inside the normalized prompt text."""
    prompt = normalize(prompt_text)
    leaked = [c.id for c in cases if normalize(c.message) and normalize(c.message) in prompt]
    if leaked:
        raise LeakageError(f"eval messages found inside the prompt (few-shot leakage): {', '.join(leaked)}")


@dataclass(frozen=True)
class Estimate:
    calls: int  # cases * runs
    max_calls: int  # 2 * calls (every reply retried once)
    cost_usd_low: Decimal | None  # None when a price is None
    cost_usd_high: Decimal | None


def estimate_run(
    cases: list[Case],
    prompt_text: str,
    *,
    runs: int,
    price_in_per_mtok: Decimal | None,
    price_out_per_mtok: Decimal | None,
) -> Estimate:
    """A ROUGH estimate (character-count heuristics, not real tokenization): input tokens per
    call between len(text)/4 and len(text)/2.5 (prompt + the user turn), output between 250
    and 450 tokens. `cost_usd_low` uses `calls` and the low numbers, `cost_usd_high` uses
    `max_calls` and the high numbers. Prices are per 1M tokens."""
    calls = len(cases) * runs
    max_calls = 2 * calls
    if price_in_per_mtok is None or price_out_per_mtok is None:
        return Estimate(calls, max_calls, None, None)
    chars = sum(len(prompt_text) + len(_user_turn(c)) for c in cases) * runs
    million = Decimal(1_000_000)
    low = (Decimal(chars) / 4 * price_in_per_mtok + Decimal(250 * calls) * price_out_per_mtok) / million
    # every reply retried once: twice the input, and the retry turn is a little longer
    high = (Decimal(chars) * 2 / Decimal("2.5") * price_in_per_mtok + Decimal(450 * max_calls) * price_out_per_mtok) / million
    return Estimate(calls, max_calls, low, high)


def _user_turn(case: Case) -> str:
    return json.dumps(
        {"members": [{"id": m.id, "name": m.name} for m in case.members], "sender_id": case.sender_id, "message": case.message},
        ensure_ascii=False,
    )


def run_split(
    split: Literal["dev", "test"],
    *,
    prompt_version: str,
    llm: LLMClient,
    runs: int = RUNS_PER_CASE,
    datasets_dir: Path = DATASETS_DIR,
    results_dir: Path = RESULTS_DIR,
    prices_path: Path | None = None,
    today: date,
) -> Path:
    """Load the cases, load the prompt (`extractor.load_prompt`), run `check_no_leakage`
    BEFORE any LLM call, then for every case and every run call `extractor.extract(message,
    sender_id=..., members=..., llm=llm, prompt_version=...)`, score it, build the report, and
    write `<results_dir>/extraction_<model>_<prompt_version>_<split>_<today ISO>.json` (creating
    the folder; `<model>` is the first LLMResult's model with "/" replaced by "_").
    Returns that path. LLMError from the client propagates (no partial file).

    The JSON has: "prompt_version", "split", "date", "model" and "temperature" (taken from the
    first LLMResult; every result records them), "prices" ({"in": str, "out": str}, USD per 1M
    tokens, from `config.price_for(model, prices_path)`, or null when the model is not listed),
    "prices_sha" (`config.prices_fingerprint(prices_path)`), "cases" ({case_id: {"message": str,
    "roster": str, "sender_id": int}}: the case texts, so a result file is readable on its own),
    "runs_per_case", "n_cases", "report" (the
    build_report dict) and "runs": {case_id: [{"status", "issues", "cost_usd" (str|None),
    "latency_s", "input_tokens", "output_tokens", "cached_tokens" (sum over the run's LLM calls of
    the values that were reported; None when no call reported it), "extracted": <expense as JSON
    dict | None>}]}.
    """
    cases = load_cases(split, datasets_dir)
    check_no_leakage(cases, load_prompt(prompt_version))  # before any LLM call

    scores: dict[str, list[RunScore]] = {}
    raw: dict[str, list[dict]] = {}
    model = temperature = None
    for case in cases:
        scores[case.id], raw[case.id] = [], []
        for _ in range(runs):
            extraction = extract(
                case.message, sender_id=case.sender_id, members=case.members, llm=llm, prompt_version=prompt_version
            )
            score = score_run(case, extraction)
            scores[case.id].append(score)
            if model is None and extraction.llm_calls:
                model, temperature = extraction.llm_calls[0].model, extraction.llm_calls[0].temperature
            raw[case.id].append(
                {
                    "status": extraction.status,
                    "issues": extraction.issues,
                    "cost_usd": None if score.cost_usd is None else format(score.cost_usd, "f"),
                    "latency_s": score.latency_s,
                    "input_tokens": sum(c.input_tokens for c in extraction.llm_calls),
                    "output_tokens": sum(c.output_tokens for c in extraction.llm_calls),
                    "cached_tokens": _sum_reported([c.cached_tokens for c in extraction.llm_calls]),
                    "extracted": extraction.expense.model_dump(mode="json") if extraction.expense else None,
                }
            )
    result = {
        "prompt_version": prompt_version,
        "split": split,
        "date": today.isoformat(),
        "model": model,
        "temperature": temperature,
        "prices": _price_record(model, prices_path),
        "prices_sha": prices_fingerprint(prices_path),
        "cases": {c.id: {"message": c.message, "roster": c.roster, "sender_id": c.sender_id} for c in cases},
        "runs_per_case": runs,
        "n_cases": len(cases),
        "report": build_report(cases, scores),
        "runs": raw,
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    safe_model = (model or "unknown").replace("/", "_")
    path = results_dir / f"extraction_{safe_model}_{prompt_version}_{split}_{today.isoformat()}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(
    argv: list[str] | None = None,
    *,
    llm_factory: Callable[[], LLMClient] | None = None,
    today: Callable[[], date] = date.today,
) -> int:
    """CLI: `--split dev|test` (required), `--prompt` (default "extract_v1"), `--model` (the
    OpenAI model; default OPENAI_MODEL), `--runs` (default 3), `--yes`.

    Without `--yes` it is a DRY RUN: load the cases, run the leakage check, print the number
    of calls (and the rough cost estimate when the model (`--model`, else OPENAI_MODEL) is
    listed in the price file, else "unknown") and return 0, WITHOUT calling `llm_factory` or
    any LLM. With `--yes` it builds the client with `llm_factory()` (called with no arguments;
    `--model` only affects the default factory, `OpenAIClient.from_env(model=...)`), runs
    `run_split`, prints a short summary of the overall metrics
    and the result file path, and returns 0. DatasetError / LeakageError: print the message
    and return 2. `main` reads the module-level DATASETS_DIR and RESULTS_DIR at CALL time (tests
    monkeypatch them), and passes them to `load_cases` / `run_split`.
    """
    parser = argparse.ArgumentParser(prog="python -m tests.llm_evals.run_evals")
    parser.add_argument("--split", required=True, choices=["dev", "test"])
    parser.add_argument("--prompt", default="extract_v1")
    parser.add_argument("--model", default=None, help="OpenAI model (default: OPENAI_MODEL)")
    parser.add_argument("--runs", type=int, default=RUNS_PER_CASE)
    parser.add_argument("--yes", action="store_true", help="really call the LLM (costs money)")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.split, DATASETS_DIR)
        prompt_text = load_prompt(args.prompt)
        check_no_leakage(cases, prompt_text)
        if not args.yes:
            model_name = args.model or optional("OPENAI_MODEL")
            price = price_for(model_name) if model_name else None
            estimate = estimate_run(
                cases, prompt_text, runs=args.runs,
                price_in_per_mtok=price.in_per_mtok if price else None,
                price_out_per_mtok=price.out_per_mtok if price else None,
            )
            print(f"{args.split}: {len(cases)} cases x {args.runs} runs = {estimate.calls} calls "
                  f"(up to {estimate.max_calls} if every reply is retried), model {model_name or '(none set)'}")
            if estimate.cost_usd_low is None:
                print(f"estimated cost: unknown (model {model_name!r} is not listed in config/prices.json)")
            else:
                print(f"estimated cost (rough): ${estimate.cost_usd_low:.4f} to ${estimate.cost_usd_high:.4f}")
            print("Dry run only. Add --yes to make the real calls.")
            return 0
        llm = llm_factory() if llm_factory else OpenAIClient.from_env(model=args.model)
        path = run_split(args.split, prompt_version=args.prompt, llm=llm, runs=args.runs,
                         datasets_dir=DATASETS_DIR, results_dir=RESULTS_DIR, today=today())
    except (DatasetError, LeakageError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    overall = json.loads(path.read_text(encoding="utf-8"))["report"]["overall"]
    type_accuracy = overall["type"]["accuracy"]
    type_text = "n/a" if type_accuracy is None else f"{type_accuracy:.3f}"
    print(f"type accuracy {type_text} | false expense {overall['type']['false_expense_rate']} "
          f"| missed {overall['type']['missed_expense_rate']}")
    print(f"full-case accuracy {overall['extraction']['full_case_accuracy']:.3f} | consistency {overall['consistency']:.3f} "
          f"| grounding failures {overall['grounding_failure_rate']}")
    print(f"cost ${overall['cost_usd']} | latency p50 {overall['latency_s']['p50']:.2f}s p95 {overall['latency_s']['p95']:.2f}s")
    print(f"result file: {path}")
    return 0


def _sum_reported(values: list[int | None]) -> int | None:
    """Sum of the values that were reported; None when none was."""
    reported = [v for v in values if v is not None]
    return sum(reported) if reported else None


def _price_record(model: str | None, prices_path: Path | None) -> dict | None:
    """The model's prices (USD per 1M tokens) as plain strings, or None when it is not listed."""
    price = price_for(model, prices_path) if model else None
    if price is None:
        return None
    return {"in": format(price.in_per_mtok, "f"), "out": format(price.out_per_mtok, "f")}


if __name__ == "__main__":
    raise SystemExit(main())
