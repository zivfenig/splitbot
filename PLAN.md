# SplitBot: build plan

Deadline: Monday 29.9, 10:00 (single PDF: deck exported to PDF + demo link).
Saturday: build and run everything in Telegram. Sunday: finish experiments, demo video, deck.
A stage is done only when *I* verified it (not when tests are green).

---

## Done
- **Core (pure, tested):** models, money, validation (participants, members, grounding),
  policy, state, store (idempotency, compare-and-swap), config.
- **LLM layer:** OpenAI client (usage/cost/latency, JSON mode, temperature from config),
  extractor (max one retry, safe retry errors), prompt `extract_v1`.
- **Extraction eval harness:** `run_evals.py` + my verified extraction dataset.
- **Stage A (done):** repo aligned with the architecture; smoke calls for Jev and embeddings.
- **Stage B (done):** extraction model gpt-4o-mini; prompt `extract_v2` is final and frozen;
  official test-set numbers recorded in `results/` and DECISIONS.md.
- **Stage C (done):** Jev is the bot's router (0% missed expense on test, AUC 0.984); the
  embedding router stays in the repo as the documented baseline.
- **Stage D (done, 2026-09-27):** read/write ledger tools; every write goes through
  extractor → validators → confirmation → ledger, with a shared (chat_id, message_id)
  idempotency key; the store runs in WAL mode with compare-and-swap/`BEGIN IMMEDIATE` on every
  state transition; all 4 concurrency tests pass with negative controls, and the negative-control
  sweep itself is a rerunnable script (`scripts/verify_concurrency_negative_controls.py`), not
  just a one-time claim.

---

## Stage A (done): Align the repo with the architecture in CLAUDE.md
- Remove code/tests/scripts/env entries that are not part of this architecture.
- `state.py`: expense states `pending_confirmation → confirmed | rejected | expired`;
  change requests (correction/delete) with the same states and all-relevant approval.
- `store.py`: the ledger is the source of truth (no outbox): save/update/soft-delete,
  balances per currency, search, summary by category/month. All math in code.
- Keep every existing test that still describes a product rule; delete the rest.
- `.env.example`: `OPENAI_*`, `OPENROUTER_API_KEY`, `JEV_MODEL=typesafe/jev-1.13`,
  `OPENAI_EMBEDDING_MODEL`, `TELEGRAM_*`.
- Smoke: one Jev call via OpenRouter (verify the request/response shape from the docs,
  don't guess) and one embeddings call.

I verify: `pytest` green, test names read like rules, smoke outputs.

## Stage B (done): Experiment 2: extraction quality vs model
- Dataset: `extraction_dev.jsonl` (39) / `extraction_test.jsonl` (20), mine, verified.
- Does a ~16× more expensive model buy better extraction? Dev, 3 runs each: gpt-4o-mini
  (baseline) vs gpt-4o, per field, with full-case accuracy, grounding failures, consistency,
  cost per call and per correct extraction, p50/p95 latency, and the cases where they disagree.
- Prompt v2 from dev failures only; final numbers on test once: v2 on both models, plus v1 on
  gpt-4o-mini. Models that don't support temperature 0 are excluded.
- Result: gpt-4o-mini; v2 frozen (no further prompt rounds).

## Stage C (done): Router + Experiment 1: embeddings vs Jev
- `Router` protocol; `EmbeddingRouter` (kNN / similarity to `router_reference.jsonl`),
  `JevRouter` (`choice` question over expense / query / ignore, returns probabilities).
- Dataset: `router_dev.jsonl` / `router_test.jsonl` / `router_reference.jsonl`, mine, verified.
  Rows are tagged `easy` / `hard`.
- The only thresholded decision is binary, ignore vs pass; the threshold is the highest
  ignore-filtered rate with zero expenses routed to ignore on dev (not Youden), then fixed
  for test. Report on test:
  - **missed action rate** (an `expense`-labelled row, i.e. new/correction/delete, routed to
    ignore): must be ~0
  - **ignore filtered rate** (ignore rows correctly skipped): the saving
  - ROC-AUC (ignore vs pass), query detection, per-class confusion matrix, easy vs hard,
    cost, latency (p50/p95); per-message scores saved
- Result: Jev is the bot's router; the embedding router stays as the documented baseline.

## Stage D (done): Ledger tools + concurrency (~1.5h)
- Read tools: `get_balances` (per currency, a small set of settlement transfers computed in code),
  `search_expenses`, `spending_summary` (category / month / payer).
- Write tools: `propose_expense`, `propose_correction`, `propose_delete`: always run
  extractor → validators → confirmation built from the code template (always shown, whatever
  the confidence); idempotent per (chat_id, message_id). A new expense needs only the
  sender's approval; corrections and deletes need all relevant people. Every pending action
  expires after `PENDING_EXPIRY_HOURS` (default 1) and never blocks other messages.
- Store: WAL mode + 5 s busy timeout; every state transition compare-and-swap or
  `BEGIN IMMEDIATE`; idempotency is a DB-level UNIQUE constraint (one test hits the constraint
  directly, bypassing application logic).
- Concurrency tests (threads, one connection each), each with a negative control that must
  fail without the protection (a control that does not fail is a bug in the test):
  1. the same message processed twice in parallel → one expense
  2. several users confirm at the same moment → one state transition (and, for a correction,
     every vote recorded and the change applied once)
  3. 50 messages in parallel → all stored, no lost writes, no DB lock errors
  4. two unrelated pending actions from different users resolve independently, without
     interfering with each other

## Stage E: Agent (~1.5h)
- `prompts/agent_v1.md`; OpenAI tool calling; guardrails from CLAUDE.md (max steps, cost cap,
  answer grounding, write tools only via workflow).
- Free-text approval loop: if the user answers a pending confirmation with corrected
  information instead of pressing a button ("זה היה 100 ולא 120"), the agent updates the
  pending record (compare-and-swap while it is still pending), re-runs the confirmation with
  the new numbers and waits again, until the user approves, rejects, or it expires.
- Agent eval: 5–10 scenarios (mine): a seeded ledger + a question → expected tools, forbidden
  tools, correct answer. Must include: balances, category summary, search by month,
  "record 50 for milk" (must go through confirmation), and a bypass attempt
  ("delete all of Dani's expenses" → no delete without approval).

## Stage F: Telegram bot + real group (~2h)
- Routing: @ / reply → agent; otherwise router. Confirmation buttons (✓ / ✗) or a free-text
  reply, corrections/deletes with all-relevant approval, reply targeting, `/rules`, `/pending`
  (lists open pending actions). A pending action expires after 1 hour: nothing is written and
  the bot posts a one-line notice in the group. `auto` mode (opt-in) commits after the grace
  window unless the sender corrects or rejects.
- Workflow checks, each with a test: new expense without amount → ask; missing participants →
  everyone; same member twice in exact amounts → ask; router said `expense` but the extractor
  says `chat` → ask "רצית לרשום הוצאה?", never drop the message silently.
- `tests/e2e/scenarios.md`: 10 scenarios in the real group; balances must match to the agora.

## Stage G (Sunday): results, demo, deck
- Final test-set numbers for both experiments; 2–3 min demo video.
- Deck (exported to PDF): problem · architecture · prompts & process · critical reflection
  (AI-WRONG / BUG-CAUGHT) · testing strategy per station · experiments · what's not tested ·
  demo link.

---

### If behind schedule, cut in this order
1. Corrections/deletes with all-relevant approval → author approval only.
2. Agent eval → 5 scenarios.
3. Router experiment → Jev only (keep the embeddings baseline if time allows).
Out of scope (list in `docs/not_tested.md`): receipt photos, voice notes, settlements between
members, follow-up messages without reply, load testing over HTTP.
