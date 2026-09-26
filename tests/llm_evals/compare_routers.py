"""Side-by-side comparison of two router result files (same split, e.g. the test split).

  python -m tests.llm_evals.compare_routers <result_a.json> <result_b.json>

Files are the JSON written by `run_router_eval.run_router_split`. Pure code.
"""

import json
import sys
from pathlib import Path

_SECTIONS = ("overall", "easy", "hard")
_METRICS = (
    "missed_expense_rate", "ignore_filtered_rate", "query_missed_rate", "query_detection", "accuracy",
    "auc", "errors", "cost_usd", "cost_usd_per_call", "latency_p50_s", "latency_p95_s", "n",
)
_RATES = {"missed_expense_rate", "ignore_filtered_rate", "query_missed_rate", "query_detection", "accuracy"}


def compare(a: dict, b: dict) -> dict:
    """Raises ValueError when the splits differ or the message ids differ. Returns:
      {"routers": [a["router"], b["router"]], "split": str, "thresholds": [ta, tb],
       "sections": {"overall"|"easy"|"hard": {metric: [value_a, value_b]}}}
    with the metrics missed_expense_rate, ignore_filtered_rate, query_missed_rate, query_detection,
    accuracy, auc, errors, cost_usd, cost_usd_per_call, latency_p50_s, latency_p95_s, n
    (taken from each file's `metrics`)."""
    if a["split"] != b["split"]:
        raise ValueError("the two results are not comparable: different split")
    if {m["id"] for m in a["messages"]} != {m["id"] for m in b["messages"]}:
        raise ValueError("the two results are not comparable: different message ids")

    def value(section: dict, metric: str):
        if metric == "latency_p50_s":
            return section["latency_s"]["p50"]
        if metric == "latency_p95_s":
            return section["latency_s"]["p95"]
        return section.get(metric)

    return {
        "routers": [a["router"], b["router"]],
        "split": a["split"],
        "thresholds": [a["threshold"], b["threshold"]],
        "sections": {
            name: {m: [value(a["metrics"][name], m), value(b["metrics"][name], m)] for m in _METRICS}
            for name in _SECTIONS
        },
    }


def render(comparison: dict) -> str:
    """Markdown: a title `# <router a> vs <router b> (<split>)`, the thresholds, and one table
    per section (overall, easy, hard) with one row per metric and one column per router (rates
    as percent with one decimal, AUC with 3 decimals, cost as given, latency in seconds, "-"
    for None)."""
    ra, rb = comparison["routers"]

    def cell(metric: str, v) -> str:
        if v is None:
            return "-"
        if metric in _RATES:
            return f"{v * 100:.1f}%"
        if metric == "auc":
            return f"{v:.3f}"
        if metric.startswith("latency"):
            return f"{v:.2f}s"
        return str(v)

    lines = [
        f"# {ra} vs {rb} ({comparison['split']})",
        "",
        f"thresholds (ignore vs pass): {ra} {comparison['thresholds'][0]} · {rb} {comparison['thresholds'][1]}",
    ]
    for name in _SECTIONS:
        lines += ["", f"## {name}", "", f"| metric | {ra} | {rb} |", "|---|---|---|"]
        for metric, (x, y) in comparison["sections"][name].items():
            lines.append(f"| {metric} | {cell(metric, x)} | {cell(metric, y)} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Two result file paths; prints `render(compare(...))` and returns 0; ValueError/OSError/
    KeyError or a wrong argument count prints a message and returns 2."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m tests.llm_evals.compare_routers <result_a.json> <result_b.json>", file=sys.stderr)
        return 2
    try:
        results = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args]
        print(render(compare(*results)))
    except (ValueError, KeyError, OSError) as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
