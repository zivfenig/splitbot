# Decisions & AI log

One short entry per interesting moment. 30 seconds each. This becomes the
"process" and "critical reflection" sections of the submission.

Types: DECISION · AI-WRONG (Claude suggested/produced something wrong) ·
BUG-CAUGHT · BUG-MISSED · PROMPT-CHANGE · CUT (dropped from scope)

| Date | Type | What happened | What I did / why |
|------|------|---------------|------------------|
| 2026-09-25 | DECISION | Money math, approvals and duplicate checks in code, not LLM | Predictable, testable; LLM only for understanding text |
| 2026-09-25 | DECISION | LLM provider: OpenAI (openai SDK) instead of Anthropic | I already have an OpenAI key; CLAUDE.md/PLAN.md updated |
| 2026-09-25 | DECISION | No Splitwise SDK; thin httpx client, `currency_code: "ILS"` always explicit | Full control over errors (200 + `errors`), retries, idempotency; no reliance on group defaults |
| 2026-09-25 | DECISION | Splitwise layer is a swappable adapter | API needs Pro; I'm on a 7-day trial, so the backend may have to change |
| 2026-09-25 | DECISION | Demo scenario: shared apartment (groceries, bills, rent); "close the trip" → "end-of-month settle up" | Closer to how the bot would really be used |
| 2026-09-25 | DECISION | Test setup: Splitwise group "SplitBot Test" (4 members, 3 fake unregistered with Hebrew names) + Telegram group with me and the bot, privacy mode off | Safe to test without real people |
| 2026-09-25 | DECISION | Added splitwise/base.py: an ExpenseBackend contract (Protocol). The bot receives the backend as a parameter | Tests run against a fake backend (fast, offline, can simulate outages). If the Splitwise Pro trial ends, only one file changes. Claude flagged that this breaks the "fixed layout" rule and asked first |
| 2026-09-25 | BUG-CAUGHT | Claude wrote the Splitwise endpoints from memory and flagged them as unverified. I checked the official docs: JSON body is OK, but delete_expense also returns 200 on failure (must check success), and deletes are soft (deleted_at) | Added handling plus a test for delete with success: false. Lesson: verify API assumptions against the docs, not the model's memory |
| 2026-09-25 | DECISION | .env is git-ignored and Claude Code is denied reading it | Secrets are protected from the AI too, not only from git |
| 2026-09-25 | DECISION | Stage 0 verified end to end: a real ₪10 expense was created, read back and soft-deleted in "SplitBot Test"; Telegram send worked | Money rule added: users see shekels, agorot are internal, convert only at the edges (LLM output, Splitwise API, bot replies) |
| 2026-09-25 | DECISION | Critical rules are enforced, not only requested: Stop hook runs pytest; test-first with human-approved test names; independent reviewer subagent. | |
| 2026-09-25 | DECISION | Members resolved by the LLM from a list of (id, name), returned as IDs; "ambiguous" + candidates if unsure. No alias lists | Handles nicknames/transliterations (דניאל → דני) without maintenance; code still validates every ID and asks on ambiguous/invalid |
| 2026-09-25 | DECISION | Default approval = author confirms with buttons; `all` for rules (rent, bills, big amounts); `auto` opt-in only | Safe by default; the person who reported is the natural one to confirm |
| 2026-09-25 | DECISION | Bot speaks Hebrew; LLM extracts Hebrew and English; evals ~80% Hebrew / ~20% English | Matches how the group actually writes |
| 2026-09-25 | DECISION | Currencies: closed list ILS/USD/EUR, default ILS, recorded in original currency, no conversion; money in minor units, users see "38.90" | No FX guessing; edges convert, core stays integer |
| 2026-09-25 | DECISION | Closed subcategory list; main category derived in code; Splitwise category IDs fetched by script in Stage 3 | Subcategory and category can never contradict; IDs are never guessed |
| 2026-09-25 | DECISION | Grounding: every field has evidence (substring of message) + source (message/default); pure validator; failure → clarification. Limit: evidence proves the text exists, not that the interpretation is right | Catches hallucinated amounts/currencies deterministically; the limit goes in "what's NOT tested" |
| 2026-09-25 | DECISION | PLAN Stage 5: agent lives in a small dashboard (charts + chat), not `/ask` in Telegram | Telegram = recording + approvals; dashboard = insights + questions. Tech decided in Stage 5 |
| 2026-09-25 | DECISION | Added validation.py (pure member-ID + evidence validators) to the file layout. Claude asked first | Keeps models.py to contracts and money.py to money |
| 2026-09-25 | DECISION | Participants rule: "with X" = author + X only; "without X" = everyone except X; both → the explicit list wins; neither → everyone | One unambiguous rule for the Stage 2 prompt and evals; conflicts resolve to the narrower, safer list |
| 2026-09-25 | DECISION | Amount text: comma + exactly 3 digits = thousands ("1,200" = 1200); comma + 1–2 digits = decimal ("38,90" = 38.90) | Common in Hebrew/European writing; tested in money tests |
| 2026-09-25 | DECISION | Stage 1 test budget: ~10 unit tests total, parametrized; more only after explaining why | Tests should read like product rules, not test the libraries |
| 2026-09-25 | DECISION | `confidence` is an enum (high/medium/low), not a float | LLM self-reported float confidence is poorly calibrated, and policy only needs "low → strictest" |
| | | | |
