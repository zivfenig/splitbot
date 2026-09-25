# Prompt changelog

Old versions are never edited. Every change is a new file, tied to eval results.

## extract_v1 — 2026-09-25
First version. No evals yet (baseline run comes next).
- English instructions; 4 of 5 few-shot examples are Hebrew (nickname + exclusion,
  exclusion, exact amounts, English + USD + "with", chat).
- Rules: evidence must be an exact substring; defaults are marked `source: "default"`;
  participants are reported as `only` / `exclude` (code decides); amounts are copied as
  written (the LLM never does arithmetic); ambiguous names → `ambiguous` + candidates.
- Amended before its first eval run (versions are immutable from the first run on): the
  participants rule now says exclusions always apply, also with "only", and the sender goes
  in "exclude" when they pay for others without sharing.
- Also amended before the first eval run (after the independent review and a read-through):
  confidence definitions made consistent (normal defaults do not lower confidence; medium =
  an interpretation was needed; low = missing/unclear/unsupported) and example 1 replaced by
  a real "medium" example (דניאל → דני); `exclude` is [] when empty and `only` is never [];
  each exact-amounts entry's evidence holds exactly one number; ambiguous candidates must be
  different ids.
- Known gaps to watch in the evals: corrections and deletes have no example; two people
  with the same name; per-person amounts without a stated total; unsupported currencies.
