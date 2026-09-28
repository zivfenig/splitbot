"""Router eval: `python -m tests.llm_evals.run_router_eval --split dev|test --router embedding|jev`.

The only thresholded decision is binary: ignore vs pass (to the agent). The threshold is chosen
on dev (`choose_threshold`) and then FIXED for test (`--threshold-from <dev result file>`).
Real router calls happen only in `main()` with `--yes`; everything else is pure or takes an
injected `Router` (unit tests use fake routers). Datasets are the human's verified files and are
never edited by code.

Dataset rows (`datasets/router_dev.jsonl`, `router_test.jsonl`, `router_reference.jsonl`):
  {"id", "split": "dev|test|reference", "message", "label": "expense|query|ignore",
   "difficulty": "easy|hard" (null for reference), "source", "verified"}
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Literal

from splitbot.config import price_for, prices_fingerprint
from splitbot.router.base import LABELS, RouteResult, Router, decide
from splitbot.validation import normalize

DATASETS_DIR = Path(__file__).parent / "datasets"
RESULTS_DIR = Path(__file__).parent / "results"


class DatasetError(ValueError):
    """A dataset row or file is not usable (the data is never "fixed" by code)."""


@dataclass(frozen=True)
class RouterCase:
    id: str
    split: str  # "dev" | "test"
    message: str
    label: str  # "expense" (the action class: new, correction, delete) | "query" | "ignore"
    difficulty: str  # "easy" | "hard"


def load_router_cases(split: Literal["dev", "test"], datasets_dir: Path = DATASETS_DIR) -> list[RouterCase]:
    """Rows of `router_<split>.jsonl` in file order. Raises DatasetError when a row is not
    `"verified": true`, has a label outside expense/query/ignore or a difficulty outside
    easy/hard, its `split` differs from the requested one, an id is duplicated, or (for the
    other files that exist in `datasets_dir`: dev, test, reference) the same message text
    (compared with `validation.normalize`) or the same id appears in two files."""
    rows = _read_rows(datasets_dir / f"router_{split}.jsonl")
    cases: list[RouterCase] = []
    seen: set[str] = set()
    for row in rows:
        case = _to_case(row, split)
        if case.id in seen:
            raise DatasetError(f"{case.id}: duplicate id in the {split} file")
        seen.add(case.id)
        cases.append(case)
    for other in ("dev", "test", "reference"):
        path = datasets_dir / f"router_{other}.jsonl"
        if other == split or not path.exists():
            continue
        other_rows = _read_rows(path)
        other_ids = {r.get("id") for r in other_rows}
        other_texts = {normalize(str(r.get("message", ""))) for r in other_rows}
        for case in cases:
            if case.id in other_ids or normalize(case.message) in other_texts:
                raise DatasetError(f"{case.id}: also appears in the {other} file")
    return cases


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


def _to_case(row: dict, split: str) -> RouterCase:
    case_id = row.get("id", "?")
    if row.get("verified") is not True:
        raise DatasetError(f"{case_id}: not verified by the human")
    if row.get("split") != split:
        raise DatasetError(f"{case_id}: split is {row.get('split')!r}, expected {split!r}")
    if row.get("label") not in LABELS:
        raise DatasetError(f"{case_id}: unknown label {row.get('label')!r}")
    if row.get("difficulty") not in ("easy", "hard"):
        raise DatasetError(f"{case_id}: difficulty must be easy or hard")
    if not isinstance(row.get("message"), str) or not row["message"].strip():
        raise DatasetError(f"{case_id}: empty message")
    return RouterCase(id=case_id, split=split, message=row["message"], label=row["label"], difficulty=row["difficulty"])


# --- pure metrics ------------------------------------------------------------


def roc_auc(positive_scores: list[float], negative_scores: list[float]) -> float | None:
    """Area under the ROC curve: the probability that a random positive scores higher than a
    random negative, ties counting 0.5 (Mann-Whitney). None when either list is empty."""
    if not positive_scores or not negative_scores:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positive_scores for n in negative_scores)
    return wins / (len(positive_scores) * len(negative_scores))


Row = tuple[RouterCase, RouteResult]


def choose_threshold(rows: list[Row]) -> float:
    """The `ignore` threshold for `base.decide`, chosen on dev: the one with the highest
    ignore-filtered rate that routes ZERO expense rows to ignore (not Youden: the errors are not
    symmetric). Let m = the highest `scores["ignore"]` among rows labelled "expense". Let n = the
    smallest `scores["ignore"]` among rows labelled "ignore" that is > m. The threshold is (m + n)
    / 2 (midway, so dev has zero misses with some margin). If no ignore row scores above m,
    nothing can be filtered without missing an expense: return 1.01 (above any probability).
    Query rows do not constrain the choice. Error results count with their scores (ignore 0.0).
    Raises ValueError when `rows` has no expense row."""
    expense = [r.scores["ignore"] for case, r in rows if case.label == "expense"]
    if not expense:
        raise ValueError("cannot choose a threshold without any expense row")
    top = max(expense)
    above = [r.scores["ignore"] for case, r in rows if case.label == "ignore" and r.scores["ignore"] > top]
    return (top + min(above)) / 2 if above else 1.01


def compute_metrics(rows: list[Row], threshold: float) -> dict:
    """{"threshold": float, "overall": S, "easy": S, "hard": S} where S describes the rows of
    that difficulty ("overall" = all rows; a difficulty with no rows gives n = 0 and None rates):
      {"n": int, "counts": {label: int},
       "missed_expense_rate": expense rows decided "ignore" / expense rows,
       "ignore_filtered_rate": ignore rows decided "ignore" / ignore rows,
       "query_missed_rate": query rows decided "ignore" / query rows,
       "query_detection": query rows whose `result.label == "query"` / query rows,
       "accuracy": rows with `result.label == case.label` / rows,
       "per_class_recall": {label: recall or None},
       "confusion": {true label: {predicted label: count}} (all three labels, zeros included),
       "auc": `roc_auc` with the IGNORE rows' `scores["ignore"]` as positives and the other rows'
              as negatives,
       "errors": number of results with an error,
       "cost_usd": total as a plain decimal string, None if any result's cost is None,
       "cost_usd_per_call": total / n (8 decimals, ROUND_HALF_UP) or None,
       "latency_s": {"p50": float, "p95": float}}   # nearest-rank over the per-call latencies
    Rates are floats in [0, 1] or None when the denominator is 0. A row is decided with
    `base.decide(result, threshold)`."""
    sections = {
        "overall": rows,
        "easy": [row for row in rows if row[0].difficulty == "easy"],
        "hard": [row for row in rows if row[0].difficulty == "hard"],
    }
    return {"threshold": threshold, **{name: _section(part, threshold) for name, part in sections.items()}}


def _rate(hits: int, total: int) -> float | None:
    return hits / total if total else None


def _section(rows: list[Row], threshold: float) -> dict:
    by_label = {label: [(c, r) for c, r in rows if c.label == label] for label in LABELS}
    decided = lambda group: sum(decide(r, threshold) == "ignore" for _, r in group)  # noqa: E731
    confusion = {t: {p: sum(r.label == p for _, r in by_label[t]) for p in LABELS} for t in LABELS}
    positives = [r.scores["ignore"] for c, r in rows if c.label == "ignore"]
    negatives = [r.scores["ignore"] for c, r in rows if c.label != "ignore"]
    costs = [r.cost_usd for _, r in rows]
    total = None if not rows or any(c is None for c in costs) else sum(costs, Decimal(0))
    latencies = sorted(r.latency_s for _, r in rows)
    return {
        "n": len(rows),
        "counts": {label: len(by_label[label]) for label in LABELS},
        "missed_expense_rate": _rate(decided(by_label["expense"]), len(by_label["expense"])),
        "ignore_filtered_rate": _rate(decided(by_label["ignore"]), len(by_label["ignore"])),
        "query_missed_rate": _rate(decided(by_label["query"]), len(by_label["query"])),
        "query_detection": _rate(sum(r.label == "query" for _, r in by_label["query"]), len(by_label["query"])),
        "accuracy": _rate(sum(r.label == c.label for c, r in rows), len(rows)),
        "per_class_recall": {
            label: _rate(confusion[label][label], len(by_label[label])) for label in LABELS
        },
        "confusion": confusion,
        "auc": roc_auc(positives, negatives),
        "errors": sum(r.error is not None for _, r in rows),
        "cost_usd": None if total is None else format(total, "f"),
        "cost_usd_per_call": None
        if total is None
        else format((total / len(rows)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f"),
        "latency_s": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
        },
    }


def _percentile(sorted_values: list[float], percent: int) -> float:
    if not sorted_values:
        return 0.0
    rank = -(-percent * len(sorted_values) // 100)  # ceil(percent * n / 100), nearest-rank
    return sorted_values[max(rank, 1) - 1]


# --- running -----------------------------------------------------------------


def run_router_split(
    split: Literal["dev", "test"],
    *,
    router: Router,
    threshold: float | None,
    datasets_dir: Path = DATASETS_DIR,
    results_dir: Path = RESULTS_DIR,
    prices_path: Path | None = None,
    today: date,
) -> Path:
    """Route every message of the split ONCE, in file order, and write two files (creating the
    folder); returns the JSON path.

    `threshold`: for "dev" it must be None (the threshold is chosen with `choose_threshold`
    from the routed rows); for "test" it must be a number (fixed from dev) - otherwise
    ValueError before any router call.

    `<results_dir>/router_<router.name>_<split>_<today ISO>.json` has: "component": "router",
    "router": router.name, "model" (`router.model` when the router has that attribute, else
    null), "split", "date", "threshold", "threshold_chosen_on": "dev", "n_cases", "prices"
    ({"in", "out"} strings per 1M tokens from `config.price_for(model, prices_path)`, or null),
    "prices_sha" (`config.prices_fingerprint(prices_path)`), "setup" (`router.setup` when the
    router has it, else null), "metrics" (`compute_metrics`), and "messages": one entry per
    case: {"id", "message", "label", "difficulty", "scores", "predicted_label", "decision"
    ("ignore"|"pass"), "latency_s", "cost_usd" (string|None), "error" (string|None)}.

    `<results_dir>/failures_router_<router.name>_<split>_<today ISO>.md` is a readable gallery
    of the wrongly routed messages, in three sections in this order: "Missed expenses"
    (label expense, decided ignore: the serious kind), "Queries routed to ignore", and "Wrong
    label" (the router's own label differs from the truth, among messages that passed). Each
    entry shows the id, the message, the difficulty, the true label, the scores and the
    decision. A section without entries says "none"."""
    if (split == "dev") != (threshold is None):
        raise ValueError("dev chooses its own threshold (pass None); test needs the fixed dev threshold")
    cases = load_router_cases(split, datasets_dir)
    rows = [(case, router.route(case.message)) for case in cases]  # once, in file order
    chosen = choose_threshold(rows) if threshold is None else threshold
    metrics = compute_metrics(rows, chosen)

    model = getattr(router, "model", None)
    price = price_for(model, prices_path) if model else None
    setup = getattr(router, "setup", None)
    stamp = today.isoformat()
    result = {
        "component": "router",
        "router": router.name,
        "model": model,
        "split": split,
        "date": stamp,
        "threshold": chosen,
        "threshold_chosen_on": "dev",
        "n_cases": len(cases),
        "prices": None if price is None else {"in": format(price.in_per_mtok, "f"), "out": format(price.out_per_mtok, "f")},
        "prices_sha": prices_fingerprint(prices_path),
        "setup": None if setup is None else {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in setup.items()},
        "metrics": metrics,
        "messages": [
            {
                "id": case.id,
                "message": case.message,
                "label": case.label,
                "difficulty": case.difficulty,
                "scores": r.scores,
                "predicted_label": r.label,
                "decision": decide(r, chosen),
                "latency_s": r.latency_s,
                "cost_usd": None if r.cost_usd is None else format(r.cost_usd, "f"),
                "error": r.error,
            }
            for case, r in rows
        ],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"router_{router.name}_{split}_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    gallery = results_dir / f"failures_router_{router.name}_{split}_{stamp}.md"
    gallery.write_text(_gallery(router.name, split, stamp, chosen, result["messages"]), encoding="utf-8")
    return path


def _gallery(name: str, split: str, stamp: str, threshold: float, messages: list[dict]) -> str:
    def entry(m: dict) -> str:
        scores = " · ".join(f"{label} {m['scores'][label]:.2f}" for label in LABELS)
        return (f"- **{m['id']}** ({m['difficulty']}) true: {m['label']} · decision: {m['decision']} · "
                f"router label: {m['predicted_label']} · scores: {scores}\n  > {m['message']}")

    sections = [
        ("Missed expenses (an expense routed to ignore: the serious kind)",
         [m for m in messages if m["label"] == "expense" and m["decision"] == "ignore"]),
        ("Queries routed to ignore", [m for m in messages if m["label"] == "query" and m["decision"] == "ignore"]),
        ("Wrong label (the router's own label differs from the truth, among messages that passed)",
         [m for m in messages if m["decision"] == "pass" and m["predicted_label"] != m["label"]]),
    ]
    lines = [f"# Router failures: {name} · {split} · {stamp} · threshold {threshold:.4f}", ""]
    for title, items in sections:
        lines += [f"## {title}", ""]
        lines += [entry(m) for m in items] if items else ["none"]
        lines.append("")
    return "\n".join(lines)


_AVERAGED_FIELDS = (
    "missed_expense_rate", "ignore_filtered_rate", "query_missed_rate",
    "query_detection", "accuracy", "auc",
)


def combine_router_runs(result_paths: list[Path]) -> dict:
    """Post-hoc consistency summary across several EXISTING router result files -- reads no
    network and makes no router calls: pure aggregation of files `run_router_split` already
    wrote. Unlike `run_agent_eval.py`'s `run_consistency`, this harness has no built-in
    `--repeat`, so repeated runs (e.g. several separate `--yes` invocations of the same
    split/router) are only ever produced as independent files; this reads N of them together.

    Every file must be a `run_router_split` result for the SAME `router` and `split`, with the
    SAME set of case ids (same `n_cases`, same `messages[i]["id"]`s) -- raises ValueError, naming
    the mismatch, before returning anything, if not.

    Returns:
      {"component": "router_consistency", "router", "model", "split", "runs": len(result_paths),
       "source_files": [path.name, ...] (input order), "n_cases",
       "average_metrics": {"overall"/"easy"/"hard": {
           <each of missed_expense_rate/ignore_filtered_rate/query_missed_rate/query_detection/
           accuracy/auc>: the mean across files that have a non-None value for it, or None if
           none do;
           "n": summed (not averaged) -- the total number of routed rows across all files;
           "errors": summed -- total error results across all files;
           "counts"/"confusion": summed cell-by-cell across files;
           "cost_usd_per_call": mean of the files that have a cost, or None if none do;
           "latency_s": {"p50", "p95"}: mean of each across files}},
       "errors_per_run": [file's own "overall"."errors", in input order],
       "total_errors": sum(errors_per_run), "total_calls": n_cases * runs,
       "per_case_accuracy": {case_id: (files where that file's predicted_label == the true
           label) / runs, in dataset order (first file's message order)}}.
    A rate/mean that has no files to average (every file's value was None, e.g. no "hard" rows
    at all) is None, never 0 or an error.
    """
    if not result_paths:
        raise ValueError("need at least one result file")
    datasets = [json.loads(p.read_text(encoding="utf-8")) for p in result_paths]
    first = datasets[0]
    for data, path in zip(datasets, result_paths):
        if data.get("component") != "router":
            raise ValueError(f"{path.name} is not a router result file")
        if data.get("router") != first.get("router") or data.get("split") != first.get("split"):
            raise ValueError(f"{path.name} has a different router/split than {result_paths[0].name}")
        if {m["id"] for m in data["messages"]} != {m["id"] for m in first["messages"]}:
            raise ValueError(f"{path.name} covers a different set of case ids than {result_paths[0].name}")

    def avg_section(sections: list[dict]) -> dict:
        out: dict = {}
        for field in _AVERAGED_FIELDS:
            values = [s[field] for s in sections if s[field] is not None]
            out[field] = sum(values) / len(values) if values else None
        out["n"] = sum(s["n"] for s in sections)
        out["errors"] = sum(s["errors"] for s in sections)
        out["counts"] = {label: sum(s["counts"][label] for s in sections) for label in LABELS}
        out["confusion"] = {
            t: {p: sum(s["confusion"][t][p] for s in sections) for p in LABELS} for t in LABELS
        }
        costs = [Decimal(s["cost_usd_per_call"]) for s in sections if s["cost_usd_per_call"] is not None]
        out["cost_usd_per_call"] = format(sum(costs) / len(costs), "f") if costs else None
        out["latency_s"] = {
            key: sum(s["latency_s"][key] for s in sections) / len(sections) for key in ("p50", "p95")
        }
        return out

    average_metrics = {
        name: avg_section([data["metrics"][name] for data in datasets])
        for name in ("overall", "easy", "hard")
    }
    errors_per_run = [data["metrics"]["overall"]["errors"] for data in datasets]
    by_id = [{m["id"]: m for m in data["messages"]} for data in datasets]
    per_case_accuracy = {
        m["id"]: sum(run[m["id"]]["predicted_label"] == m["label"] for run in by_id) / len(datasets)
        for m in first["messages"]
    }
    return {
        "component": "router_consistency",
        "router": first["router"],
        "model": first.get("model"),
        "split": first["split"],
        "runs": len(result_paths),
        "source_files": [p.name for p in result_paths],
        "n_cases": first["n_cases"],
        "average_metrics": average_metrics,
        "errors_per_run": errors_per_run,
        "total_errors": sum(errors_per_run),
        "total_calls": first["n_cases"] * len(result_paths),
        "per_case_accuracy": per_case_accuracy,
    }


def read_threshold(dev_result_path: Path) -> float:
    """The "threshold" of a dev result file written by `run_router_split`. Raises ValueError
    when the file is not a router dev result."""
    data = json.loads(dev_result_path.read_text(encoding="utf-8"))
    threshold = data.get("threshold") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("component") != "router"
        or data.get("split") != "dev"
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
    ):
        raise ValueError(f"{dev_result_path.name} is not a router dev result")
    return float(threshold)


def main(
    argv: list[str] | None = None,
    *,
    router_factory: Callable[[str], Router] | None = None,
    today: Callable[[], date] = date.today,
) -> int:
    """CLI: `--split dev|test` (required), `--router embedding|jev` (required), `--threshold-from
    <dev result file>` (required for test, refused for dev), `--yes`.

    Without `--yes` it is a DRY RUN: load the cases, print the number of router calls (one per
    message; the embedding router also needs ONE setup call to embed the references) and return
    0 WITHOUT calling `router_factory`. With `--yes` it builds the router with
    `router_factory(name)` (default: EmbeddingRouter.from_env / JevRouter.from_env), runs
    `run_router_split` and prints the headline numbers (missed expense rate, ignore filtered
    rate, AUC, the threshold) and the file paths. DatasetError, a missing/invalid
    --threshold-from or a wrong combination of arguments: print the message and return 2.
    `main` reads the module-level DATASETS_DIR and RESULTS_DIR at CALL time (tests monkeypatch
    them)."""
    parser = argparse.ArgumentParser(prog="python -m tests.llm_evals.run_router_eval")
    parser.add_argument("--split", required=False, choices=["dev", "test"])
    parser.add_argument("--router", required=False, choices=["embedding", "jev"])
    parser.add_argument("--threshold-from", default=None, help="dev result file whose threshold is used (test only)")
    parser.add_argument("--yes", action="store_true", help="really call the router (costs a little money)")
    parser.add_argument(
        "--combine", nargs="+", metavar="RESULT_JSON",
        help="post-hoc consistency summary across N EXISTING result files (no router calls, no cost); "
             "ignores --split/--router/--threshold-from/--yes",
    )
    args = parser.parse_args(argv)

    if args.combine:
        try:
            summary = combine_router_runs([Path(p) for p in args.combine])
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"router_{summary['router']}_{summary['split']}_consistency_{summary['runs']}x"
        path = RESULTS_DIR / f"{stem}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        overall = summary["average_metrics"]["overall"]

        def show(value) -> str:
            return "-" if value is None else f"{value:.3f}"

        print(
            f"{summary['runs']} runs combined | missed expense rate {show(overall['missed_expense_rate'])} | "
            f"ignore filtered rate {show(overall['ignore_filtered_rate'])} | AUC {show(overall['auc'])} | "
            f"errors {summary['total_errors']}/{summary['total_calls']}"
        )
        print(f"result file: {path}")
        return 0

    if not args.split or not args.router:
        print("--split and --router are required unless --combine is given", file=sys.stderr)
        return 2

    try:
        if args.split == "dev" and args.threshold_from:
            raise ValueError("dev chooses its own threshold: do not pass --threshold-from")
        if args.split == "test" and not args.threshold_from:
            raise ValueError("test needs the fixed dev threshold: pass --threshold-from <dev result file>")
        cases = load_router_cases(args.split, DATASETS_DIR)
        threshold = read_threshold(Path(args.threshold_from)) if args.split == "test" else None
    except (DatasetError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.yes:
        extra = " + 1 setup call that embeds the reference messages" if args.router == "embedding" else ""
        print(f"{args.split}: {len(cases)} messages = {len(cases)} router calls{extra}")
        print("Dry run only. Add --yes to make the real calls.")
        return 0

    router = (router_factory or _default_router)(args.router)
    path = run_router_split(args.split, router=router, threshold=threshold, datasets_dir=DATASETS_DIR,
                            results_dir=RESULTS_DIR, today=today())
    overall = json.loads(path.read_text(encoding="utf-8"))
    metrics = overall["metrics"]["overall"]

    def show(value) -> str:
        return "-" if value is None else f"{value:.3f}"

    print(f"threshold {overall['threshold']:.4f} | missed expense rate {show(metrics['missed_expense_rate'])} | "
          f"ignore filtered rate {show(metrics['ignore_filtered_rate'])} | AUC {show(metrics['auc'])}")
    print(f"result file: {path}")
    return 0


def _default_router(name: str) -> Router:
    if name == "embedding":
        from splitbot.router.embedding_router import EmbeddingRouter

        return EmbeddingRouter.from_env()
    from splitbot.router.jev_router import JevRouter

    return JevRouter.from_env()


if __name__ == "__main__":
    raise SystemExit(main())
