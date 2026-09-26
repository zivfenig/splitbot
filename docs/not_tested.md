# What is NOT tested (and known limits)

Add to this file in every stage. It feeds the "what's NOT tested" section of the write-up.

## Out of scope
Receipt photos, voice notes, settlements between members, follow-up messages without a reply,
load testing over HTTP.

## Known limits (by design)
- **Evidence proves the text exists, not that the interpretation is right.** The grounding
  check can confirm "240" is in the message; it cannot confirm 240 is the right amount.
- **The LLM's conversion of an amount written in words is not verified by code** (no digit
  to compare). A human always confirms the number (policy: at least author confirmation).
- **The foreign-currency check is a keyword heuristic.** It catches `$ € £`, USD/EUR/GBP and
  the common Hebrew words; it can still miss unusual wording, and a default ILS can hide it.
- **gpt-4o-mini does not always flag an ambiguous name** (dev case-37: "סופר 300 בלי דני" with two
  Danis, 0/3 runs flagged). The author's confirmation shows the resolved name, so a human
  catches it; the check is not automatic.
- **Subcategory, description and message_type are inferred**, so they carry no evidence.
- **Participants have ONE evidence string for the whole field, not one per member**, so a
  person wrongly added to `only` or `exclude` can still pass grounding (the evidence exists,
  it just does not cover that person).

## Handled by the workflow (see PLAN.md, Stage F), not by the validators
- A `new` expense with a missing amount or other required field is not flagged by the
  validators; the workflow must ask.
- Missing `participants` is not handled by `resolve_participants`; the workflow must treat
  it as "everyone" or ask.
- The same member twice in exact amounts is not detected; the workflow must ask.
- A default payer is only checked against the author when the workflow passes `author_id`.

## Not covered by tests
- Amount-text edge cases: leading zeros ("007"), surrounding whitespace, Unicode digits
  (these are rejected or accepted safely, but not asserted).
- `format_amount` with negative numbers; `split_expense` with `total <= 0`.
- Invisible characters in Hebrew text (RLM/LRM, ZWSP) and gershayim variants (״ vs ") are not
  normalized; the effect is a needless clarification question, not a wrong expense.
- Store: behavior with several processes writing at once is not tested (the concurrency tests
  use threads on one database file).
- An unknown expense id in the store raises `KeyError`; not asserted.
