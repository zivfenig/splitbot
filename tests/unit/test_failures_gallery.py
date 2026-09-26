import copy
import json

from tests.llm_evals import failures_gallery
from tests.llm_evals.failures_gallery import (
    build_gallery,
    gallery_path,
    load_dataset_messages,
    main,
)

MSG_A = "paid 240 for sushi, without Dani"
MSG_C = "קניתי חלב לחם וביצים 38.90"


def _result(with_cases: bool = True) -> dict:
    result = {
        "prompt_version": "extract_v1",
        "split": "dev",
        "date": "2026-09-26",
        "model": "gpt-4o-mini",
        "runs_per_case": 3,
        "report": {
            "per_case": {
                "case-A": {"full_correct_runs": 0, "runs": 3},
                "case-B": {"full_correct_runs": 3, "runs": 3},
                "case-C": {"full_correct_runs": 2, "runs": 3},
            },
            "failures": [
                # case-A: runs 1 and 2 fail the same way, run 3 fails differently
                {"case_id": "case-A", "run": 1, "field": "amount", "expected": "240.00", "actual": "24.00"},
                {"case_id": "case-A", "run": 2, "field": "amount", "expected": "240.00", "actual": "24.00"},
                {"case_id": "case-A", "run": 3, "field": "participants", "expected": "EXPECTED_P", "actual": "ACTUAL_P"},
                # case-C (Hebrew): one failed run
                {"case_id": "case-C", "run": 2, "field": "payer", "expected": "PAYER_E", "actual": "PAYER_A"},
            ],
        },
    }
    if with_cases:
        result["cases"] = {
            "case-A": {"message": MSG_A, "roster": "A", "sender_id": 1},
            "case-B": {"message": "message of the clean case", "roster": "A", "sender_id": 1},
            "case-C": {"message": MSG_C, "roster": "A", "sender_id": 1},
        }
    return result


def _section(text: str, case_id: str) -> str:
    """The part of the markdown from `### <case_id>` up to the next `### ` heading."""
    assert f"### {case_id}" in text
    return text.split(f"### {case_id}", 1)[1].split("### ", 1)[0]


def _row(section: str, *tokens: str) -> str:
    """The one line of the section that contains all the tokens."""
    rows = [line for line in section.splitlines() if all(t in line for t in tokens)]
    assert len(rows) == 1, (tokens, rows)
    return rows[0]


def test_failures_gallery_shows_message_expected_actual_and_model_per_failed_case(tmp_path, monkeypatch, capsys):
    # --- header: model, prompt version, split, date
    text = build_gallery(_result())
    for word in ("gpt-4o-mini", "extract_v1", "dev", "2026-09-26"):
        assert word in text

    # --- case with several failing runs: message, x/3, one row per distinct failure
    a = _section(text, "case-A")
    assert MSG_A in a
    assert "gpt-4o-mini" in a
    assert "correct runs: 0/3" in a
    assert "runs 1, 2" in _row(a, "amount", "240.00", "24.00")  # the two identical failures are merged
    assert "runs 3" in _row(a, "participants", "EXPECTED_P", "ACTUAL_P")
    assert "runs 3" not in _row(a, "amount", "240.00", "24.00")
    assert len([ln for ln in a.splitlines() if "amount" in ln]) == 1

    # --- Hebrew case
    c = _section(text, "case-C")
    assert MSG_C in c
    assert "correct runs: 2/3" in c
    assert "runs 2" in _row(c, "payer", "PAYER_E", "PAYER_A")

    # --- the case with no failures is not listed at all
    assert "case-B" not in text
    assert "message of the clean case" not in text

    # --- message source: result["cases"] wins over the `messages` argument
    text = build_gallery(_result(with_cases=True), messages={"case-A": "OTHER TEXT", "case-C": "OTHER TEXT"})
    assert MSG_A in text
    assert "OTHER TEXT" not in text

    # --- no "cases": the `messages` argument is used
    text = build_gallery(_result(with_cases=False), messages={"case-A": MSG_A, "case-C": MSG_C})
    assert MSG_A in _section(text, "case-A")
    assert MSG_C in _section(text, "case-C")
    assert "(message unknown)" not in text

    # --- neither: the placeholder text
    text = build_gallery(_result(with_cases=False))
    assert "(message unknown)" in _section(text, "case-A")
    assert "(message unknown)" in _section(text, "case-C")
    assert "correct runs: 0/3" in _section(text, "case-A")  # still shown without the message

    # --- no failures at all: says so, no case sections
    clean = copy.deepcopy(_result())
    clean["report"]["failures"] = []
    clean["report"]["per_case"] = {k: {"full_correct_runs": 3, "runs": 3} for k in ("case-A", "case-B", "case-C")}
    text = build_gallery(clean)
    assert "no failures" in text.lower()
    assert "###" not in text
    assert "case-A" not in text

    # --- load_dataset_messages: {id: message} from extraction_<split>.jsonl
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    rows = [
        {"id": "case-A", "message": MSG_A, "verified": True},
        {"id": "case-C", "message": MSG_C, "verified": True},
    ]
    (datasets / "extraction_dev.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    assert load_dataset_messages("dev", datasets) == {"case-A": MSG_A, "case-C": MSG_C}

    # --- gallery_path: next to the result file, or in out_dir
    result_file = tmp_path / "results" / "extraction_gpt-4o-mini_extract_v1_dev_2026-09-26.json"
    expected_name = "failures_extraction_gpt-4o-mini_extract_v1_dev_2026-09-26.md"
    assert gallery_path(result_file) == tmp_path / "results" / expected_name
    other = tmp_path / "elsewhere"
    assert gallery_path(result_file, out_dir=other) == other / expected_name

    # --- main: two result files, one without "cases" (messages come from the dataset)
    results = tmp_path / "results"
    results.mkdir()
    with_cases = results / "r_with_cases.json"
    without_cases = results / "r_without_cases.json"
    with_cases.write_text(json.dumps(_result(with_cases=True), ensure_ascii=False), encoding="utf-8")
    without_cases.write_text(json.dumps(_result(with_cases=False), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(failures_gallery, "DATASETS_DIR", datasets)

    assert main([str(with_cases), str(without_cases)]) == 0
    out = capsys.readouterr().out
    g1 = results / "failures_r_with_cases.md"
    g2 = results / "failures_r_without_cases.md"
    assert g1.exists() and g2.exists()
    assert str(g1) in out and str(g2) in out
    assert MSG_A in g2.read_text(encoding="utf-8")  # joined from the dataset
    assert MSG_C in g2.read_text(encoding="utf-8")
    assert "(message unknown)" not in g2.read_text(encoding="utf-8")
    assert MSG_A in g1.read_text(encoding="utf-8")

    # --- main: bad usage
    assert main([]) == 2
    assert capsys.readouterr().out.strip() != ""
    assert main([str(results / "missing.json")]) == 2
