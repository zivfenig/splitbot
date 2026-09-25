#!/usr/bin/env python3
"""Stop hook: Claude cannot finish a turn with failing tests.

Runs `pytest -q` when files under src/, tests/ or pyproject.toml have changed.
- Tests pass (or no relevant changes)  -> exit 0, Claude stops normally.
- Tests fail                           -> exit 2, stderr is shown to Claude,
                                          which must fix or report.
- Already blocked once this turn       -> exit 0 (no infinite loop); CLAUDE.md
                                          tells Claude to report the failure.
Only the fast pytest suite runs here. LLM evals are never run by this hook.
Uses the standard library only, so the system python3 can run it.
"""
import json
import os
import subprocess
import sys

MAX_LINES = 60


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    # Second stop in a row after we already blocked: let Claude stop and report.
    if payload.get("stop_hook_active"):
        return 0

    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    venv_python = os.path.join(root, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        return 0  # no venv yet, nothing to run

    changed = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "tests", "pyproject.toml"],
        cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    if not changed:
        return 0

    try:
        result = subprocess.run(
            [venv_python, "-m", "pytest", "-q"],
            cwd=root, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        print("pytest timed out after 180s. Tell the user.", file=sys.stderr)
        return 2

    # 0 = passed, 5 = no tests collected
    if result.returncode in (0, 5):
        return 0

    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-MAX_LINES:])
    print(
        "Tests are failing. Make ONE fix attempt, or stop and tell the user exactly "
        "what fails and why. Never weaken, skip or delete a test to make it pass.\n\n"
        + tail,
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
