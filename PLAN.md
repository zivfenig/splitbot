# SplitBot — build plan

Deadline: Monday 29.9, 10:00. Priority tags: **MUST** / **SHOULD** / **NICE**.
Rule: a stage is done only when *I* verified it (not when tests are green).

---

## Stage 0 — Setup & smoke test (Fri) — MUST
Build:
- Repo, venv, `pyproject.toml`, folder layout from CLAUDE.md, `.env.example`, `.gitignore`.
- Keys: OpenAI API, Telegram bot (BotFather), Splitwise API key.
- Test Splitwise group + test Telegram group with 2–3 friends (or test accounts).
- `scripts/smoke_splitwise.py`: create one expense in the test group, read it back, delete it.

I verify: the expense appears in the Splitwise app, then disappears.

---

## Stage 1 — Deterministic core (Fri) — MUST
Build (pure functions, no network):
- `models.py`: ExtractedExpense (fields with evidence + source, member IDs or
  "ambiguous"), Expense, Share, ApprovalRule, GroupConfig; currencies (ILS/USD/EUR);
  closed subcategory list with subcategory → main category mapping in code.
- Member-ID validation: every returned ID is a real member; ambiguous/invalid → ask.
- `money.py`: minor units, currencies, amount parse/format ("38.90"), equal split,
  "everyone except X", exact shares, rounding (leftover → payer).
- Evidence validator: evidence is in the message, amount number matches, non-ILS currency
  has evidence, defaults marked `source: "default"`. Failure → needs clarification.
- `policy.py`: modes `author` (default) / `all` / `auto` (opt-in only); rules by main
  category and by amount; unknown or low-confidence category → strictest rule.
- `state.py`: `pending_approval → approved → submitting → submitted`,
  plus `rejected`, `expired`, `failed`. Illegal transitions raise errors.
- `store.py`: SQLite. Processed message IDs (idempotency), expenses, outbox.

Tests: `tests/unit/` only, about 10 for the whole stage (validation 2–3, money 2,
policy 2, state + duplicates 2), parametrized.
I verify: I read the list of test names — it should read like the rules of the product.

---

## Stage 2 — LLM extraction + eval harness (Sat morning) — MUST
Build:
- `prompts/extract_v1.md` + `llm/extractor.py`: LLM → JSON → Pydantic.
  Invalid → one retry with the error message → still invalid → `needs_clarification`.
- Output includes `message_type` (new / correction / delete / chat) and `confidence`.
- The LLM gets group members (id + name) and returns member IDs, or "ambiguous" + candidates.
- Datasets: ~40 golden cases + ~15 adversarial (two people named Dani, "oops it was 260",
  prompt injection, chat that looks like an expense, foreign currency).
  ~80% Hebrew, ~20% English. About a third of the messages are written by me
  (`source: "user"`), the rest drafted by Claude and varied (typos, slang, emojis, mixed
  languages). Expected answers: verified by me only.
- `run_evals.py` fails if any eval text appears in the prompt file (no few-shot leakage).
- **Must-have eval cases** (added via `/add-eval-case` once the models exist; I verify each):
  - "פיצה עם מיכל ובלי דני 140" → ₪140 (currency: default); participants: author + Michal
    only (the explicit list wins over the exclusion).
  - "150: דני 50, משה 60" → total ₪150; exact amounts Dani 50, Moshe 60; the author's
    remainder (40) is computed by code and shown in the confirmation.
- `run_evals.py`: per-field accuracy (amount, payer, participants, category, type),
  list of failures, consistency check (each case 3×), cost and latency per case.
  Saves to `results/<prompt>_<date>.json`, with the model name and temperature recorded.
  (Stage 6 may compare JSON mode with strict Structured Outputs.)

I verify: I label/approve every expected answer myself, then read the failure list.

---

## Stage 3 — Splitwise client + MCP server (Sat) — MUST
Build:
- Script that fetches the real Splitwise category IDs → our subcategory mapping (don't guess IDs).
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
- Message → workflow → approval buttons (✓ / ✗) → Splitwise. Bot speaks Hebrew.
  Default: the author confirms ("דיווחת על הוצאה של ₪240 על פיצה, מחולקת בין כולם. נכון?").
- Commands: `/rules` (e.g. `/rules rent=all`, `/rules over_500=all`), `/pending`.
- Corrections update the existing expense; duplicates are asked about, not added.
- Workflow completeness check: a `new` expense without an amount (or other required field)
  → ask, never guess. With a test.
- Missing participants → treat as everyone, or ask. With a test.
- The same member twice in exact amounts → ask. With a test.

I verify: run the 10 scenarios in `tests/e2e/scenarios.md` in the real test group.
Final balances must match the expected balances to the agora.

---

## Stage 5 — Agent mode (Sun) — SHOULD
Build:
- Small dashboard (tech decided in Stage 5, keep it minimal): 2–3 charts (by category,
  by month, balances) + a chat panel that talks to the agent, which uses our MCP tools.
  Telegram = recording + approvals; dashboard = insights + questions (no `/ask` in Telegram).
- Agent with MCP tools. Read tools are free; any write goes through approval.
- 2–3 tasks: "how much did we spend on food?", "who owes whom?",
  "end-of-month settle up" (settlement with minimum transfers — computed in code).
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
