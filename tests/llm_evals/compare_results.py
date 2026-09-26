"""Side-by-side comparison of two eval result files (same split; models and/or prompt versions may differ).

  python -m tests.llm_evals.compare_results <result_a.json> <result_b.json>

Result files are the JSON written by `run_evals.run_split`. Pure code, no LLM, no network.
"""

import json
import sys
from pathlib import Path

_SUMMARY = (
    ("full_case_accuracy", lambda o: o["extraction"]["full_case_accuracy"]),
    ("grounding_failure_rate", lambda o: o["grounding_failure_rate"]),
    ("consistency", lambda o: o["consistency"]),
    ("cost_usd", lambda o: o["cost_usd"]),
    ("cost_usd_per_call", lambda o: o["cost_usd_per_call"]),
    ("cost_usd_per_correct", lambda o: o["cost_usd_per_correct"]),
    ("latency_p50_s", lambda o: o["latency_s"]["p50"]),
    ("latency_p95_s", lambda o: o["latency_s"]["p95"]),
)
_RATES = {"full_case_accuracy", "grounding_failure_rate", "consistency"}


def load_result(path: Path) -> dict:
    """Read one result file (UTF-8 JSON)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare(a: dict, b: dict) -> dict:
    """Compare two result dicts. Raises ValueError when their "split" differs or their case ids
    differ (different prompt versions and/or models ARE comparable). Returns:

      {"models": [a["model"], b["model"]],
       "prompts": [a["prompt_version"], b["prompt_version"]],
       "prices": [a["prices"], b["prices"]],
       "fields": {field: [accuracy_a, accuracy_b]},   # report["overall"]["extraction"]["per_field_accuracy"],
                                                      # every field that appears in either file
       "type": [{"accuracy", "false_expense_rate", "missed_expense_rate"} for a, for b],
       "summary": {metric: [value_a, value_b]}, with the metrics: full_case_accuracy,
                  grounding_failure_rate, consistency, cost_usd (total), cost_usd_per_call,
                  cost_usd_per_correct, latency_p50_s, latency_p95_s,
       "disagreements": [{"case_id": str, "message"?: absent, "correct_runs": [x, y],
                          "failed_fields": [[fields failed in a], [fields failed in b]],
                          "failures": [[{"run", "field", "expected", "actual"}, ...] for a,
                                       [...] for b]}]}

    A case is a DISAGREEMENT when the two files differ in `full_correct_runs` for it, or in the
    set of fields that failed in any of its runs (the union over its runs). Sorted by case id.
    Values are taken from each file's `report` as they are (numbers, strings or None).
    """
    if a["split"] != b["split"]:
        raise ValueError("the two results are not comparable: different split")
    per_case_a, per_case_b = a["report"]["per_case"], b["report"]["per_case"]
    if set(per_case_a) != set(per_case_b):
        raise ValueError("the two results are not comparable: different case ids")

    overall_a, overall_b = a["report"]["overall"], b["report"]["overall"]
    fields_a = overall_a["extraction"]["per_field_accuracy"]
    fields_b = overall_b["extraction"]["per_field_accuracy"]
    names = list(fields_a) + [f for f in fields_b if f not in fields_a]

    def failures_by_case(result: dict) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for f in result["report"]["failures"]:
            grouped.setdefault(f["case_id"], []).append(
                {"run": f["run"], "field": f["field"], "expected": f["expected"], "actual": f["actual"]}
            )
        return grouped

    fail_a, fail_b = failures_by_case(a), failures_by_case(b)
    disagreements = []
    for case_id in sorted(per_case_a):
        runs = [per_case_a[case_id]["full_correct_runs"], per_case_b[case_id]["full_correct_runs"]]
        lists = [fail_a.get(case_id, []), fail_b.get(case_id, [])]
        failed = [sorted({f["field"] for f in lst}) for lst in lists]
        if runs[0] != runs[1] or failed[0] != failed[1]:
            disagreements.append(
                {"case_id": case_id, "correct_runs": runs, "failed_fields": failed, "failures": lists}
            )
    return {
        "models": [a["model"], b["model"]],
        "prompts": [a["prompt_version"], b["prompt_version"]],
        "prices": [a["prices"], b["prices"]],
        "fields": {name: [fields_a.get(name), fields_b.get(name)] for name in names},
        "type": [overall_a["type"], overall_b["type"]],
        "summary": {name: [get(overall_a), get(overall_b)] for name, get in _SUMMARY},
        "disagreements": disagreements,
    }


def render(comparison: dict) -> str:
    """The comparison as markdown text. Each side is labelled "<model> (<prompt_version>)" (used as
    the column header and in the title `# <label a> vs <label b>`). A per-field table with one column per side (accuracy as
    percent with one decimal, "-" for a missing value), the summary table, and the list of
    disagreements (case id, correct runs for each model, and each model's failed fields)."""
    model_a, model_b = (f"{m} ({p})" for m, p in zip(comparison["models"], comparison["prompts"]))

    def pct(value) -> str:
        return "-" if value is None else f"{value * 100:.1f}%"

    def cell(name: str, value) -> str:
        if value is None:
            return "-"
        if name in _RATES:
            return pct(value)
        if name.startswith("latency"):
            return f"{value:.2f}s"
        return str(value)

    lines = [f"# {model_a} vs {model_b}", "", "## Per field (accuracy)", "",
             f"| field | {model_a} | {model_b} |", "|---|---|---|"]
    for name, (x, y) in comparison["fields"].items():
        lines.append(f"| {name} | {pct(x)} | {pct(y)} |")
    lines += ["", "## Type", "", f"| metric | {model_a} | {model_b} |", "|---|---|---|"]
    for key in ("accuracy", "false_expense_rate", "missed_expense_rate"):
        x, y = (t[key] for t in comparison["type"])
        lines.append(f"| {key} | {pct(x)} | {pct(y)} |")
    lines += ["", "## Summary", "", f"| metric | {model_a} | {model_b} |", "|---|---|---|"]
    for name, (x, y) in comparison["summary"].items():
        lines.append(f"| {name} | {cell(name, x)} | {cell(name, y)} |")
    disagreements = comparison["disagreements"]
    lines += ["", f"## Disagreements ({len(disagreements)})", ""]
    for d in disagreements:
        (ra, rb), (fa, fb) = d["correct_runs"], d["failed_fields"]
        lines.append(f"- {d['case_id']}: {model_a} {ra} correct runs, failed {fa or 'nothing'}; "
                     f"{model_b} {rb} correct runs, failed {fb or 'nothing'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: two result file paths; prints `render(compare(...))` and returns 0. A ValueError
    (files not comparable) prints the message and returns 2."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m tests.llm_evals.compare_results <result_a.json> <result_b.json>", file=sys.stderr)
        return 2
    try:
        print(render(compare(load_result(Path(args[0])), load_result(Path(args[1])))))
    except (ValueError, KeyError, OSError) as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
