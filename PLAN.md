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

---

## Stage A: Align the repo with the architecture in CLAUDE.md (now, ~1h)
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

## Stage B: Experiment 2: extraction quality vs model (~1h, runs in background)
- Dataset: `extraction_dev.jsonl` (39) / `extraction_test.jsonl` (20), mine, verified.
- Run dev on 3 OpenAI models (cheap / mid / strong, chosen from what the account offers).
- Report per model: full-case accuracy, per-field accuracy, grounding failures,
  consistency (3 runs), cost and latency (p50/p95). Failures list.
- Pick the model; improve the prompt only from dev failures; final numbers on test.

## Stage C: Router + Experiment 1: embeddings vs Jev (~1.5h)
- `Router` protocol; `EmbeddingRouter` (kNN / similarity to `router_reference.jsonl`),
  `JevRouter` (`choice` question over expense / query / ignore, returns probabilities).
- Dataset: `router_dev.jsonl` / `router_test.jsonl` / `router_reference.jsonl`, mine, verified.
  Rows are tagged `easy` / `hard`.
- Choose thresholds on dev; report on test:
  - **missed expense rate** (expense routed to ignore): must be ~0
  - **ignore filtered rate** (ignore rows correctly skipped): the saving
  - query detection, per-class confusion matrix, easy vs hard, cost, latency
- Pick the router for the bot.

## Stage D: Ledger tools + concurrency (~1.5h)
- Read tools: `get_balances`, `search_expenses`, `spending_summary` (category / month / payer).
- Write tools: `propose_expense`, `propose_correction`, `propose_delete`: always run
  extractor → validators → confirmation; idempotent per (chat_id, message_id).
- Concurrency tests (threads), each with a negative control:
  1. the same message processed twice in parallel → one expense
  2. several users confirm at the same moment → one state transition
  3. 50 messages in parallel → all stored, no lost writes, no DB lock errors

## Stage E: Agent (~1.5h)
- `prompts/agent_v1.md`; OpenAI tool calling; guardrails from CLAUDE.md (max steps, cost cap,
  answer grounding, write tools only via workflow).
- Agent eval: 5–10 scenarios (mine): a seeded ledger + a question → expected tools, forbidden
  tools, correct answer. Must include: balances, category summary, search by month,
  "record 50 for milk" (must go through confirmation), and a bypass attempt
  ("delete all of Dani's expenses" → no delete without approval).

## Stage F: Telegram bot + real group (~2h)
- Routing: @ / reply → agent; otherwise router. Confirmation buttons (✓ / ✗),
  corrections/deletes with all-relevant approval, reply targeting, `/rules`, `/pending`.
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
