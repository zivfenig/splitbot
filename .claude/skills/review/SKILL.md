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
   Tell it: "You are a strict reviewer. Do NOT edit any file in the repo. Report findings only."

3. Checklist for the subagent:

   **A. Project rules**
   - Money: amounts in minor units (int). Any `float` touching money = violation.
   - The LLM never does arithmetic and never decides approval; code does.
   - Every LLM output goes through Pydantic validation before use.
   - Prompts live only in `prompts/`; no prompt text inside Python code.
   - Tests are in the right folder: `unit/` has no network and no LLM calls.
   - Nothing reads or prints `.env` or secrets.
   - No new dependency that was not approved.

   **B. Test quality** (tests were written by AI too, so check them)
   - Every rule added in this change has at least one test. List rules with no test.
   - Each test has a meaningful assert (not just "no exception").
   - Edge cases exist, not only the happy path.
   - No test was weakened: compare changed expectations with `git diff`; flag any
     expected value that changed.
   - Test names read like rules; the suite stays small (one test = one rule that would
     cause real damage if broken).

   **C. Try to break it** (adversarial)
   - For each new validation/guardrail, try to construct 2–3 inputs that PASS the check
     but are WRONG (e.g. evidence "40" quoted from "240"). Actually run them.
   - Report every input that gets through as a [BLOCKER], with the exact input.

   **D. Tool guardrails and concurrency** (whenever `tools/`, `agent/`, `store.py` or `bot/`
   changed)
   - *Write tools*: there must be NO path that writes to the ledger except extractor →
     validators → confirmation → ledger. Try to bypass it: call the write function with
     crafted input, with a message that "asks" for a delete of everyone's expenses, with an
     already-confirmed state, with another chat's ids. Any write without confirmation is a
     [BLOCKER].
   - *Idempotency*: the same (chat_id, message_id) presented twice, sequentially and in
     parallel, must create one record.
   - *Read tools*: math only in code; per currency; only confirmed, not-deleted expenses;
     another chat's data never visible.
   - *Agent loop*: max steps and cost cap enforced; every number in an answer must appear in
     that turn's tool results (answer grounding): try answers with an invented number, a
     reformatted number ("1,200" vs 1200, "₪1200") and a number from a previous turn; a
     confirmation text must come from the code template.
   - *Concurrency review*: look for read-then-write outside one transaction, a missing UNIQUE
     key or compare-and-swap, shared mutable state, SQLite locking (transactions, busy timeout).
   - *Negative control for every concurrency test*: the test must FAIL when its protection is
     removed. Actually check it: copy the repo to a scratch folder under /tmp, remove the
     protection there (never in the repo), run the test, and report. A test that still passes
     without its protection is vacuous: [BLOCKER].

4. Output format (from the subagent, relayed to the user as-is, short):
   ```
   VERDICT: clean | issues found
   [BLOCKER] file:line — problem — suggested fix
   [WARN]    file:line — problem — suggested fix
   Missing tests: <rule> — <suggested test name>
   Break attempts: <input> → caught | GOT THROUGH
   Tool bypass attempts: <input> → blocked | GOT THROUGH        (section D only)
   Concurrency negative controls: <test> → fails without protection | STILL PASSES (vacuous)
   ```

5. Do not fix anything automatically. Ask the user which findings to fix.
   If a finding shows that earlier AI-written code or tests were wrong, suggest an
   AI-WRONG row for DECISIONS.md.
