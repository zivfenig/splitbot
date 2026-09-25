---
name: add-eval-case
description: Add one case to the LLM eval dataset with a human-verified expected answer. Use when the user types /add-eval-case, pastes a message the bot got wrong, or wants a new golden/adversarial case.
---

# Add eval case

The expected answer must be verified by the human. You may draft; you may never
mark a case verified yourself.

1. Get the input: the raw message text, and who sent it (member id).
   If the user pasted a real bot failure, also get what the bot extracted.

2. Check for duplicates in `tests/llm_evals/datasets/` (same or near-identical text).
   If one exists, show it and ask whether to still add.

3. Draft the case as ONE JSON line, following the current `ExtractedExpense` model in
   `src/splitbot/models.py` (field names and types must match it exactly):
   ```json
   {"id": "he-042", "text": "...", "sender": "m_ziv",
    "lang": "he|en|mixed",
    "tags": ["names", "currency", "correction", "adversarial", ...],
    "source": "synthetic|real-failure",
    "expected": { ...fields with value, evidence, source... },
    "notes": "why this case matters",
    "verified": false}
   ```
   - `evidence` must be an exact substring of `text`.
   - Defaults (ILS, payer = sender, all members) use `"source": "default"`.
   - Ambiguous cases: the expected result is "needs_clarification", not a guess.

4. Show the draft as a short readable summary (not only JSON):
   "Amount: ₪240 (evidence: '240') · Payer: Ziv (default) · Excluded: Dani ('בלי דני') ·
   Subcategory: restaurant". Ask: "Correct? Edit anything?"

5. Only after the user says it is correct: set `"verified": true` and append the line
   to the right file (`extraction_golden.jsonl` or `extraction_adversarial.jsonl`).

6. Report the dataset balance in one line, e.g.
   "golden: 31 (he 25 / en 6) · adversarial: 9". Target is ~80% Hebrew.
