import copy
import json
import re

import pytest

from tests.llm_evals.compare_routers import compare, main, render


def _section(n, missed, ignore_f, query_missed, query_det, acc, auc, errors, cost, per_call, p50, p95):
    return {
        "n": n,
        "missed_expense_rate": missed,
        "ignore_filtered_rate": ignore_f,
        "query_missed_rate": query_missed,
        "query_detection": query_det,
        "accuracy": acc,
        "auc": auc,
        "errors": errors,
        "cost_usd": cost,
        "cost_usd_per_call": per_call,
        "latency_s": {"p50": p50, "p95": p95},
    }


def _result(router, threshold, split, ids, metrics):
    return {
        "router": router,
        "split": split,
        "threshold": threshold,
        "metrics": {"threshold": threshold, **metrics},
        "messages": [{"id": i} for i in ids],
    }


IDS = ["m1", "m2", "m3", "m4"]

# embedding: has a cost; easy/hard have no query rows in "easy" -> query_detection None.
EMBEDDING = _result(
    "embedding", 0.35, "test", IDS,
    {
        "overall": _section(4, 0.125, 0.875, 0.25, 0.5, 0.75, 0.912, 1, "0.0012", "0.0003", 0.25, 0.5),
        "easy": _section(2, 0.0, 1.0, 0.5, None, 1.0, 0.98, 0, "0.0004", "0.0002", 0.125, 0.25),
        "hard": _section(2, 0.25, 0.75, 0.0, 1.0, 0.5, 0.844, 1, "0.0008", "0.0004", 0.375, 0.75),
    },
)

# jev: cost is None everywhere (the price of the model is unknown).
JEV = _result(
    "jev", 0.5, "test", IDS,
    {
        "overall": _section(4, 0.0625, 0.5, 0.125, 0.75, 0.625, 0.801, 0, None, None, 1.5, 2.5),
        "easy": _section(2, 0.0, 0.625, 0.0, 1.0, 0.875, 0.9, 0, None, None, 1.25, 2.0),
        "hard": _section(2, 0.375, 0.25, 0.5, 0.25, 0.375, 0.7, 0, None, None, 1.75, 3.0),
    },
)

METRICS = [
    "missed_expense_rate", "ignore_filtered_rate", "query_missed_rate", "query_detection",
    "accuracy", "auc", "errors", "cost_usd", "cost_usd_per_call",
    "latency_p50_s", "latency_p95_s", "n",
]


def test_router_comparison_table_shows_both_routers_side_by_side(tmp_path, capsys):
    result = compare(EMBEDDING, JEV)

    assert result["routers"] == ["embedding", "jev"]
    assert result["split"] == "test"
    assert result["thresholds"] == [0.35, 0.5]
    assert set(result["sections"]) == {"overall", "easy", "hard"}
    for section in result["sections"].values():
        assert set(METRICS) <= set(section)

    overall = result["sections"]["overall"]
    assert overall["n"] == [4, 4]
    assert overall["missed_expense_rate"] == [0.125, 0.0625]
    assert overall["ignore_filtered_rate"] == [0.875, 0.5]
    assert overall["query_missed_rate"] == [0.25, 0.125]
    assert overall["query_detection"] == [0.5, 0.75]
    assert overall["accuracy"] == [0.75, 0.625]
    assert overall["auc"] == [0.912, 0.801]
    assert overall["errors"] == [1, 0]
    assert overall["cost_usd"] == ["0.0012", None]
    assert overall["cost_usd_per_call"] == ["0.0003", None]
    assert overall["latency_p50_s"] == [0.25, 1.5]
    assert overall["latency_p95_s"] == [0.5, 2.5]

    easy = result["sections"]["easy"]
    assert easy["n"] == [2, 2]
    assert easy["query_detection"] == [None, 1.0]
    assert easy["latency_p50_s"] == [0.125, 1.25]

    hard = result["sections"]["hard"]
    assert hard["accuracy"] == [0.5, 0.375]
    assert hard["auc"] == [0.844, 0.7]
    assert hard["latency_p95_s"] == [0.75, 3.0]

    # incomparable inputs
    other_split = copy.deepcopy(JEV)
    other_split["split"] = "dev"
    with pytest.raises(ValueError):
        compare(EMBEDDING, other_split)
    other_ids = copy.deepcopy(JEV)
    other_ids["messages"] = [{"id": i} for i in ["m1", "m2", "m3", "m5"]]
    with pytest.raises(ValueError):
        compare(EMBEDDING, other_ids)

    # rendering
    text = render(result)
    assert "embedding" in text.splitlines()[0]
    assert "jev" in text.splitlines()[0]
    assert "test" in text.splitlines()[0]
    assert "0.35" in text and "0.5" in text
    for name in ("overall", "easy", "hard"):
        assert name in text
    assert "87.5%" in text
    assert "12.5%" in text
    assert "6.2%" in text or "6.3%" in text  # 0.0625 -> one decimal (either rounding)
    assert "0.912" in text
    assert "0.801" in text
    assert "0.0012" in text
    assert "None" not in text
    # "-" cells: embedding easy query_detection, jev cost + cost per call in 3 sections
    assert len(re.findall(r"(?<=\|)\s*-\s*(?=\|)", text)) == 7

    # main on two files
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    path_a.write_text(json.dumps(EMBEDDING))
    path_b.write_text(json.dumps(JEV))
    assert main([str(path_a), str(path_b)]) == 0
    assert text in capsys.readouterr().out

    # main refuses incomparable files and a wrong number of arguments
    path_dev = tmp_path / "dev.json"
    path_dev.write_text(json.dumps(other_split))
    assert main([str(path_a), str(path_dev)]) == 2
    captured = capsys.readouterr()
    assert (captured.out + captured.err).strip() != ""
    assert main([str(path_a)]) == 2
    captured = capsys.readouterr()
    assert (captured.out + captured.err).strip() != ""
