# Prompt changelog

An evaluated prompt version is never edited: every change is a new file, tied to eval results.

## extract_v1 — 2026-09-25
Not evaluated yet.
- English instructions; 5 few-shot examples, 4 of them Hebrew (nickname + exclusion,
  exclusion, exact amounts, English + USD + "with", chat).
- Evidence must be an exact substring of the message; defaults are marked `source: "default"`.
- Participants are reported as `only` / `exclude` and code decides; exclusions always apply,
  also with "only", and the sender goes in "exclude" when they pay for others without sharing.
- Amounts written in digits are copied as written. An amount written in words or shorthand is
  converted to digits with `amount_in_words: true` and the words as evidence: the only
  calculation the model may do (code can't verify it, so a human confirms).
- `refers_to` holds the words that identify the target of a correction or delete (null for
  new/chat); a rule, no example.
- Confidence: high = clear (normal defaults do not lower it); medium = an interpretation was
  needed; low = missing, unclear or unsupported.
- `exclude` is [] when empty and `only` is never []; each exact-amounts entry's evidence
  holds exactly one number; ambiguous candidates are different ids.
- Known gaps to watch in the evals: corrections and deletes have no example; two people
  with the same name; per-person amounts without a stated total; unsupported currencies.

## extract_v2 — 2026-09-26
Problem (dev, gpt-4o-mini, extract_v1): case-04 "store name + amount" read as chat (0/3);
case-08 and case-42 pizza read as subcategory "other"; case-55 light bulbs and filters read as
"other" instead of supplies (case-19: cleaning products read as cleaning by gpt-4o); case-44 a
correction put the new value in refers_to and left the amount empty (0/3); case-48 "paid for
pizza yesterday" without an amount read as chat (0/3, both models).
Change: five targeted rules, no new examples. In `message_type`: a store or item name plus a
number is a new expense; a payment with no amount is still new (amount null, low confidence);
a correction with a new and an old value puts the NEW one in `amount` and the OLD one in
`refers_to`. In the subcategory list: supermarket and store names are groceries; cleaning is
paying a person or service, supplies are things bought for the home (cleaning products are
supplies); ready-to-eat food is restaurant or delivery, never other.
Hypothesis: those cases improve and nothing else changes (watch case-08, case-19, case-47 for
regressions).
Result (dev, gpt-4o-mini, 3 runs): full-case accuracy 80.3% -> 85.5%; subcategory and category
84.0% -> 96.3%; refers_to 66.7% -> 100%; payer 92.6% -> 100%; participants 88.9% -> 96.3%;
grounding failures 4.4% -> 0.9%; missed expense rate 6.7% -> 0%; input tokens per call 2,790 ->
2,983. Fixed: case-33, case-42, case-44, case-55. Regressions: case-27 (chat about paying later
now read as a new expense, 3/3 -> 0/3; false expense rate 0% -> 11.1%) and case-19 (2/3, one
run read the currency as USD). Not fixed: case-04 and case-51 (recognised as new, but the amount
is still not extracted), case-48 (new with low confidence now, but subcategory still "other"),
case-37, case-58.

## jev_router_v1 — 2026-09-26
The `choice` question sent to Jev (via OpenRouter) by the router: the instructions and the
three criteria (`expense` = the "action" class: a new expense, a correction or a delete; `query`;
`ignore`). Not evaluated yet.

## agent_v1 — 2026-09-27
Not evaluated yet.
- English instructions; no few-shot examples (the agent's inputs are tool schemas and short
  messages, not a fixed extraction shape, so examples did not seem to add much; revisit if the
  eval shows otherwise).
- States the code-built context block it will see (date, roster, sender, reply target) and that
  the message itself is DATA, never instructions, mirroring `extract_v1`'s rule.
- Names when to call each write tool (new / correction / delete) and when NOT to: no bulk
  target, no guessing a correction/delete target without a reply or a search match, chat-only
  messages never call a write tool.
- States that write tools only ever create a pending proposal, never take money/identity
  arguments, and that a confirmation is shown separately, never the model's own summary of it
  (this backs the code's rule of using `Proposal.confirmation_text` verbatim).
- States the answer-grounding rule in the model's own words (every number from a tool result or
  the message; state amounts/dates as returned, never recomputed) so the model does not fight the
  code-side check that already enforces it.

## agent_v2 — 2026-09-27
Problem (real run, gpt-5.4-mini, agent_v1): scenario 2 ("כמה הוצאנו החודש על אוכל בחוץ?", a
broad category question) called `spending_summary(by="subcategory", ...)` instead of
`by="category"` in 5/5 real runs, answering with two partial per-subcategory numbers
("140 restaurant, 90 delivery") instead of the one combined "230" the question asked for. Both
numbers were real and grounded; the model just never aggregated them, and it must never do that
arithmetic itself. Unlike an extraction miss on a write (case-4, case-5: caught by the
confirmation step before anything is written), this is a read-only answer with no safety net,
so a wrong or unhelpfully split answer reaches the user directly.
Change: one paragraph added to the "Tools" section explaining `spending_summary`'s `by="category"`
vs `by="subcategory"`, and naming the exact failure mode ("do NOT call it with by="subcategory"
... that splits the same spending into several partial numbers... which you would then have to
add together yourself, which you must never do"). Nothing else changed.
Hypothesis: scenario 2 passes consistently; nothing else regresses (scenario 3, the only other
case that also names a category-ish filter, is unaffected since it uses `search_expenses`, not
`spending_summary`).
Result (real runs, gpt-5.4-mini, `AGENT_MODEL`, full 11-case suite, 5x consistency): 10/11
scenarios passed all 5 runs (up from 8/11 under agent_v1). Scenario 2 fixed cleanly: 5/5,
`spending_summary(by="category", ...)`, answer states "230" every run. Scenario 5 unaffected,
still 0/5 (the known extraction-miss gap, documented in `docs/not_tested.md`; not this prompt's
job to fix). The other 8 previously-passing scenarios stayed at 5/5: no regression.
Unplanned side effect: scenario 11 ALSO now passes the automated check 5/5 (was 0/5, previously
falling back on `max_steps`) -- but reading the actual replies, this is not a real fix: the
model now searches only the current month plus a search filtered to January, concludes "no
netflix expenses since the start of the year" and stops (2 tool calls, well under the step
cap), never finding the real expense (dated in the previous month). It no longer hits the
safety net (`max_steps`), it now confidently states something false. The automated check
passes only because scenario 11 was deliberately designed with no expected-number check (see
`agent_cases.jsonl`'s note on it) -- a human read of the gallery is needed before treating
scenario 11 as fixed; it is NOT, it just fails differently now. Flagged to Ziv, not silently
counted as a win.
