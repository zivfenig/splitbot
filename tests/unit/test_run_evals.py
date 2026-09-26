"""Self-tests for the eval harness (tests/llm_evals/run_evals.py). Offline: FakeLLM only."""

import json
from datetime import date
from decimal import Decimal

import pytest

from splitbot.llm.client import LLMResult
from splitbot.llm.extractor import Extraction, load_prompt
from splitbot.models import ExtractedExpense, Member
from tests.fakes import FakeLLM
from tests.llm_evals import run_evals

MEMBERS = [Member(id=i, name=n) for i, n in enumerate(["זיו", "דני", "משה", "מיכל", "דני לוי"], start=1)]
TODAY = date(2026, 9, 26)
LEAKING_MESSAGE = "הזמנתי סושי ב-212 בלי משה, הוא בחוץ"  # copied from a few-shot example of extract_v1
M1, M2, M3 = "שילמתי 50 על החלב", "bought oat milk 50", "שילמתי 50 על הכביסה"


# --- small builders ---------------------------------------------------------


def ev(value, evidence=None):
    return {"value": value, "evidence": evidence, "source": "message" if evidence else "default"}


def known(i):
    return {"kind": "known", "id": i}


def amb(*ids):
    return {"kind": "ambiguous", "candidates": list(ids)}


def parts(only=None, exclude=()):
    return {"only": only, "exclude": list(exclude)}


def person(i, amount):
    return {"member": known(i), "amount": amount, "evidence": f"דני {amount}"}


def exp(**kw):
    return ExtractedExpense.model_validate({"message_type": "new", "confidence": "high", **kw})


def make_case(expected, *, id="c1", message=None, accepted=None, expect_low=False):
    if message is None:  # a message that contains every piece of evidence of the expected answer
        fields = [getattr(expected, n) for n in ("amount", "currency", "payer", "participants", "refers_to")]
        quoted = [f.evidence for f in fields if f is not None]
        quoted += [p.evidence for p in expected.exact_amounts or []]
        message = " ".join(["שילמתי"] + [q for q in quoted if q])
    return run_evals.Case(id, "dev", "A", 1, message, MEMBERS, expected, accepted, expect_low)


def extraction(expense, *, status="ok", issues=(), latency=0.0, cost=None):
    call = LLMResult("{}", "fake", 0.0, 10, 5, latency, cost)
    return Extraction(status, expense, list(issues), "extract_v1", [call])


def run_score(fields, *, ptype="new", ground=False, sig="s", cost=Decimal("0.01"), latency=1.0, failures=()):
    return run_evals.RunScore(fields, list(failures), all(fields.values()), ptype, ground, (sig,), cost, latency)


def payload(amount="50", **over):
    base = {
        "message_type": "new", "confidence": "high", "amount": ev(amount, amount), "amount_in_words": False,
        "currency": ev("ILS"), "payer": ev(known(1)), "participants": ev(parts()), "exact_amounts": None,
        "subcategory": "groceries", "description": None, "refers_to": None,
    }
    return {**base, **over}


def row(id_, message, *, split="dev", roster="A", verified=True, expected=None, low=False, accepted=("groceries",)):
    return {
        "id": id_, "split": split, "roster": roster, "sender_id": 1, "message": message, "source": "user",
        "verified": verified, "expected": expected or payload(), "notes": "",
        "scoring": {"subcategory_any_of": list(accepted), "expect_low": low, "score_confidence": False,
                    "score_description": False},
    }


def write_dataset(folder, dev, test=None):
    folder.mkdir(parents=True, exist_ok=True)
    rosters = {
        "A": [{"id": 1, "name": "זיו"}, {"id": 2, "name": "דני"}, {"id": 3, "name": "משה"}],
        "B": [{"id": 1, "name": "זיו"}, {"id": 2, "name": "דני"}, {"id": 3, "name": "משה"}, {"id": 4, "name": "מיכל"}],
    }
    (folder / "rosters.json").write_text(json.dumps(rosters, ensure_ascii=False), encoding="utf-8")
    for split, rows in (("dev", dev), ("test", test)):
        if rows is not None:
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
            (folder / f"extraction_{split}.jsonl").write_text(text, encoding="utf-8")
    return folder


def approx_or_none(actual, wanted):
    assert actual is None if wanted is None else actual == pytest.approx(wanted)




# --- 1 ----------------------------------------------------------------------

BAD_DATASETS = {
    "row-not-verified": ([row("d1", M1, verified=False)], None, "dev"),
    "unknown-roster-name": ([row("d1", M1, roster="Z")], None, "dev"),
    "expected-does-not-validate": ([row("d1", M1, expected=payload(subcategory="sushi"))], None, "dev"),
    "duplicate-id-in-file": ([row("d1", M1), row("d1", M2)], None, "dev"),
    "row-split-differs-from-requested": ([row("d1", M1, split="test")], None, "dev"),
    "same-id-in-dev-and-test": ([row("d1", M1)], [row("d1", M3, split="test")], "dev"),
    "same-message-in-dev-and-test": ([row("d1", M1)], [row("t1", M1, split="test")], "test"),
}


def test_every_verified_case_matches_the_model_and_validators(tmp_path):
    # (a) the real, human-verified data
    dev, test = run_evals.load_cases("dev"), run_evals.load_cases("test")
    every = dev + test
    assert all(isinstance(c.expected, ExtractedExpense) for c in every)
    assert {c.id for c in every if c.expected_is_ask} == {"case-36", "case-37"}
    assert {c.language for c in every} <= {"he", "en", "mixed"}
    assert len({c.id for c in every}) == len(every)

    # (b) each broken tiny dataset is refused
    for name, (dev_rows, test_rows, split) in BAD_DATASETS.items():
        folder = write_dataset(tmp_path / name, dev_rows, test_rows)
        with pytest.raises(run_evals.DatasetError):
            run_evals.load_cases(split, datasets_dir=folder)

    # control: a good tiny dataset loads and the row fields are filled in
    good_dev = [row("d1", M1, low=True, accepted=("groceries", "delivery")), row("d2", M2)]
    folder = write_dataset(tmp_path / "good", good_dev, [row("t1", M3, split="test", roster="B")])
    first, second = run_evals.load_cases("dev", datasets_dir=folder)
    assert [first.id, second.id] == ["d1", "d2"]
    assert (first.split, first.roster, first.sender_id, first.message) == ("dev", "A", 1, M1)
    assert [(m.id, m.name) for m in first.members] == [(1, "זיו"), (2, "דני"), (3, "משה")]
    assert first.accepted_subcategories == ["groceries", "delivery"]
    assert (first.expect_low, second.expect_low) == (True, False)
    assert (first.language, second.language) == ("he", "en")
    assert first.expected.amount.value == "50"
    (only_test,) = run_evals.load_cases("test", datasets_dir=folder)
    assert len(only_test.members) == 4  # roster B
    dev_only = write_dataset(tmp_path / "dev_only", good_dev)  # no test file: nothing to cross-check
    assert len(run_evals.load_cases("dev", datasets_dir=dev_only)) == 2


# --- 2 ----------------------------------------------------------------------


def R(name, e, a, field, ok, accepted=None, not_scored=()):
    return pytest.param(e, a, field, ok, accepted, not_scored, id=name)


AMOUNT = {"amount": ev("412.50", "412.50")}
WORDS = {"amount": ev("200", "מאתיים"), "amount_in_words": True}
ONLY_MICHAL = {"participants": ev(parts(only=[known(4)]), "עם מיכל")}
SCORING_ROWS = [
    R("amount-comma-equals-dot", AMOUNT, {"amount": ev("412,50")}, "amount", True),
    R("amount-different-fails", AMOUNT, {"amount": ev("412.60")}, "amount", False),
    R("amount-unparsable-equal-texts", {"amount": ev("1.200", "1.200")}, {"amount": ev(" 1.200 ")}, "amount", True),
    R("amount-unparsable-different-texts", {"amount": ev("1.200", "1.200")}, {"amount": ev("1.300")}, "amount", False),
    R("words-flag-wrongly-set", AMOUNT, {"amount_in_words": True}, "amount_in_words", False),
    R("words-flag-missed", WORDS, {"amount_in_words": False}, "amount_in_words", False),
    R("words-flag-equal", WORDS, {}, "amount_in_words", True),
    R("currency-differs", {"currency": ev("USD", "$")}, {"currency": ev("ILS")}, "currency", False),
    R("currency-same", {"currency": ev("USD", "$")}, {}, "currency", True),
    R("payer-same-member-other-evidence", {"payer": ev(known(2), "דני")}, {"payer": ev(known(2))}, "payer", True),
    R("payer-other-member", {"payer": ev(known(2), "דני")}, {"payer": ev(known(3))}, "payer", False),
    R("payer-ambiguous-same-candidates", {"payer": ev(amb(2, 5), "דני")}, {"payer": ev(amb(5, 2))}, "payer", True),
    R("payer-ambiguous-other-candidates", {"payer": ev(amb(2, 5), "דני")}, {"payer": ev(amb(2, 3))}, "payer", False),
    R("payer-ambiguous-vs-known", {"payer": ev(amb(2, 5), "דני")}, {"payer": ev(known(2))}, "payer", False),
    R("participants-only-equals-only-with-author", ONLY_MICHAL,
      {"participants": ev(parts(only=[known(1), known(4)]))}, "participants", True),
    R("participants-author-exclusion-lost",
      {"participants": ev(parts(only=[known(2), known(4)], exclude=[known(1)]), "עם דני ומיכל בלי זיו")},
      {"participants": ev(parts(only=[known(2), known(4)]))}, "participants", False),
    R("participants-missing-exclusion", {"participants": ev(parts(exclude=[known(2)]), "בלי דני")},
      {"participants": ev(parts())}, "participants", False),
    R("participants-ambiguous-same-sets", {"participants": ev(parts(exclude=[amb(2, 5)]), "בלי דני")},
      {"participants": ev(parts(exclude=[amb(5, 2)]))}, "participants", True),
    R("participants-ambiguous-other-sets", {"participants": ev(parts(exclude=[amb(2, 5)]), "בלי דני")},
      {"participants": ev(parts(exclude=[amb(2, 3)]))}, "participants", False),
    R("exact-amounts-order-irrelevant", {"exact_amounts": [person(2, "30"), person(4, "30")]},
      {"exact_amounts": [person(4, "30"), person(2, "30")]}, "exact_amounts", True),
    R("exact-amounts-compared-parsed", {"exact_amounts": [person(2, "30")]},
      {"exact_amounts": [person(2, "30.00")]}, "exact_amounts", True),
    R("exact-amounts-other-amount", {"exact_amounts": [person(2, "30")]},
      {"exact_amounts": [person(2, "35")]}, "exact_amounts", False),
    R("exact-amounts-invented", {}, {"exact_amounts": [person(2, "50")]}, "exact_amounts", False),
    R("exact-amounts-missed", {"exact_amounts": [person(2, "30")]}, {"exact_amounts": None}, "exact_amounts", False),
    R("subcategory-any-accepted", {"subcategory": "restaurant"}, {"subcategory": "delivery"}, "subcategory", True,
      accepted=["restaurant", "delivery"]),
    R("subcategory-outside-accepted", {"subcategory": "restaurant"}, {"subcategory": "groceries"}, "subcategory",
      False, accepted=["restaurant", "delivery"]),
    R("subcategory-without-list-must-match", {"subcategory": "restaurant"}, {"subcategory": "delivery"},
      "subcategory", False),
    R("refers-to-ignores-outer-whitespace", {"message_type": "correction", "refers_to": ev("240", "240")},
      {"refers_to": ev(" 240 ")}, "refers_to", True, not_scored=("amount", "amount_in_words", "exact_amounts")),
    R("refers-to-different", {"message_type": "correction", "refers_to": ev("240", "240")},
      {"refers_to": ev("260")}, "refers_to", False),
    R("fields-not-set-in-expected-are-not-scored", {"amount": ev("100", "100")},
      {"currency": ev("USD", "$"), "payer": ev(known(3)), "subcategory": "other"}, None,
      True, not_scored=("currency", "payer", "participants", "subcategory", "refers_to")),
    R("chat-row-scores-only-type-and-confidence", {"message_type": "chat"}, {}, None, True,
      not_scored=("amount", "amount_in_words", "exact_amounts", "currency", "payer", "participants")),
    R("nothing-parsed-fails-every-field", {"amount": ev("100", "100"), "currency": ev("USD", "$")}, None, None, False),
]


@pytest.mark.parametrize("e, a, field, ok, accepted, not_scored", SCORING_ROWS)
def test_scoring_compares_each_field_by_meaning_not_by_text(e, a, field, ok, accepted, not_scored):
    case = make_case(exp(**e), accepted=accepted)
    if a is None:  # the reply could not be parsed at all
        score = run_evals.score_run(case, extraction(None, status="needs_clarification", issues=["not valid JSON"]))
        assert score.fields and not any(score.fields.values())
        assert (score.predicted_type, score.grounding_failure, score.full_correct) == (None, None, False)
        return
    status = "needs_clarification" if case.expected_is_ask else "ok"
    score = run_evals.score_run(case, extraction(exp(**{**e, **a}), status=status))
    failed = [f.field for f in score.failures]
    assert failed == ([] if ok else [field])
    assert score.full_correct is ok
    if field is not None:
        assert score.fields[field] is ok
    assert not set(not_scored) & set(score.fields)
    if field in ("amount", "currency") and not ok:  # the failure text is readable
        (failure,) = score.failures
        assert e[field]["value"] in failure.expected and a[field]["value"] in failure.actual


# --- 3 ----------------------------------------------------------------------

# each run: (expected type, predicted type or None = nothing parsed, expect_low, confidence in the reply)
TYPE_ROWS = [
    pytest.param([("chat", "new", False, "high"), ("chat", "chat", False, "high"),
                  ("new", "chat", False, "high"), ("new", "new", False, "high")],
                 0.5, 0.5, 0.5, [True] * 4, id="one-of-two-chat-rows-becomes-expense-one-of-two-new-is-missed"),
    pytest.param([("chat", None, False, "high"), ("chat", "chat", False, "high")],
                 0.5, 0.5, None, [False, True], id="unparsed-reply-on-chat-row-is-a-false-expense"),
    pytest.param([("new", "new", False, "high")] * 3 + [("new", "chat", False, "high")],
                 0.75, None, 0.25, [True] * 4, id="no-chat-rows-means-no-false-expense-rate"),
    pytest.param([("chat", "chat", False, "high")] * 2,
                 1.0, 0.0, None, [True] * 2, id="no-non-chat-rows-means-no-missed-rate"),
    pytest.param([("chat", "chat", True, "low"), ("chat", "chat", True, "medium"), ("chat", "chat", True, "high"),
                  ("chat", "chat", False, "high"), ("chat", "chat", False, "medium"), ("chat", "chat", False, "low")],
                 1.0, 0.0, None, [True, False, False, True, True, False], id="high-vs-medium-is-never-scored"),
]


@pytest.mark.parametrize("specs, accuracy, false_rate, missed_rate, confidence_ok", TYPE_ROWS)
def test_type_confusions_and_low_confidence_follow_the_scoring_rules(
    specs, accuracy, false_rate, missed_rate, confidence_ok
):
    cases, scores = [], {}
    for i, (expected_type, predicted, low, confidence) in enumerate(specs):
        expected = exp(message_type="chat") if expected_type == "chat" else exp(amount=ev("100", "100"))
        case = make_case(expected, id=f"c{i}", expect_low=low)
        if predicted is None:
            actual = None
        elif predicted == "chat":
            actual = exp(message_type="chat", confidence=confidence)
        else:
            actual = exp(amount=ev("100"), confidence=confidence)
        cases.append(case)
        scores[case.id] = [run_evals.score_run(case, extraction(actual))]
    assert [s[0].fields["confidence"] for s in scores.values()] == confidence_ok
    assert [s[0].predicted_type for s in scores.values()] == [p for _, p, _, _ in specs]
    types = run_evals.build_report(cases, scores)["overall"]["type"]
    assert types["accuracy"] == pytest.approx(accuracy)
    approx_or_none(types["false_expense_rate"], false_rate)
    approx_or_none(types["missed_expense_rate"], missed_rate)


# --- 4 ----------------------------------------------------------------------


def test_grounding_failures_ignore_rows_where_the_expected_answer_is_an_ask():
    ambiguous_exclude = exp(amount=ev("100", "100"), participants=ev(parts(exclude=[amb(2, 5)]), "בלי דני"))
    unparsable_amount = exp(amount=ev("1.200", "1.200"))
    for expected in (ambiguous_exclude, unparsable_amount):
        case = make_case(expected)
        assert case.expected_is_ask
        asked = run_evals.score_run(case, extraction(expected, status="needs_clarification", issues=["ask"]))
        assert asked.fields["asks"] is True and asked.full_correct
        assert asked.grounding_failure is None  # even though the run has issues
        silent = run_evals.score_run(case, extraction(expected, status="ok"))
        assert silent.fields["asks"] is False and not silent.full_correct
        assert silent.grounding_failure is None

    normal = make_case(exp(amount=ev("100", "100")), id="normal")
    assert not normal.expected_is_ask
    clean = run_evals.score_run(normal, extraction(exp(amount=ev("100", "100"))))
    dirty = run_evals.score_run(
        normal, extraction(exp(amount=ev("100", "100")), status="needs_clarification", issues=["evidence not found"])
    )
    assert "asks" not in clean.fields and "asks" not in dirty.fields
    assert (clean.grounding_failure, dirty.grounding_failure) == (False, True)

    unparsed = run_evals.score_run(normal, extraction(None, status="needs_clarification", issues=["bad JSON"]))
    ask_case = make_case(unparsable_amount, id="ask")
    ask_run = run_evals.score_run(ask_case, extraction(unparsable_amount, status="needs_clarification", issues=["x"]))
    # non-None values: dirty, clean, clean -> 1 of 3; the ask runs and the unparsed run are left out
    report = run_evals.build_report(
        [normal, ask_case], {"normal": [dirty, clean, clean, unparsed], "ask": [ask_run] * 3}
    )
    assert report["overall"]["grounding_failure_rate"] == pytest.approx(1 / 3)
    only_asks = run_evals.build_report([ask_case], {"ask": [ask_run] * 3})
    assert only_asks["overall"]["grounding_failure_rate"] is None


# --- 5 ----------------------------------------------------------------------

TYPE_AMOUNT = {"type": True, "amount": True}
AMOUNT_WRONG = {"type": True, "amount": False}
WITH_CURRENCY = {"type": True, "amount": True, "currency": True}
CURRENCY_WRONG = {"type": True, "amount": True, "currency": False}
CURRENCY_FAILURE = run_evals.Failure("currency", "USD", "ILS")
AMOUNT_FAILURE = run_evals.Failure("amount", "100", "200")


def report_inputs(h2_run3_cost=Decimal("0.01")):
    """4 cases x 3 runs. Latencies are 1..12 in order, every run costs 0.01."""
    expected = exp(amount=ev("100", "100"))
    messages = {"h1": "שילמתי 100", "h2": "שילמתי 100", "e1": "paid 100", "m1": "שילמתי 100 for pizza"}
    cases = [make_case(expected, id=k, message=v) for k, v in messages.items()]
    rs = run_score
    runs = {
        "h1": [rs(TYPE_AMOUNT, sig="a", latency=1.0), rs(TYPE_AMOUNT, sig="a", latency=2.0),
               rs(TYPE_AMOUNT, sig="a", latency=3.0)],
        "h2": [rs(WITH_CURRENCY, sig="x", latency=4.0),
               rs(CURRENCY_WRONG, sig="y", latency=5.0, failures=[CURRENCY_FAILURE]),
               rs(CURRENCY_WRONG, sig="y", latency=6.0, failures=[CURRENCY_FAILURE], cost=h2_run3_cost)],
        "e1": [rs(AMOUNT_WRONG, sig="z", latency=7.0 + i, failures=[AMOUNT_FAILURE]) for i in range(3)],
        "m1": [rs(TYPE_AMOUNT, sig="m", latency=10.0 + i) for i in range(3)],
    }
    return cases, runs


def test_report_covers_overall_language_case_consistency_cost_and_latency():
    cases, runs = report_inputs()
    report = run_evals.build_report(cases, runs)
    assert json.loads(json.dumps(report)) == report  # JSON-serializable
    assert set(report) == {"overall", "by_language", "per_case", "failures"}
    assert set(report["by_language"]) == {"he", "en", "mixed"}
    overall = report["overall"]
    assert set(overall) == {"n_cases", "n_runs", "type", "extraction", "grounding_failure_rate", "consistency",
                            "cost_usd", "latency_s"}
    assert set(overall["type"]) == {"accuracy", "false_expense_rate", "missed_expense_rate"}
    assert set(overall["extraction"]) == {"full_case_accuracy", "per_field_accuracy"}

    # (n_cases, n_runs, full_case_accuracy, {field: accuracy}, consistency, cost, p50, p95)
    wanted = {
        "overall": (4, 12, 7 / 12, {"type": 1.0, "amount": 0.75, "currency": 1 / 3}, 0.75, "0.12", 6.0, 12.0),
        "he": (2, 6, 4 / 6, {"type": 1.0, "amount": 1.0, "currency": 1 / 3}, 0.5, "0.06", 3.0, 6.0),
        "en": (1, 3, 0.0, {"type": 1.0, "amount": 0.0}, 1.0, "0.03", 8.0, 9.0),
        "mixed": (1, 3, 1.0, {"type": 1.0, "amount": 1.0}, 1.0, "0.03", 11.0, 12.0),
    }
    sections = {"overall": overall, **report["by_language"]}
    for name, (n_cases, n_runs, full, per_field, consistency, cost, p50, p95) in wanted.items():
        section = sections[name]
        assert (section["n_cases"], section["n_runs"]) == (n_cases, n_runs), name
        assert section["extraction"]["full_case_accuracy"] == pytest.approx(full), name
        assert section["extraction"]["per_field_accuracy"] == pytest.approx(per_field), name  # only scored fields
        assert section["consistency"] == pytest.approx(consistency), name
        assert isinstance(section["cost_usd"], str) and Decimal(section["cost_usd"]) == Decimal(cost), name
        assert section["latency_s"] == pytest.approx({"p50": p50, "p95": p95}), name
        assert section["grounding_failure_rate"] == 0.0, name
        assert section["type"]["false_expense_rate"] is None and section["type"]["missed_expense_rate"] == 0.0

    per_case = report["per_case"]
    assert set(per_case) == {"h1", "h2", "e1", "m1"}
    for case_id, (language, full_runs, consistent, cost, latency) in {
        "h1": ("he", 3, True, "0.03", 2.0), "h2": ("he", 1, False, "0.03", 5.0),
        "e1": ("en", 0, True, "0.03", 8.0), "m1": ("mixed", 3, True, "0.03", 11.0),
    }.items():
        entry = per_case[case_id]
        assert (entry["language"], entry["runs"], entry["full_correct_runs"], entry["consistent"]) == (
            language, 3, full_runs, consistent), case_id
        assert Decimal(entry["cost_usd"]) == Decimal(cost) and entry["latency_s"] == pytest.approx(latency)

    def failure(case_id, run, field, expected, actual):
        return {"case_id": case_id, "run": run, "field": field, "expected": expected, "actual": actual}

    assert report["failures"] == [
        failure("e1", 1, "amount", "100", "200"), failure("e1", 2, "amount", "100", "200"),
        failure("e1", 3, "amount", "100", "200"),
        failure("h2", 2, "currency", "USD", "ILS"), failure("h2", 3, "currency", "USD", "ILS"),
    ]

    # one unknown cost makes every total that contains it unknown (never 0)
    cases, runs = report_inputs(h2_run3_cost=None)
    unknown = run_evals.build_report(cases, runs)
    assert unknown["overall"]["cost_usd"] is None
    assert unknown["by_language"]["he"]["cost_usd"] is None
    assert unknown["per_case"]["h2"]["cost_usd"] is None
    assert Decimal(unknown["by_language"]["en"]["cost_usd"]) == Decimal("0.03")
    assert Decimal(unknown["per_case"]["h1"]["cost_usd"]) == Decimal("0.03")


# --- 6 ----------------------------------------------------------------------


def test_leakage_check_result_file_and_dry_run(tmp_path, monkeypatch, capsys):
    # (a) leakage check
    prompt = "Examples\nMessage: Paid $45 for the cleaning lady, just with Michal\nMessage: " + M1 + "\n"
    leaked = make_case(exp(), id="leaky", message=M1)
    variant = make_case(exp(), id="variant", message="  PAID $45 for  the cleaning lady,\njust with Michal ")
    innocent = make_case(exp(), id="innocent", message="totally different text 12")
    with pytest.raises(run_evals.LeakageError, match="leaky"):
        run_evals.check_no_leakage([innocent, leaked], prompt)
    with pytest.raises(run_evals.LeakageError, match="variant"):
        run_evals.check_no_leakage([variant], prompt)
    assert run_evals.check_no_leakage([innocent], prompt) is None
    real_cases = run_evals.load_cases("dev") + run_evals.load_cases("test")
    assert run_evals.check_no_leakage(real_cases, load_prompt("extract_v1")) is None

    # (b) run_split with a fake LLM: 2 cases x 3 runs = 6 replies
    reply = json.dumps(payload())
    folder = write_dataset(tmp_path / "data", [row("d1", M1), row("d2", M2)])
    results = tmp_path / "results"
    fake = FakeLLM([reply] * 6)
    path = run_evals.run_split("dev", prompt_version="extract_v1", llm=fake, runs=3, datasets_dir=folder,
                               results_dir=results, today=TODAY)
    assert path == results / "extract_v1_dev_2026-09-26.json" and len(fake.calls) == 6
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["prompt_version"], data["split"], data["date"]) == ("extract_v1", "dev", "2026-09-26")
    assert (data["model"], data["temperature"], data["runs_per_case"], data["n_cases"]) == ("fake", 0.0, 3, 2)
    assert set(data["report"]) == {"overall", "by_language", "per_case", "failures"}
    assert set(data["runs"]) == {"d1", "d2"}
    for items in data["runs"].values():
        assert len(items) == 3
        for item in items:
            assert set(item) == {"status", "issues", "cost_usd", "latency_s", "input_tokens", "output_tokens",
                                 "extracted"}
            assert (item["status"], item["issues"], item["cost_usd"]) == ("ok", [], None)
            assert (item["latency_s"], item["input_tokens"], item["output_tokens"]) == (0.0, 10, 5)
            assert item["extracted"]["amount"]["value"] == "50"

    leaking = write_dataset(tmp_path / "leak", [row("d1", LEAKING_MESSAGE, expected=payload("212"))])
    silent = FakeLLM([])
    with pytest.raises(run_evals.LeakageError):
        run_evals.run_split("dev", prompt_version="extract_v1", llm=silent, datasets_dir=leaking,
                            results_dir=tmp_path / "leak_results", today=TODAY)
    assert silent.calls == [] and not (tmp_path / "leak_results").exists()

    # (c) estimate
    cases = [make_case(exp(), id="a"), make_case(exp(), id="b")]
    args = {"runs": 3, "price_in_per_mtok": Decimal(1), "price_out_per_mtok": Decimal(2)}
    priced = run_evals.estimate_run(cases, "x" * 400, **args)
    assert (priced.calls, priced.max_calls) == (6, 12)
    assert 0 < priced.cost_usd_low <= priced.cost_usd_high
    for missing in ({"price_in_per_mtok": None}, {"price_out_per_mtok": None}):
        unknown = run_evals.estimate_run(cases, "x" * 400, **{**args, **missing})
        assert (unknown.calls, unknown.cost_usd_low, unknown.cost_usd_high) == (6, None, None)

    # (d) main: a dry run never builds the client
    monkeypatch.delenv("OPENAI_PRICE_IN_PER_MTOK", raising=False)
    monkeypatch.delenv("OPENAI_PRICE_OUT_PER_MTOK", raising=False)

    def no_llm():
        raise AssertionError("the LLM client must not be built")

    assert run_evals.main(["--split", "dev"], llm_factory=no_llm) == 0
    assert str(len(run_evals.load_cases("dev")) * 3) in capsys.readouterr().out

    monkeypatch.setattr(run_evals, "DATASETS_DIR", folder)
    monkeypatch.setattr(run_evals, "RESULTS_DIR", results)
    built = []

    def factory():
        built.append(1)
        return FakeLLM([reply, reply])

    argv = ["--split", "dev", "--runs", "1", "--yes"]
    assert run_evals.main(argv, llm_factory=factory, today=lambda: TODAY) == 0
    assert built == [1]
    assert "extract_v1_dev_2026-09-26.json" in capsys.readouterr().out
    assert (results / "extract_v1_dev_2026-09-26.json").exists()

    # bad data: a message and exit code 2 (dry run, so no client either)
    for name, rows in {"leak": [row("d1", LEAKING_MESSAGE, expected=payload("212"))],
                       "unverified": [row("d1", M1, verified=False)]}.items():
        monkeypatch.setattr(run_evals, "DATASETS_DIR", write_dataset(tmp_path / f"main_{name}", rows))
        assert run_evals.main(["--split", "dev"], llm_factory=no_llm) == 2
        printed = capsys.readouterr()
        assert (printed.out + printed.err).strip()
        if name == "leak":
            assert "d1" in printed.out + printed.err
