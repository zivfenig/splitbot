---
name: review
description: Independent review of the current changes against CLAUDE.md rules and test quality. Use when the user types /review, after /implement, and before /checkpoint.
---

# Review (independent reviewer)

The code author must not grade its own work. Do this:

1. Collect the change set: `git diff` plus any untracked files under `src/`, `tests/`,
   `prompts/`, `scripts/`.

2. Spawn ONE fresh subagent with the Agent tool (general-purpose). Give it:
   the full text of CLAUDE.md, the change set, and the checklist below.
   Tell it: "You are a strict reviewer. Do NOT edit any file. Report findings only."

3. Checklist for the subagent:

   **A. Project rules**
   - Money: amounts in minor units (int). Any `float` touching money = violation.
   - The LLM never does arithmetic and never decides approval; code does.
   - Every LLM output goes through Pydantic validation before use.
   - Prompts live only in `prompts/`; no prompt text inside Python code.
   - Tests are in the right folder: `unit/` has no network and no LLM calls.
   - Splitwise calls: 200 + non-empty `errors`, or `success` not true, = failure.
   - Nothing reads or prints `.env` or secrets.
   - No new dependency that was not approved.

   **B. Test quality** (tests were written by AI too — check them)
   - Every rule added in this change has at least one test. List rules with no test.
   - Each test has a meaningful assert (not just "no exception").
   - Edge cases exist, not only the happy path.
   - No test was weakened: compare changed expectations with `git diff`; flag any
     expected value that changed.
   - Test names read like rules.

4. Output format (from the subagent, relayed to the user as-is, short):
   ```
   VERDICT: clean | issues found
   [BLOCKER] file:line — problem — suggested fix
   [WARN]    file:line — problem — suggested fix
   Missing tests: <rule> — <suggested test name>
   ```

5. Do not fix anything automatically. Ask the user which findings to fix.
   If a finding shows that earlier AI-written code or tests were wrong, suggest an
   AI-WRONG row for DECISIONS.md.
