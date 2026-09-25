---
name: implement
description: Test-first workflow for one step of PLAN.md. Use when the user types /implement or asks to build the next step. The user approves test names BEFORE any code is written.
---

# Implement (test-first)

Work on ONE step only. Keep every message short and plain.

1. **Restate the step** in one sentence: what it does and which files it touches.
   If anything is unclear, ask (max 3 numbered questions, each with a suggested default).

2. **Propose test names only — no code yet.**
   A numbered list of test function names, grouped by file, each followed by one
   plain sentence. Names must read like product rules, e.g.
   `test_leftover_agorot_go_to_payer` — "240.01 split 3 ways: payer gets the extra agora".
   Include edge cases, not only the happy path (zero, unknown member, rounding,
   invalid input, illegal state transition, non-ILS currency...).
   Then STOP and wait for the user's approval or edits.

3. **Write the tests** exactly as approved, in the correct folder
   (`tests/unit/` = pure, no network, no LLM). Run them and show that they FAIL
   (red) — this proves they test something real.

4. **Implement** the smallest code that makes them pass. Follow every rule in
   CLAUDE.md (minor units, never float; LLM never does math; prompts only in
   `prompts/`). Run `pytest` and show the result in a few lines.

5. **Never** change an approved test's expectation to make it pass. If a test
   seems wrong, stop and tell the user why.

6. Finish with: "Step done. Run /review?" — do not start the next step on your own.

Note: a Stop hook runs `pytest` when you finish a turn. If tests are red it will
block you and show the failures. Make ONE fix attempt; if it still fails, stop and
tell the user exactly what fails and why.
