"""Unit tests for tests/llm_evals/run_router_eval.py. Offline: fake routers, tiny datasets in tmp_path.

The real dataset files are only READ (in the loader test), never written.
"""

import json
import re
from datetime import date
from decimal import Decimal

import pytest

from splitbot import config
from splitbot.router.base import LABELS, RouteResult, decide
from tests.llm_evals import run_router_eval
from tests.llm_evals.run_router_eval import (
    DatasetError,
    RouterCase,
    choose_threshold,
    compute_metrics,
    load_router_cases,
    read_threshold,
    roc_auc,
    run_router_split,
)

TODAY = date(2026, 9, 26)


# --- small builders ------------------------------------------------------------


def case(id, label, difficulty="easy", message=None, split="dev"):
    return RouterCase(id=id, split=split, message=message or f"message {id}", label=label, difficulty=difficulty)


def res(expense, query, ignore, label, latency=0.1, cost=Decimal("0.001"), error=None):
    scores = {"expense": expense, "query": query, "ignore": ignore}
    return RouteResult(label=label, scores=scores, latency_s=latency, cost_usd=cost, error=error)


def ign(ignore, error=None):
    """A result with the given ignore score (the rest goes to expense); used for threshold tests."""
    label = "ignore" if ignore >= 0.5 else "expense"
    return res(1.0 - ignore, 0.0, ignore, label, error=error)


class FakeRouter:
    name = "fake"
    model = "fake-model"

    def __init__(self, script):
        self.setup = {"tokens": 30, "latency_s": 0.1, "cost_usd": None}
        self.script = script
        self.calls = []

    def route(self, message):
        self.calls.append(message)
        return self.script[message]


def row(id, message, label="expense", difficulty="easy", split="dev", verified=True):
    return {
        "id": id,
        "split": split,
        "message": message,
        "label": label,
        "difficulty": difficulty,
        "source": "test",
        "verified": verified,
    }


def write_datasets(directory, dev=None, test=None, reference=None):
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in (("dev", dev), ("test", test), ("reference", reference)):
        if rows is not None:
            text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
            (directory / f"router_{name}.jsonl").write_text(text, encoding="utf-8")


# (id, message, true label, difficulty, (expense, query, ignore) scores, the router's own label)
DEV_SPEC = [
    ("d1", "paid 50 for pizza", "expense", "easy", (0.9, 0.05, 0.05), "expense"),
    ("d2", "ok I will cover the taxi later", "expense", "hard", (0.2, 0.1, 0.7), "ignore"),
    ("d3", "bought milk 12", "expense", "easy", (0.8, 0.1, 0.1), "expense"),
    ("d4", "good morning everyone", "ignore", "easy", (0.03, 0.02, 0.95), "ignore"),
    ("d5", "lol did you see the game", "ignore", "hard", (0.05, 0.05, 0.9), "ignore"),
    ("d6", "haha nice one", "ignore", "hard", (0.5, 0.1, 0.4), "expense"),
    ("d7", "how much do I owe Dani", "query", "easy", (0.1, 0.8, 0.1), "query"),
    ("d8", "what was the sushi total again", "query", "hard", (0.05, 0.1, 0.85), "ignore"),
]
TEST_SPEC = [
    ("t1", "paid 30 for taxi", "expense", "easy", (0.7, 0.2, 0.1), "expense"),
    ("t2", "I will get the next one", "expense", "hard", (0.1, 0.05, 0.85), "ignore"),
    ("t3", "who owes me money", "query", "easy", (0.05, 0.05, 0.9), "ignore"),
    ("t4", "thanks so much", "ignore", "easy", (0.6, 0.2, 0.2), "expense"),
    ("t5", "lol", "ignore", "hard", (0.02, 0.03, 0.95), "ignore"),
]
# dev: m = 0.7 (d2, an expense scored ignore-like), n = 0.9 (d5) -> threshold 0.8
DEV_THRESHOLD = 0.8


def spec_rows(spec, split):
    return [row(i, msg, label, diff, split=split) for i, msg, label, diff, _, _ in spec]


def script_for(*specs):
    script = {}
    for spec in specs:
        for i, msg, _, _, (e, q, s), label in spec:
            cost = None if i == "d1" else Decimal("0.001")
            script[msg] = res(e, q, s, label, latency=0.1, cost=cost)
    return script


def make_workspace(tmp_path):
    datasets = tmp_path / "datasets"
    write_datasets(
        datasets,
        dev=spec_rows(DEV_SPEC, "dev"),
        test=spec_rows(TEST_SPEC, "test"),
        reference=[row("ref1", "bought bread 9", "expense", None, split="reference")],
    )
    return datasets, tmp_path / "results"


def gallery_sections(text):
    headings = ["Missed expenses", "Queries routed to ignore", "Wrong label"]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions), "sections are not in the required order"
    bounds = positions + [len(text)]
    return {h: text[bounds[k] + len(h) : bounds[k + 1]] for k, h in enumerate(headings)}


# --- 1. threshold ----------------------------------------------------------------


@pytest.mark.parametrize(
    "rows, expected, filtered",
    [
        pytest.param(
            [("e1", "expense", 0.1), ("e2", "expense", 0.3), ("i1", "ignore", 0.2), ("i2", "ignore", 0.6), ("i3", "ignore", 0.8)],
            0.45,  # m = 0.3, n = 0.6 (0.2 is below m and never filtered)
            {"i2", "i3"},
            id="midway_between_top_expense_and_lowest_ignore_above_it",
        ),
        pytest.param(
            [
                ("e1", "expense", 0.1),
                ("e2", "expense", 0.3),
                ("i1", "ignore", 0.2),
                ("i2", "ignore", 0.6),
                ("i3", "ignore", 0.8),
                ("q1", "query", 0.9),
                ("q2", "query", 0.5),
            ],
            0.45,
            {"i2", "i3"},
            id="query_rows_do_not_change_the_choice",
        ),
        pytest.param(
            [("e1", "expense", 0.2), ("i1", "ignore", 0.9)],
            0.55,
            {"i1"},
            id="one_expense_one_ignore",
        ),
        pytest.param(
            [("e1", "expense", 0.5), ("e2", "expense", 0.2), ("i1", "ignore", 0.5), ("i2", "ignore", 0.3)],
            1.01,  # no ignore row is ABOVE m = 0.5 (equal does not count)
            set(),
            id="no_ignore_above_top_expense_gives_1.01",
        ),
        pytest.param([("e1", "expense", 0.5)], 1.01, set(), id="no_ignore_rows_at_all_gives_1.01"),
        pytest.param(
            [("e1", "expense", 0.0, "timeout"), ("i0", "ignore", 0.0, "timeout"), ("i1", "ignore", 0.4), ("i2", "ignore", 0.7)],
            0.2,  # the error expense counts with ignore score 0.0 -> m = 0.0, n = 0.4
            {"i1", "i2"},
            id="error_results_count_with_ignore_score_zero",
        ),
        pytest.param([("i1", "ignore", 0.9), ("q1", "query", 0.2)], ValueError, None, id="no_expense_row_raises"),
        pytest.param([], ValueError, None, id="no_rows_raises"),
    ],
)
def test_threshold_is_the_highest_saving_that_misses_no_expense_on_dev(rows, expected, filtered):
    built = []
    for item in rows:
        id, label, score = item[:3]
        error = item[3] if len(item) > 3 else None
        built.append((case(id, label), ign(score, error)))

    if expected is ValueError:
        with pytest.raises(ValueError):
            choose_threshold(built)
        return

    threshold = choose_threshold(built)
    assert threshold == pytest.approx(expected)
    for c, r in built:
        decision = decide(r, threshold)
        if c.label == "expense":
            assert decision == "pass", f"expense {c.id} would be dropped"
        if c.label == "ignore":
            assert (decision == "ignore") == (c.id in filtered), f"ignore row {c.id}"


# --- 2. metrics ------------------------------------------------------------------


def test_metrics_missed_expense_ignore_filtered_query_detection_confusion_and_auc_split_by_difficulty():
    # roc_auc: probability that a positive scores higher than a negative, ties 0.5
    assert roc_auc([0.9, 0.8], [0.2, 0.1]) == 1.0
    assert roc_auc([0.2, 0.1], [0.9, 0.8]) == 0.0
    assert roc_auc([0.9, 0.5], [0.5, 0.1]) == pytest.approx(0.875)  # (1 + 0.5 + 1 + 1) / 4
    assert roc_auc([], [0.1]) is None
    assert roc_auc([0.1], []) is None

    rows = [
        # easy
        (case("E1", "expense", "easy"), res(0.8, 0.1, 0.1, "expense", latency=0.1, cost=Decimal("0.001"))),
        (case("E2", "ignore", "easy"), res(0.1, 0.1, 0.8, "ignore", latency=0.2, cost=Decimal("0.002"))),
        (case("E3", "query", "easy"), res(0.2, 0.7, 0.1, "query", latency=0.3, cost=Decimal("0.003"))),
        (case("E4", "query", "easy"), res(0.6, 0.3, 0.1, "expense", latency=0.4, cost=Decimal("0.004"))),
        # hard
        (case("H1", "expense", "hard"), res(0.3, 0.1, 0.6, "ignore", latency=0.5, cost=Decimal("0.005"))),
        (case("H2", "ignore", "hard"), res(0.5, 0.2, 0.3, "expense", latency=0.6, cost=Decimal("0.006"))),
        (case("H3", "expense", "hard"), res(1.0, 0.0, 0.0, "expense", latency=0.7, cost=None, error="timeout")),
    ]
    m = compute_metrics(rows, 0.5)
    assert m["threshold"] == 0.5

    zero = {"expense": 0, "query": 0, "ignore": 0}
    easy, hard, overall = m["easy"], m["hard"], m["overall"]

    # sizes and counts
    assert (overall["n"], easy["n"], hard["n"]) == (7, 4, 3)
    assert overall["counts"] == {"expense": 3, "query": 2, "ignore": 2}
    assert easy["counts"] == {"expense": 1, "query": 2, "ignore": 1}
    assert hard["counts"] == {"expense": 2, "query": 0, "ignore": 1}

    # rates (H1 is the only missed expense; E2 the only filtered ignore; no query is decided ignore)
    assert easy["missed_expense_rate"] == 0.0
    assert hard["missed_expense_rate"] == pytest.approx(0.5)
    assert overall["missed_expense_rate"] == pytest.approx(1 / 3)
    assert easy["ignore_filtered_rate"] == 1.0
    assert hard["ignore_filtered_rate"] == 0.0
    assert overall["ignore_filtered_rate"] == pytest.approx(0.5)
    assert easy["query_missed_rate"] == 0.0
    assert hard["query_missed_rate"] is None  # no query rows in hard
    assert overall["query_missed_rate"] == 0.0

    # query detection uses the router's label (E4 passes but is labelled expense), not the threshold
    assert easy["query_detection"] == pytest.approx(0.5)
    assert hard["query_detection"] is None
    assert overall["query_detection"] == pytest.approx(0.5)

    # accuracy and per-class recall
    assert easy["accuracy"] == pytest.approx(0.75)
    assert hard["accuracy"] == pytest.approx(1 / 3)
    assert overall["accuracy"] == pytest.approx(4 / 7)
    assert easy["per_class_recall"] == {"expense": 1.0, "query": pytest.approx(0.5), "ignore": 1.0}
    assert hard["per_class_recall"] == {"expense": pytest.approx(0.5), "query": None, "ignore": 0.0}
    assert overall["per_class_recall"] == {
        "expense": pytest.approx(2 / 3),
        "query": pytest.approx(0.5),
        "ignore": pytest.approx(0.5),
    }

    # full 3x3 confusion, zeros included
    assert easy["confusion"] == {
        "expense": {"expense": 1, "query": 0, "ignore": 0},
        "query": {"expense": 1, "query": 1, "ignore": 0},
        "ignore": {"expense": 0, "query": 0, "ignore": 1},
    }
    assert hard["confusion"] == {
        "expense": {"expense": 1, "query": 0, "ignore": 1},
        "query": zero,
        "ignore": {"expense": 1, "query": 0, "ignore": 0},
    }
    assert overall["confusion"] == {
        "expense": {"expense": 2, "query": 0, "ignore": 1},
        "query": {"expense": 1, "query": 1, "ignore": 0},
        "ignore": {"expense": 1, "query": 0, "ignore": 1},
    }
    assert set(overall["confusion"]) == set(LABELS)

    # AUC: ignore rows' ignore scores vs the others' (the error row counts with 0.0)
    assert easy["auc"] == pytest.approx(1.0)  # [0.8] vs [0.1, 0.1, 0.1]
    assert hard["auc"] == pytest.approx(0.5)  # [0.3] vs [0.6, 0.0]
    assert overall["auc"] == pytest.approx(0.9)  # [0.8, 0.3] vs [0.1, 0.1, 0.1, 0.6, 0.0] -> 9/10

    # errors
    assert (easy["errors"], hard["errors"], overall["errors"]) == (0, 1, 1)

    # cost: exact decimal string; one unknown cost makes the total unknown (never 0)
    assert isinstance(easy["cost_usd"], str)
    assert Decimal(easy["cost_usd"]) == Decimal("0.010")
    assert easy["cost_usd_per_call"] == "0.00250000"  # 0.010 / 4, 8 decimals
    assert hard["cost_usd"] is None and hard["cost_usd_per_call"] is None
    assert overall["cost_usd"] is None and overall["cost_usd_per_call"] is None

    # latency, nearest-rank: easy [0.1..0.4], hard [0.5, 0.6, 0.7], overall [0.1..0.7]
    assert easy["latency_s"] == {"p50": pytest.approx(0.2), "p95": pytest.approx(0.4)}
    assert hard["latency_s"] == {"p50": pytest.approx(0.6), "p95": pytest.approx(0.7)}
    assert overall["latency_s"] == {"p50": pytest.approx(0.4), "p95": pytest.approx(0.7)}

    # a difficulty without rows: n = 0 and None rates
    only_easy = compute_metrics(rows[:4], 0.5)
    assert only_easy["hard"]["n"] == 0
    assert only_easy["hard"]["missed_expense_rate"] is None
    assert only_easy["hard"]["accuracy"] is None


# --- 3. result file, loader, CLI ---------------------------------------------------


def test_router_eval_result_file_has_scores_thresholds_prices_and_failures_gallery(tmp_path, monkeypatch, capsys):
    # (a) loader on the REAL verified data (read only; no row counts pinned)
    dev_cases = load_router_cases("dev")
    test_cases = load_router_cases("test")
    for c in dev_cases + test_cases:
        assert c.label in ("expense", "query", "ignore")
        assert c.difficulty in ("easy", "hard")
    all_ids = [c.id for c in dev_cases + test_cases]
    assert len(all_ids) == len(set(all_ids)), "ids repeat across dev and test"

    # (a) loader on tiny datasets: every kind of bad data raises DatasetError
    ok = row("d1", "hello there")
    bad_datasets = {
        "unverified_row": ("dev", dict(dev=[row("d1", "m1", verified=False)])),
        "unknown_label": ("dev", dict(dev=[row("d1", "m1", label="refund")])),
        "bad_difficulty": ("dev", dict(dev=[row("d1", "m1", difficulty="medium")])),
        "split_field_differs": ("dev", dict(dev=[row("d1", "m1", split="test")])),
        "duplicate_id": ("dev", dict(dev=[row("d1", "m1"), row("d1", "m2")])),
        "same_message_dev_and_test": ("dev", dict(dev=[ok], test=[row("t1", "hello there", split="test")])),
        "same_message_seen_from_test": ("test", dict(dev=[ok], test=[row("t1", "hello there", split="test")])),
        "same_id_dev_and_test": ("dev", dict(dev=[row("x", "m1")], test=[row("x", "m2", split="test")])),
        "same_message_dev_and_reference": (
            "dev",
            dict(dev=[ok], reference=[row("ref1", "hello there", difficulty=None, split="reference")]),
        ),
        "same_id_dev_and_reference": (
            "dev",
            dict(dev=[row("x", "m1")], reference=[row("x", "m2", difficulty=None, split="reference")]),
        ),
    }
    for name, (split, files) in bad_datasets.items():
        directory = tmp_path / "bad" / name
        write_datasets(directory, **files)
        with pytest.raises(DatasetError):
            load_router_cases(split, directory)

    datasets, results = make_workspace(tmp_path)
    loaded = load_router_cases("dev", datasets)
    assert [c.id for c in loaded] == [s[0] for s in DEV_SPEC], "file order is kept"

    # (b) dev run: threshold chosen on dev, each message routed once in file order
    script = script_for(DEV_SPEC, TEST_SPEC)
    fake = FakeRouter(script)
    path = run_router_split(
        "dev", router=fake, threshold=None, datasets_dir=datasets, results_dir=results, today=TODAY
    )
    assert path == results / "router_fake_dev_2026-09-26.json"
    assert fake.calls == [s[1] for s in DEV_SPEC]

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [(c, script[c.message]) for c in loaded]
    assert data["component"] == "router"
    assert data["router"] == "fake"
    assert data["model"] == "fake-model"
    assert data["split"] == "dev"
    assert data["date"] == "2026-09-26"
    assert data["threshold"] == choose_threshold(rows)
    assert data["threshold"] == pytest.approx(DEV_THRESHOLD)
    assert data["threshold_chosen_on"] == "dev"
    assert data["n_cases"] == 8
    assert data["prices"] is None  # "fake-model" is not in the default price file
    assert re.fullmatch(r"[0-9a-f]{12}", data["prices_sha"])
    assert data["prices_sha"] == config.prices_fingerprint()
    assert data["setup"] == {"tokens": 30, "latency_s": 0.1, "cost_usd": None}
    assert data["metrics"] == json.loads(json.dumps(compute_metrics(rows, data["threshold"])))

    decisions = {"d4": "ignore", "d5": "ignore", "d8": "ignore"}
    assert [m["id"] for m in data["messages"]] == [s[0] for s in DEV_SPEC]
    for m, (i, msg, label, diff, (e, q, s), routed) in zip(data["messages"], DEV_SPEC):
        assert set(m) >= {
            "id", "message", "label", "difficulty", "scores", "predicted_label",
            "decision", "latency_s", "cost_usd", "error",
        }  # fmt: skip
        assert (m["message"], m["label"], m["difficulty"]) == (msg, label, diff)
        assert m["scores"] == {"expense": e, "query": q, "ignore": s}
        assert m["predicted_label"] == routed
        assert m["decision"] == decisions.get(i, "pass")
        assert m["latency_s"] == 0.1
        assert m["cost_usd"] == (None if i == "d1" else "0.001")
        assert m["error"] is None

    # (b) prices with a price file that lists the model
    prices_file = tmp_path / "prices.json"
    prices_file.write_text(
        json.dumps({"version": 1, "unit": "usd per 1M tokens", "models": {"fake-model": {"in": 1.5, "out": 6}}}),
        encoding="utf-8",
    )
    priced_path = run_router_split(
        "dev", router=FakeRouter(script), threshold=None, datasets_dir=datasets,
        results_dir=tmp_path / "results_priced", prices_path=prices_file, today=TODAY,
    )  # fmt: skip
    priced = json.loads(priced_path.read_text(encoding="utf-8"))
    assert isinstance(priced["prices"]["in"], str) and isinstance(priced["prices"]["out"], str)
    assert Decimal(priced["prices"]["in"]) == Decimal("1.5")
    assert Decimal(priced["prices"]["out"]) == Decimal("6")
    assert priced["prices_sha"] == config.prices_fingerprint(prices_file)

    # (b) dev gallery: "Missed expenses" is empty, the others list the failures
    dev_gallery = (results / "failures_router_fake_dev_2026-09-26.md").read_text(encoding="utf-8")
    sections = gallery_sections(dev_gallery)
    assert "none" in sections["Missed expenses"].lower()
    queries = sections["Queries routed to ignore"]
    assert "d8" in queries and "what was the sushi total again" in queries
    assert "hard" in queries and "query" in queries and "ignore" in queries and "0.85" in queries
    wrong = sections["Wrong label"]
    assert "d2" in wrong and "ok I will cover the taxi later" in wrong
    assert "d6" in wrong and "haha nice one" in wrong
    assert "d8" not in wrong  # decided ignore, so it is not a "passed with the wrong label" case
    for fine in ("d1", "d3", "d4", "d5", "d7"):
        assert not any(fine in body for body in sections.values()), f"{fine} was routed correctly"

    # (b) invalid threshold arguments raise BEFORE any router call
    guard = FakeRouter(script)
    with pytest.raises(ValueError):
        run_router_split("test", router=guard, threshold=None, datasets_dir=datasets, results_dir=results, today=TODAY)
    with pytest.raises(ValueError):
        run_router_split("dev", router=guard, threshold=0.5, datasets_dir=datasets, results_dir=results, today=TODAY)
    assert guard.calls == []

    # (b) test split uses exactly the threshold read from the dev result
    fixed = read_threshold(path)
    assert fixed == data["threshold"]
    test_router = FakeRouter(script)
    test_path = run_router_split(
        "test", router=test_router, threshold=fixed, datasets_dir=datasets, results_dir=results, today=TODAY
    )
    assert test_path == results / "router_fake_test_2026-09-26.json"
    assert test_router.calls == [s[1] for s in TEST_SPEC]
    test_data = json.loads(test_path.read_text(encoding="utf-8"))
    assert test_data["split"] == "test"
    assert test_data["threshold"] == fixed
    assert test_data["threshold_chosen_on"] == "dev"
    assert test_data["n_cases"] == 5
    assert {m["id"]: m["decision"] for m in test_data["messages"]} == {
        "t1": "pass", "t2": "ignore", "t3": "ignore", "t4": "pass", "t5": "ignore",
    }  # fmt: skip
    assert test_data["metrics"]["overall"]["missed_expense_rate"] == pytest.approx(0.5)  # t2 is missed

    # (b) test gallery: every section has its entry
    test_gallery = (results / "failures_router_fake_test_2026-09-26.md").read_text(encoding="utf-8")
    sections = gallery_sections(test_gallery)
    assert "t2" in sections["Missed expenses"] and "I will get the next one" in sections["Missed expenses"]
    assert "t3" in sections["Queries routed to ignore"] and "who owes me money" in sections["Queries routed to ignore"]
    assert "t4" in sections["Wrong label"] and "thanks so much" in sections["Wrong label"]
    for fine in ("t1", "t5"):
        assert not any(fine in body for body in sections.values())
    assert all("none" not in body.lower() for body in sections.values())

    # (b) read_threshold refuses files that are not a router dev result
    not_dev = {
        "test_result": test_path,
        "extraction_result": None,
        "not_json": None,
    }
    extraction = tmp_path / "extraction.json"
    extraction.write_text(json.dumps({"component": "extraction", "split": "dev", "threshold": 0.5}), encoding="utf-8")
    not_json = tmp_path / "not_json.json"
    not_json.write_text("this is not json", encoding="utf-8")
    not_dev.update(extraction_result=extraction, not_json=not_json)
    for name, bad in not_dev.items():
        with pytest.raises(ValueError):
            read_threshold(bad)

    # (c) main: dry run prints the call count and never builds the router
    cli_datasets, _ = make_workspace(tmp_path / "cli")
    cli_results = tmp_path / "cli" / "cli_results"
    monkeypatch.setattr(run_router_eval, "DATASETS_DIR", cli_datasets)
    monkeypatch.setattr(run_router_eval, "RESULTS_DIR", cli_results)

    def failing_factory(name):
        pytest.fail("router_factory must not be called")

    capsys.readouterr()
    assert run_router_eval.main(["--split", "dev", "--router", "jev"], router_factory=failing_factory) == 0
    assert re.search(r"\b8\b", capsys.readouterr().out), "the dry run says how many router calls (8 messages)"
    assert run_router_eval.main(["--split", "dev", "--router", "embedding"], router_factory=failing_factory) == 0
    out = capsys.readouterr().out
    assert re.search(r"\b8\b", out)
    assert "setup" in out.lower(), "the embedding router needs one setup call: say so"
    assert not cli_results.exists() or not any(cli_results.iterdir()), "a dry run writes nothing"

    # (c) main with --yes: builds the router, runs, writes both files, returns 0
    built = []

    def factory(name):
        built.append(name)
        return FakeRouter(script)

    fixed_today = lambda: TODAY  # noqa: E731
    argv = ["--split", "dev", "--router", "jev", "--yes"]
    assert run_router_eval.main(argv, router_factory=factory, today=fixed_today) == 0
    out = capsys.readouterr().out
    assert built == ["jev"]
    dev_json = cli_results / "router_fake_dev_2026-09-26.json"
    assert dev_json.exists()
    assert (cli_results / "failures_router_fake_dev_2026-09-26.md").exists()
    assert "router_fake_dev_2026-09-26.json" in out
    assert "auc" in out.lower()

    argv = ["--split", "test", "--router", "jev", "--threshold-from", str(dev_json), "--yes"]
    assert run_router_eval.main(argv, router_factory=factory, today=fixed_today) == 0
    capsys.readouterr()
    assert (cli_results / "router_fake_test_2026-09-26.json").exists()

    # (c) wrong arguments and bad data: message printed, return 2, no router built
    bad_argvs = {
        "test_without_threshold_from": ["--split", "test", "--router", "jev", "--yes"],
        "dev_with_threshold_from": ["--split", "dev", "--router", "jev", "--threshold-from", str(dev_json), "--yes"],
        "missing_threshold_file": [
            "--split", "test", "--router", "jev", "--threshold-from", str(tmp_path / "nope.json"), "--yes",
        ],
    }  # fmt: skip
    for name, bad in bad_argvs.items():
        assert run_router_eval.main(bad, router_factory=failing_factory, today=fixed_today) == 2, name
        captured = capsys.readouterr()
        assert (captured.out + captured.err).strip(), f"{name}: a message is printed"

    broken = tmp_path / "broken_datasets"
    write_datasets(broken, dev=[row("d1", "m1", verified=False)], test=[])
    monkeypatch.setattr(run_router_eval, "DATASETS_DIR", broken)
    for extra in ([], ["--yes"]):
        argv = ["--split", "dev", "--router", "jev", *extra]
        assert run_router_eval.main(argv, router_factory=failing_factory, today=fixed_today) == 2
        captured = capsys.readouterr()
        assert (captured.out + captured.err).strip()
