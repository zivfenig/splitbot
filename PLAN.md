# SplitBot — build plan

Deadline: Monday 29.9, 10:00. Priority tags: **MUST** / **SHOULD** / **NICE**.
Rule: a stage is done only when *I* verified it (not when tests are green).

---

## Stage 0 — Setup & smoke test (Fri) — MUST
Build:
- Repo, venv, `pyproject.toml`, folder layout from CLAUDE.md, `.env.example`, `.gitignore`.
- Keys: Anthropic API, Telegram bot (BotFather), Splitwise API key.
- Test Splitwise group + test Telegram group with 2–3 friends (or test accounts).
- `scripts/smoke_splitwise.py`: create one expense in the test group, read it back, delete it.

I verify: the expense appears in the Splitwise app, then disappears.

---

## Stage 1 — Deterministic core (Fri) — MUST
Build (pure functions, no network):
- `models.py`: ExtractedExpense, Expense, Share, ApprovalRule, GroupConfig.
- `money.py`: equal split, "everyone except X", exact shares, rounding (leftover → payer).
- `policy.py`: modes `auto` / `author` / `all`; rules by category and by amount;
  unknown or low-confidence category → strictest rule.
- `state.py`: `pending_approval → approved → submitting → submitted`,
  plus `rejected`, `expired`, `failed`. Illegal transitions raise errors.
- `store.py`: SQLite. Processed message IDs (idempotency), expenses, outbox.

Tests: `tests/unit/` only.
I verify: I read the list of test names — it should read like the rules of the product.

---

## Stage 2 — LLM extraction + eval harness (Sat morning) — MUST
Build:
- `prompts/extract_v1.md` + `llm/extractor.py`: LLM → JSON → Pydantic.
  Invalid → one retry with the error message → still invalid → `needs_clarification`.
- Output includes `message_type` (new / correction / delete / chat) and `confidence`.
- Datasets: ~40 golden cases + ~15 adversarial (two people named Dani, "oops it was 260",
  prompt injection, chat that looks like an expense, foreign currency).
- `run_evals.py`: per-field accuracy (amount, payer, participants, category, type),
  list of failures, consistency check (each case 3×), cost and latency per case.
  Saves to `results/<prompt>_<date>.json`.

I verify: I label/approve every expected answer myself, then read the failure list.

---

## Stage 3 — Splitwise client + MCP server (Sat) — MUST
Build:
- `splitwise/client.py`: timeouts; `429` → retry with backoff;
  **HTTP 200 with non-empty `errors` = failure**.
- Idempotency across retries: write our key (`sb:<chat_id>:<msg_id>`) into the expense
  `details`; after an unclear failure (timeout), search for the key before retrying.
- If Splitwise is down: keep the expense in the outbox and tell the group.
- `mcp_server/server.py`: tools `get_members`, `list_expenses`, `get_balances`,
  `add_expense` (only for approved expenses), `update_expense`. Validation inside the server.

Tests: `tests/integration/` with mocked HTTP: 200-with-errors, 429, timeout-after-success
(no duplicate), outage → message to group.
I verify: smoke run against the real test group.

---

## Stage 4 — Telegram bot (Sat evening) — MUST
Build:
- Message → workflow → approval buttons (✓ / ✗) → Splitwise.
- Commands: `/rules` (e.g. `/rules rent=all`, `/rules over_500=all`), `/pending`.
- Corrections update the existing expense; duplicates are asked about, not added.

I verify: run the 10 scenarios in `tests/e2e/scenarios.md` in the real test group.
Final balances must match the expected balances to the agora.

---

## Stage 5 — Agent mode (Sun) — SHOULD
Build:
- `/ask ...` → agent with MCP tools. Read tools are free; any write goes through approval.
- 2–3 tasks: "how much did we spend on food?", "who owes whom?",
  "close the trip" (settlement with minimum transfers — computed in code).
- Trajectory evals (~10 tasks): expected tools, forbidden tools, max steps, correct answer,
  cost per successful task.

---

## Stage 6 — Iterate & harden (Sun) — MUST (at least one iteration)
- Prompt v2 (and v3) based on eval failures. Record before/after numbers in CHANGELOG.
- Fault-injection flag in the Splitwise client for chaos runs.

## Stage 7 — Demo + write-up (Sun evening) — MUST
- 2–3 min recorded demo.
- PDF sections: summary · process & prompts · critical reflection · testing strategy ·
  what's NOT tested · bugs caught / missed · demo link.

## Monday morning — buffer, final read, submit.

---

### If behind schedule, cut in this order
1. Agent mode → one task only.
2. `/rules` per category → one global mode.
3. Consistency check (3× runs) → 1× runs.
Out of scope from the start (write in "not tested"): receipt photos, voice notes,
large groups, real payments.
