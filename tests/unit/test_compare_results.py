"""Comparing two eval result files: per-field table, summary, and the cases where models disagree."""

import copy
import json
import re

import pytest

from tests.llm_evals.compare_results import compare, main, render


def _fail(case_id, run, field, expected, actual):
    return {"case_id": case_id, "run": run, "field": field, "expected": expected, "actual": actual}


def _case(correct, *, consistent=True, cost="0.001", latency=1.0):
    return {
        "language": "en",
        "runs": 3,
        "full_correct_runs": correct,
        "consistent": consistent,
        "cost_usd": cost,
        "latency_s": latency,
    }


# Model A (gpt-4o-mini): c1 3/3, c2 3/3, c3 2/3 (amount wrong once), c4 0/3 (payer wrong every run).
RESULT_A = {
    "prompt_version": "extract_v1",
    "split": "dev",
    "model": "gpt-4o-mini",
    "temperature": 0.0,
    "prices": {"in": "0.15", "out": "0.60"},
    "prices_sha": "aaa",
    "report": {
        "overall": {
            "n_cases": 4,
            "n_runs": 12,
            "type": {"accuracy": 1.0, "false_expense_rate": 0.0, "missed_expense_rate": 0.0},
            "extraction": {
                "full_case_accuracy": 8 / 12,
                "per_field_accuracy": {"amount": 2 / 3, "payer": 0.5},
            },
            "grounding_failure_rate": 0.0,
            "consistency": 0.75,
            "cost_usd": "0.012",
            "n_calls": 12,
            "cost_usd_per_call": "0.001",
            "cost_usd_per_correct": "0.0015",
            "latency_s": {"p50": 1.0, "p95": 2.5},
        },
        "by_language": {},
        "per_case": {
            "c1": _case(3),
            "c2": _case(3),
            "c3": _case(2, consistent=False),
            "c4": _case(0),
        },
        "failures": [
            _fail("c3", 1, "amount", 240, 204),
            _fail("c4", 1, "payer", "u1", "u2"),
            _fail("c4", 2, "payer", "u1", "u2"),
            _fail("c4", 3, "payer", "u1", None),
        ],
    },
}

# Model B (gpt-4o): c1 3/3, c2 1/3 (amount, then participants wrong), c3 2/3 (currency wrong once),
# c4 0/3 (payer wrong every run, with different wrong values than A -> same failed field).
RESULT_B = {
    "prompt_version": "extract_v1",
    "split": "dev",
    "model": "gpt-4o",
    "temperature": 0.0,
    "prices": {"in": "2.50", "out": "10.00"},
    "prices_sha": "bbb",
    "report": {
        "overall": {
            "n_cases": 4,
            "n_runs": 12,
            "type": {"accuracy": 0.9, "false_expense_rate": None, "missed_expense_rate": 0.25},
            "extraction": {
                "full_case_accuracy": 0.5,
                "per_field_accuracy": {"amount": 1.0, "payer": 5 / 6, "currency": 0.75},
            },
            "grounding_failure_rate": 0.1,
            "consistency": 0.5,
            "cost_usd": "0.24",
            "n_calls": 12,
            "cost_usd_per_call": "0.02",
            "cost_usd_per_correct": "0.04",
            "latency_s": {"p50": 2.0, "p95": 4.0},
        },
        "by_language": {},
        "per_case": {
            "c1": _case(3),
            "c2": _case(1, consistent=False),
            "c3": _case(2, consistent=False),
            "c4": _case(0),
        },
        "failures": [
            _fail("c2", 2, "amount", 100, 10),
            _fail("c2", 3, "participants", ["u1", "u2"], ["u1"]),
            _fail("c3", 2, "currency", "ILS", "USD"),
            _fail("c4", 1, "payer", "u1", "u2"),
            _fail("c4", 2, "payer", "u1", "u3"),
            _fail("c4", 3, "payer", "u1", None),
        ],
    },
}


def _without_case(result, case_id):
    out = copy.deepcopy(result)
    del out["report"]["per_case"][case_id]
    out["report"]["failures"] = [f for f in out["report"]["failures"] if f["case_id"] != case_id]
    return out


def test_compare_lists_per_field_side_by_side_and_the_cases_where_the_models_disagree(tmp_path, capsys):
    a, b = copy.deepcopy(RESULT_A), copy.deepcopy(RESULT_B)
    result = compare(a, b)

    # models and prices
    assert result["models"] == ["gpt-4o-mini", "gpt-4o"]
    assert result["prompts"] == ["extract_v1", "extract_v1"]
    assert result["prices"] == [{"in": "0.15", "out": "0.60"}, {"in": "2.50", "out": "10.00"}]

    # per-field accuracy side by side; "currency" exists only in file B
    fields = result["fields"]
    assert set(fields) == {"amount", "payer", "currency"}
    assert fields["amount"] == [2 / 3, 1.0]
    assert fields["payer"] == [0.5, 5 / 6]
    assert fields["currency"][1] == 0.75
    assert fields["currency"][0] is None  # missing in A (assumed None, see report)

    # type block: one dict per model, values taken as they are
    assert result["type"] == [
        {"accuracy": 1.0, "false_expense_rate": 0.0, "missed_expense_rate": 0.0},
        {"accuracy": 0.9, "false_expense_rate": None, "missed_expense_rate": 0.25},
    ]

    # summary metrics as [a, b]
    assert result["summary"] == {
        "full_case_accuracy": [8 / 12, 0.5],
        "grounding_failure_rate": [0.0, 0.1],
        "consistency": [0.75, 0.5],
        "cost_usd": ["0.012", "0.24"],
        "cost_usd_per_call": ["0.001", "0.02"],
        "cost_usd_per_correct": ["0.0015", "0.04"],
        "latency_p50_s": [1.0, 2.0],
        "latency_p95_s": [2.5, 4.0],
    }

    # disagreements: c1 same, c2 differs in correct runs, c3 differs in failed field, c4 same
    dis = result["disagreements"]
    assert [d["case_id"] for d in dis] == ["c2", "c3"]
    c2, c3 = dis
    assert c2["correct_runs"] == [3, 1]
    assert c2["failed_fields"][0] == []
    assert sorted(c2["failed_fields"][1]) == ["amount", "participants"]
    assert c2["failures"] == [
        [],
        [
            {"run": 2, "field": "amount", "expected": 100, "actual": 10},
            {"run": 3, "field": "participants", "expected": ["u1", "u2"], "actual": ["u1"]},
        ],
    ]
    assert c3["correct_runs"] == [2, 2]
    assert c3["failed_fields"] == [["amount"], ["currency"]]
    assert c3["failures"] == [
        [{"run": 1, "field": "amount", "expected": 240, "actual": 204}],
        [{"run": 2, "field": "currency", "expected": "ILS", "actual": "USD"}],
    ]
    assert "message" not in c2 and "message" not in c3

    # not comparable: different split, different case ids
    other_split = copy.deepcopy(RESULT_B)
    other_split["split"] = "test"
    other_cases = _without_case(RESULT_B, "c4")
    for bad in (other_split, other_cases):
        with pytest.raises(ValueError):
            compare(copy.deepcopy(RESULT_A), bad)

    # comparable: same model, different prompt versions
    same_model_v2 = copy.deepcopy(RESULT_A)
    same_model_v2["prompt_version"] = "extract_v2"
    prompt_result = compare(copy.deepcopy(RESULT_A), same_model_v2)
    assert prompt_result["prompts"] == ["extract_v1", "extract_v2"]
    assert prompt_result["models"] == ["gpt-4o-mini", "gpt-4o-mini"]
    prompt_text = render(prompt_result)
    assert "gpt-4o-mini (extract_v1)" in prompt_text
    assert "gpt-4o-mini (extract_v2)" in prompt_text

    # render: both models, every field with a percentage, disagreeing ids only
    text = render(result)
    assert "gpt-4o-mini (extract_v1)" in text
    assert "gpt-4o (extract_v1)" in text
    for field in ("amount", "payer", "currency"):
        assert field in text
    for pct in ("66.7%", "50.0%", "100.0%", "83.3%", "75.0%"):
        assert pct in text
    assert re.search(r"\bc2\b", text) and re.search(r"\bc3\b", text)
    # c1 and c4 never disagree, and no other table lists case ids
    assert not re.search(r"\bc1\b", text)
    assert not re.search(r"\bc4\b", text)

    # main: two files -> prints the rendered text, returns 0
    path_a, path_b = tmp_path / "a.json", tmp_path / "b.json"
    path_a.write_text(json.dumps(RESULT_A), encoding="utf-8")
    path_b.write_text(json.dumps(RESULT_B), encoding="utf-8")
    assert main([str(path_a), str(path_b)]) == 0
    printed = capsys.readouterr().out
    assert "gpt-4o-mini" in printed and "66.7%" in printed
    assert re.search(r"\bc2\b", printed)

    # main on incomparable files -> returns 2 and prints something
    path_c = tmp_path / "c.json"
    path_c.write_text(json.dumps(other_split), encoding="utf-8")
    assert main([str(path_a), str(path_c)]) == 2
    captured = capsys.readouterr()
    assert (captured.out + captured.err).strip() != ""
