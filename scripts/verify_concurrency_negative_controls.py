"""Verify every concurrency/state-safety protection in `store.py` by REMOVING it on a scratch
copy of the repo and confirming the relevant test then fails (a "negative control"). Never
touches this working copy: every edit happens on a fresh `tempfile.mkdtemp()` copy of `src/`,
`tests/` and `pyproject.toml`, which is discarded afterwards (kept only with `--keep`).

Run:
    python scripts/verify_concurrency_negative_controls.py [--keep] [--variant NAME ...]

Exit code 0 means every variant behaved exactly as expected: the untouched baseline copy still
passes, and every protection that was removed made its target test(s) fail. Exit code 1 means
something did NOT behave as expected -- most importantly, a variant whose test was supposed to
fail but still passed, which means that test is VACUOUS (it proves nothing) and is itself a bug
to fix, per CLAUDE.md's rule for concurrency tests.

This mirrors, as a permanent and rerunnable artifact, the ad-hoc scratch-copy checks described in
DECISIONS.md's "Concurrency negative controls were verified..." row; run it again whenever
`store.py`'s locking/CAS code changes, to keep that row true rather than a one-time claim.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = "src/splitbot/store.py"

CONCURRENCY = ["tests/concurrency"]
STALE_STATE_UNIT = ["tests/unit/test_store.py::test_state_changes_are_compare_and_swap"]
STALE_VERSION_UNIT = ["tests/unit/test_store.py::test_correction_or_delete_applies_only_after_all_approvals_exactly_once"]
REVISE_STALE_UNIT = ["tests/unit/test_store.py::test_revise_pending_expense_is_compare_and_swap_on_version"]
COMMIT_ROLLBACK_UNIT = ["tests/unit/test_store.py::test_a_failing_commit_rolls_back_and_leaves_the_store_usable"]
TARGET_CHECK_UNIT = ["tests/unit/test_store.py::test_a_change_is_refused_when_its_target_was_changed_meanwhile"]
EXPIRY_UNIT = ["tests/unit/test_store.py::test_an_answer_after_expiry_is_refused_even_before_the_sweeper_runs"]


@dataclass(frozen=True)
class Edit:
    old: str
    new: str


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    expect_fail: list[str]  # test node ids/paths that must FAIL once these edits are applied
    edits: list[Edit] = field(default_factory=list)  # empty edits = the untouched baseline
    also_expect_pass: list[str] = field(default_factory=list)  # must still PASS (a "some protections overlap" check)


VARIANTS = [
    Variant(
        "baseline_untouched", "sanity check: the scratch-copy mechanism itself, unmodified",
        expect_fail=[], also_expect_pass=[*CONCURRENCY, *STALE_STATE_UNIT, *STALE_VERSION_UNIT,
                                          *COMMIT_ROLLBACK_UNIT, *TARGET_CHECK_UNIT, *EXPIRY_UNIT],
    ),
    Variant(
        "no_message_records_table", "the shared (chat_id, message_id) idempotency table is gone",
        edits=[Edit(
            "    PRIMARY KEY (chat_id, message_id)\n);\nCREATE TABLE IF NOT EXISTS expenses",
            "    kind_dummy INTEGER\n);\nCREATE TABLE IF NOT EXISTS expenses",
        )],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "no_unique_constraints", "no UNIQUE constraint on (chat_id, message_id) anywhere",
        edits=[
            Edit(",\n    UNIQUE (chat_id, message_id)\n);\nCREATE TABLE IF NOT EXISTS change_requests",
                 "\n);\nCREATE TABLE IF NOT EXISTS change_requests"),
            Edit(",\n    UNIQUE (chat_id, message_id)\n);\n\"\"\"", "\n);\n\"\"\""),
        ],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "no_lock_no_version_cas", "deferred transactions (no BEGIN IMMEDIATE) AND no version compare-and-swap",
        edits=[
            Edit('"BEGIN IMMEDIATE"', '"BEGIN"'),
            Edit(
                '"WHERE id = ? AND state = ? AND version = ?",\n'
                '            (request.state.value, request.model_dump_json(exclude={"id", "version"}), request.id, expected_state,\n'
                '             expected_version),',
                '"WHERE id = ? AND state = ?",\n'
                '            (request.state.value, request.model_dump_json(exclude={"id", "version"}), request.id, expected_state),',
            ),
        ],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "no_lock_no_state_cas", "deferred transactions AND no state/version compare-and-swap on expenses",
        edits=[
            Edit('"BEGIN IMMEDIATE"', '"BEGIN"'),
            Edit(
                '"UPDATE expenses SET state = ?, version = version + 1 WHERE id = ? AND state = ? AND version = ?",\n'
                '                    (new_state.value, expense_id, expense.state.value, expense.version),',
                '"UPDATE expenses SET state = ? WHERE id = ?", (new_state.value, expense_id),',
            ),
        ],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "only_lock_removed", "just the deferred-BEGIN lock removed; compare-and-swap alone still catches it",
        edits=[Edit('"BEGIN IMMEDIATE"', '"BEGIN"')],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "only_version_cas_removed",
        "just the version compare-and-swap removed: the lock alone still serializes writes, so the "
        "concurrency test cannot observe a lost vote -- but the unit test (a stale IN-MEMORY read, no "
        "real thread race) proves the CAS was the thing actually preventing an overwrite",
        edits=[Edit(
            '"WHERE id = ? AND state = ? AND version = ?",\n'
            '            (request.state.value, request.model_dump_json(exclude={"id", "version"}), request.id, expected_state,\n'
            '             expected_version),',
            '"WHERE id = ? AND state = ?",\n'
            '            (request.state.value, request.model_dump_json(exclude={"id", "version"}), request.id, expected_state),',
        )],
        expect_fail=STALE_VERSION_UNIT,
        also_expect_pass=CONCURRENCY,
    ),
    Variant(
        "no_wal_no_busy_timeout", "no WAL journal mode and no busy_timeout: writers fail instead of queuing",
        edits=[
            Edit("busy_timeout_ms: int = 5000", "busy_timeout_ms: int = 0"),
            Edit('self._db.execute("PRAGMA journal_mode = WAL")', "pass"),
        ],
        expect_fail=CONCURRENCY,
    ),
    Variant(
        "no_commit_rollback", "a failing COMMIT is not rolled back",
        edits=[Edit(
            '            try:\n'
            '                self._db.execute("ROLLBACK")\n'
            '            except sqlite3.Error:\n'
            '                pass  # the failed COMMIT already ended the transaction\n'
            '            raise',
            "            raise",
        )],
        expect_fail=COMMIT_ROLLBACK_UNIT,
    ),
    Variant(
        "no_target_check_at_apply", "a change request can be applied even if its target expense changed meanwhile",
        edits=[Edit(
            'WHERE id = ? AND state = ? AND deleted = 0",\n            (request.expense_id, _CONFIRMED)',
            'WHERE id = ? AND (state = ? OR 1 = 1)",\n            (request.expense_id, _CONFIRMED)',
        )],
        expect_fail=TARGET_CHECK_UNIT,
    ),
    Variant(
        "no_expiry_at_approval", "an approval after expiry is accepted instead of refused",
        edits=[
            Edit(
                "and now is not None and is_expired(\n                expense.created_at, now\n            )",
                "and False",
            ),
            Edit("if now is not None and is_expired(request.created_at, now):", "if False:"),
        ],
        expect_fail=EXPIRY_UNIT,
    ),
    Variant(
        "no_revise_pending_version_check",
        "revise_pending_expense's version compare-and-swap is removed: it always uses the version it "
        "just read as its own \"expected\" value, so a write based on a caller's stale read is never "
        "actually checked against reality (a real bug found and fixed once already in this method)",
        edits=[Edit(
            '                if replacement.version != current.version:\n'
            '                    raise StateConflict(f"expense {expense_id} changed while we were revising it")\n',
            "",
        )],
        expect_fail=REVISE_STALE_UNIT,
    ),
]


def apply_edits(path: Path, edits: list[Edit], variant_name: str) -> None:
    text = path.read_text(encoding="utf-8")
    for edit in edits:
        if edit.old not in text:
            raise SystemExit(
                f"[{variant_name}] anchor text not found in {path.name} (store.py was refactored -- "
                f"update this script's Edit for this variant):\n{edit.old[:120]!r}"
            )
        text = text.replace(edit.old, edit.new, 1)
    path.write_text(text, encoding="utf-8")


def run_tests(root: Path, node_ids: list[str]) -> tuple[bool, str]:
    """(all passed, last summary line). Empty `node_ids` -> (True, "(nothing to run)")."""
    if not node_ids:
        return True, "(nothing to run)"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *node_ids, "-q", "-p", "no:cacheprovider", "--no-header", "--tb=line"],
        cwd=root, env={"PYTHONPATH": str(root / "src")}, capture_output=True, text=True, timeout=120,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return result.returncode == 0, (lines[-1] if lines else "(no output)")


def run_variant(variant: Variant, *, keep: bool) -> bool:
    """True iff this variant behaved exactly as expected. Prints one line per checked group."""
    root = Path(tempfile.mkdtemp(prefix=f"splitbot_negctl_{variant.name}_"))
    for part in ("src", "tests", "prompts", "config", "pyproject.toml"):
        src = REPO_ROOT / part
        dst = root / part
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy(src, dst)

    ok = True
    try:
        apply_edits(root / STORE, variant.edits, variant.name)

        if variant.expect_fail:
            passed, summary = run_tests(root, variant.expect_fail)
            verdict = "FAILS (control works)" if not passed else "STILL PASSES -- VACUOUS CONTROL"
            print(f"  {variant.name:32} expect_fail  {verdict:32} {summary}")
            ok &= not passed

        if variant.also_expect_pass:
            passed, summary = run_tests(root, variant.also_expect_pass)
            verdict = "passes as expected" if passed else "UNEXPECTEDLY FAILS"
            print(f"  {'':32} expect_pass  {verdict:32} {summary}")
            ok &= passed
    finally:
        if keep:
            print(f"  (kept at {root})")
        else:
            shutil.rmtree(root, ignore_errors=True)
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the scratch copies for inspection")
    parser.add_argument("--variant", action="append", help="run only these variant name(s); default: all")
    args = parser.parse_args(argv)

    variants = VARIANTS
    if args.variant:
        known = {v.name for v in VARIANTS}
        unknown = set(args.variant) - known
        if unknown:
            print(f"unknown variant(s): {', '.join(sorted(unknown))}\nknown: {', '.join(sorted(known))}", file=sys.stderr)
            return 2
        variants = [v for v in VARIANTS if v.name in args.variant]

    all_ok = True
    for variant in variants:
        print(f"{variant.name}: {variant.description}")
        all_ok &= run_variant(variant, keep=args.keep)

    print()
    print("ALL VARIANTS BEHAVED AS EXPECTED" if all_ok else "SOME VARIANTS DID NOT BEHAVE AS EXPECTED -- see above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
