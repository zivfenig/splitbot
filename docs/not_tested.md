# What is NOT tested (and known limits)

Add to this file in every stage. It feeds the "what's NOT tested" section of the write-up.

## Out of scope
Receipt photos, voice notes, follow-up messages without a reply,
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
- **Known issue: prompt v2 with gpt-4o-mini does not make the pipeline ask about an ambiguous
  amount** ("1.200", case-36: asked in 3/3 runs with v1, 0/3 with v2). No further prompt round;
  the unconditional confirmation shows the exact number before anything is stored, so a human
  catches it.
- **Participant language outside the protected common phrases can still be misinterpreted.**
  Code now enforces exact `only`, defaults to the full roster, understands unique clean aliases
  such as `ירדן` for `❤️ ירדן`, and protects common `של/רק/עם/כל הבית` forms. More complex or
  implicit phrasing still relies on the extractor, so the confirmation remains the final guard.
- **Settlement recognition still depends on the Agent.** Once `propose_settlement` is selected,
  code verifies sender, recipient, currency, amount and the matching open debt; confirmation is
  unconditional. Unusual repayment wording may still be routed to the wrong tool and rejected.
- **Subcategory and description are inferred**, so they carry no evidence. Action selection is
  owned by the Agent and is no longer part of extraction.
- **Participants have ONE evidence string for the whole field, not one per member**, so a
  person wrongly added to `only` or `exclude` can still pass grounding (the evidence exists,
  it just does not cover that person).
- **The agent can only answer a "since X" / multi-month range question by calling
  `spending_summary` one calendar month at a time** (it has no single "date range" tool), so a
  range longer than about `MAX_STEPS` (5) months back cannot be answered at all: it correctly
  falls back rather than guessing (agent eval scenario 11, gpt-5.4-mini, real run 2026-09-27:
  hit `max_steps` after querying January through May trying to reach "since the start of the
  year"). Accepted for now; a future prompt or tool change could teach it to call
  `spending_summary` with no month filter for an all-time total in one call instead.

## Handled by the workflow (see PLAN.md, Stage F), not by the validators
- A `new` expense with a missing amount or other required field is not flagged by the
  validators; the workflow must ask.
- Missing `participants` is not handled by `resolve_participants`; the workflow must treat
  it as "everyone" or ask.
- The same member twice in exact amounts is not detected; the workflow must ask.
- A default payer is only checked against the author when the workflow passes `author_id`.

- **The agent's per-turn cost cap counts only the agent model's own calls**, not the extractor
  call a write tool makes underneath it. Accepted: the extractor call is a small, fixed-size
  request next to the $0.02 default cap, and it is already capped separately by its own retry
  limit.

## Not covered by tests
- Amount-text edge cases: leading zeros ("007"), surrounding whitespace, Unicode digits
  (these are rejected or accepted safely, but not asserted).
- `format_amount` with negative numbers; `split_expense` with `total <= 0`.
- Invisible characters in Hebrew text (RLM/LRM, ZWSP) and gershayim variants (״ vs ") are not
  normalized; the effect is a needless clarification question, not a wrong expense.
- Store: behavior with several processes writing at once is not tested (the concurrency tests
  use threads on one database file, on one machine).
- Settlement transfers use a greedy rule: for large groups the number of transfers is not
  guaranteed to be the true minimum (the money is always exact).
- The `auto` grace window, the expiry notice in the group and `/pending` are built in later
  stages and are not covered yet. The free-text revision loop is covered deterministically,
  including a missing Telegram Reply, stale pending records and next-turn selection after search;
  the current `agent_v4` prompt still needs a fresh real-model consistency run.
- An unknown expense id in the store raises `KeyError`; not asserted.
