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
