---
name: add-eval-case
description: Add or convert eval rows (extraction, router or agent) from the human's verified data, validate them, and append them to the right dataset file. Use when the user types /add-eval-case, pastes a message the bot got wrong, or sends a verified table.
---

# Add eval case

The human authors ALL eval data: the messages AND the expected answers. You never draft or
suggest either. You convert what the human gives you, validate it, and report problems
without fixing them. `"verified": true` comes only from the human's verified file or an
explicit statement.

1. **Kind.** Which dataset is it: `extraction`, `router` or `agent`? Ask if unclear.

2. **Input.** The human's row(s): a table, a file, or a pasted real bot failure (then also the
   sender and what the bot did; `source` is `real-failure`).

3. **Duplicates.** Compare (case/whitespace-insensitive) with every file of that kind in
   `tests/llm_evals/datasets/`. Router: a message may not appear in more than one of
   `router_dev`, `router_test`, `router_reference`. Extraction: not in both dev and test.
   If a duplicate exists, show it and ask; never overwrite.

4. **Format**, one JSON object per line:
   - **extraction** (`extraction_dev.jsonl` / `extraction_test.jsonl`; roster in `rosters.json`):
     `{"id", "split": "dev|test", "roster": "A|B", "sender_id", "message", "source":
     "user|real-failure", "verified", "expected": <an ExtractedExpense payload incl.
     "amount_in_words">, "scoring": {"subcategory_any_of": [..] | null, "expect_low": bool,
     "score_confidence": bool, "score_description": bool}, "notes"}`.
     Evidence must be an exact substring of `message`; defaults use `"source": "default"`.
   - **router** (`router_dev.jsonl` / `router_test.jsonl` / `router_reference.jsonl`):
     `{"id", "split": "dev|test|reference", "message", "label": "expense|query|ignore",
     "difficulty": "easy|hard" (null for reference), "source", "verified"}`.
   - **agent** (`agent_*.jsonl`): `{"id", "roster", "sender_id", "ledger_seed": [expenses],
     "question", "expected_tools": [..], "forbidden_tools": [..], "must_confirm": bool,
     "expected_facts": [numbers/strings that must appear in the answer], "max_steps",
     "source", "verified"}`. Tool names must be real tools from `src/splitbot/tools/`.
     If the human's own agent file uses a different shape, follow theirs and note it.

5. **Validate (read-only, never edit the data).**
   - extraction: `ExtractedExpense.model_validate(expected)`, then `check_members` and
     `check_grounding`. Rows whose expected answer legitimately needs a clarification (an
     ambiguous member, an amount code rejects) are reported as "ask rows", not as errors.
   - router: label in {expense, query, ignore}; difficulty in {easy, hard, null for reference};
     ids unique; `split` matches the file; no text overlap across the three files.
   - agent: tool names exist; the seeded ledger validates as `Expense` records.
   - Every kind: every prompt file (`prompts/*.md`) must not contain a row's message (leakage).
   Any problem: stop, list it (row id, what, why), change nothing.

6. **Append** only rows the human marked verified, to the right file. Then report the balance
   in one line, e.g. "extraction dev: 39 (he 30 / en 6 / mixed 3) · router dev: 65 (expense 30 /
   query 12 / ignore 23; easy 35 / hard 30)". Extraction target is ~80% Hebrew.
