# What is NOT tested (and known limits)

Add to this file in every stage. It feeds the "what's NOT tested" section of the write-up.

## Out of scope from the start
Receipt photos, voice notes, large groups, real payments.

## Stage 1 — deterministic core

**Known limits (by design)**
- **Evidence proves the text exists, not that the interpretation is right.** The grounding
  check can confirm "240" is in the message; it cannot confirm 240 is the right amount.
- **Foreign-currency check is a keyword heuristic.** It catches `$ € £`, USD/EUR/GBP and the
  common Hebrew words; it can still miss unusual wording, and a default ILS can hide it.
- **Subcategory, description and message_type are inferred**, so they carry no evidence.

**Handled by the Stage 4 workflow (see PLAN.md), not by Stage 1 code**
- A `new` expense with a missing amount or other required field is not flagged by the
  validators; the workflow must ask.
- Missing `participants` is not handled by `resolve_participants`; the workflow must treat
  it as "everyone" or ask.
- The same member twice in exact amounts is not detected; the workflow must ask.
- A default payer is only checked against the author when the workflow passes `author_id`.

**Not covered by tests**
- Amount-text edge cases: leading zeros ("007"), surrounding whitespace, Unicode digits
  (these are rejected or accepted safely, but not asserted).
- `format_amount` with negative numbers; `split_expense` with `total <= 0`.
- Invisible characters in Hebrew text (RLM/LRM, ZWSP) and gershayim variants (״ vs ") are not
  normalized; the effect is a needless clarification question, not a wrong expense.
- Store: single-thread use only; behaviour with two processes writing at once is covered
  only by the compare-and-swap test (a simulated stale read), not by a real second process.
- Unknown expense id in the store raises `KeyError`; not asserted.
