"""Readable failures gallery for one eval result file: `results/failures_<result stem>.md`.

  python -m tests.llm_evals.failures_gallery <result.json> [<result.json> ...]

Pure code, no LLM, no network. Result files written before the case texts were stored have no
"cases" entry: their messages are joined from the dataset files (read-only).
"""

import json
from pathlib import Path

DATASETS_DIR = Path(__file__).parent / "datasets"


def load_dataset_messages(split: str, datasets_dir: Path = DATASETS_DIR) -> dict[str, str]:
    """case id -> message, read from `extraction_<split>.jsonl` in `datasets_dir` (read-only)."""
    path = datasets_dir / f"extraction_{split}.jsonl"
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {row["id"]: row["message"] for row in rows}


def build_gallery(result: dict, messages: dict[str, str] | None = None) -> str:
    """Markdown for one result dict (`report.failures` lists case_id, run, field, expected,
    actual). Layout:
      - a title line with the model, prompt version, split, date and number of runs per case;
      - then, for every case that has at least one failure, sorted by case id, a section
        `### <case id>` followed by the message and a line with the model and the number of
        correct runs ("correct runs: x/3", from `report.per_case`), and a table with one row per
        distinct (field, expected, actual) and the runs it happened in (`runs 1, 2`);
      - the message comes from `result["cases"][case_id]["message"]` when present, else from
        `messages`, else the text "(message unknown)";
      - cases without failures are not listed; a final line says how many cases have failures
        out of how many, or that there were no failures.
    """
    per_case = result["report"]["per_case"]
    texts = result.get("cases", {})
    by_case: dict[str, dict[tuple, list[int]]] = {}
    for failure in result["report"]["failures"]:
        rows = by_case.setdefault(failure["case_id"], {})
        rows.setdefault((failure["field"], failure["expected"], failure["actual"]), []).append(failure["run"])

    lines = [
        f"# Failures: {result['model']} · {result['prompt_version']} · {result['split']} · "
        f"{result['date']} · {result['runs_per_case']} runs per case",
        "",
    ]
    for case_id in sorted(by_case):
        message = (texts.get(case_id) or {}).get("message") or (messages or {}).get(case_id) or "(message unknown)"
        info = per_case[case_id]
        lines += [
            f"### {case_id}",
            "",
            f"> {message}",
            "",
            f"model: {result['model']} · prompt: {result['prompt_version']} · "
            f"correct runs: {info['full_correct_runs']}/{info['runs']}",
            "",
            "| field | expected | actual | runs |",
            "|---|---|---|---|",
        ]
        for (field, expected, actual), runs in by_case[case_id].items():
            lines.append(f"| {field} | {_cell(expected)} | {_cell(actual)} | runs {', '.join(str(r) for r in sorted(set(runs)))} |")
        lines.append("")
    if by_case:
        lines.append(f"{len(by_case)} of {len(per_case)} cases have failures.")
    else:
        lines.append(f"No failures: all {len(per_case)} cases were fully correct in every run.")
    return "\n".join(lines) + "\n"


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def gallery_path(result_path: Path, out_dir: Path | None = None) -> Path:
    """`<out_dir or the result file's folder>/failures_<result file stem>.md`."""
    return (out_dir if out_dir is not None else result_path.parent) / f"failures_{result_path.stem}.md"


def main(argv: list[str] | None = None) -> int:
    """CLI: one or more result file paths. For each, load the result, join dataset messages
    (`load_dataset_messages(result["split"])`) only when the result has no "cases", write the
    gallery to `gallery_path(path)`, print the written path, and return 0. Returns 2 (with a
    message) when no path is given or a file cannot be read."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m tests.llm_evals.failures_gallery <result.json> [<result.json> ...]")
        return 2
    for name in args:
        path = Path(name)
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            joined = None if "cases" in result else load_dataset_messages(result["split"], DATASETS_DIR)
            out = gallery_path(path)
            out.write_text(build_gallery(result, joined), encoding="utf-8")
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot write a gallery for {name}: {type(exc).__name__}")
            return 2
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
