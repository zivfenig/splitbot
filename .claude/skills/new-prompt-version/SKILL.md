---
name: new-prompt-version
description: Create the next version of an LLM prompt, tie it to an eval run, and compare against the previous version. Use when the user types /new-prompt-version or wants to change a prompt in prompts/.
---

# New prompt version

Old prompt versions are never edited. Every change is a new version tied to eval results.

1. **Find the latest version**, e.g. `prompts/extract_v2.md`, and its latest eval
   result in `tests/llm_evals/results/`.

2. **Name the problem.** Ask the user (or read the last result file) which cases failed
   and why. The change must target specific failing case ids — no "general improvements".

3. **Create the new file** `prompts/extract_v3.md` as a copy of v2, then make the
   smallest change that addresses the problem. Show the user the diff between v2 and v3.

4. **Add a CHANGELOG entry** in `prompts/CHANGELOG.md`:
   ```
   ## extract_v3 — 2026-09-27
   Problem: he-017, he-023 — "בלי X" read as "only X"
   Change: added an explicit rule + 2 examples for exclusion phrases
   Hypothesis: exclusion accuracy up, nothing else changes
   ```

5. **Run the evals** for the new version — this costs money, so first tell the user
   the number of cases and runs, and wait for "ok".
   `python -m tests.llm_evals.run_evals --prompt extract_v3`

6. **Compare with the previous version**, short table:
   - Overall and per-field accuracy: v2 → v3
   - Per language: he / en
   - Evidence failures (grounding / hallucination rate)
   - Cost and p50/p95 latency per case
   - **Fixed:** case ids · **Broken (regressions):** case ids · **Still failing:** case ids
   Put regressions first and make them loud.

7. **Do not switch** the default prompt version in config until the user approves.

8. Propose a PROMPT-CHANGE row for DECISIONS.md with the before/after numbers, and
   append the actual results to the CHANGELOG entry ("Result: exclusion 71% → 94%;
   regression: en-004").
