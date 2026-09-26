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
