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
| | | | |
