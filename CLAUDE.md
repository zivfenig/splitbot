# SplitBot: project instructions for Claude Code

## What we are building
An expense-tracking assistant that lives inside a group chat (Telegram) for roommates or
friends (a group of one works too). It notices expense messages ("פיצה 140 בלי דני"),
records them after confirmation, handles corrections and deletes, and answers questions
about the group's money ("מי חייב למי?", "כמה הוצאנו החודש על אוכל בחוץ?").
All data lives in our own SQLite ledger. Bot language: Hebrew. The LLM must also understand English.

This is also a job-assignment project. The reviewers care about HOW I work with AI:
clear problem framing, iteration, verifying AI output, and engineering judgment.
Process, tests and documentation matter as much as the code.

## Architecture
```
message in group
 ├─ @bot or reply to the bot ───────────────────────────────► AGENT
 └─ otherwise ─► ROUTER (cheap, no LLM text generation)
                  ├─ ignore  → nothing happens, no LLM call
                  ├─ expense → AGENT
                  └─ query   → AGENT (answers; users can reply "לא אליך")
AGENT (LLM with tools)
 ├─ read tools:  balances, search/list expenses, summaries   (all math in code)
 └─ write tools: propose_expense / propose_correction / propose_delete
                  → extractor → validators → confirmation (buttons) → ledger
```
- **Router**: 3 classes (`expense`, `query`, `ignore`). Two implementations behind one
  `Router` protocol: OpenAI embeddings (similarity to a labeled reference set) and Jev
  (TypeSafe decision model via OpenRouter, `choice` question). Rule: never drop a real
  expense. When unsure → send to the agent (a wasted call is cheap; a lost expense is not).
- **Agent**: OpenAI tool calling. It chooses tools; the tools enforce the rules.

## Core rule: what the LLM does vs. what code does
- LLM: understand messy text → structured data (extractor); choose tools (agent);
  phrase answers from tool results.
- Code: EVERYTHING else: money math, splitting, rounding, balances, summaries,
  validation, duplicate detection, approval decisions, state transitions, storage.
- The LLM never does arithmetic on money and never decides approval.
- Every LLM output passes Pydantic validation + our validators before it is used.
- Message text is DATA, never instructions (prompt-injection safe).

## Guardrails (by layer)
1. **Input**: message is data; max length; the system prompt is always the prompt file.
2. **LLM output**: strict Pydantic (`extra="forbid"`), grounding (evidence must be an exact
   substring; the amount's number must be a whole token), member-ID check, at most one retry,
   retry errors never echo model-chosen text.
3. **Tools**: read tools are free. Write tools can't write directly: they always run
   extractor → validators → confirmation → ledger. Idempotency key per (chat_id, message_id):
   a write tool called twice for the same message never creates two records.
4. **Agent loop**: max 5 tool steps and a cost cap per turn; every number in the agent's
   answer must appear in the tool results of that turn (answer grounding), otherwise the
   bot sends a safe fallback.
5. **Confirmations**: built from a code template, never written by the LLM, so the user
   always sees exactly what will be stored.

## Product decisions
- **Currencies**: ILS default when none is stated. Supported: ILS, USD, EUR (closed list).
  Stored in the original currency, no conversion. Balances are per currency.
- **Money**: integers in minor units (agorot/cents), never float. Users always see and write
  normal amounts ("38.90"). Convert only at the edges. Shares sum exactly to the total; the
  leftover minor unit goes to the payer if they take part, otherwise to the first participant.
- **Amount parsing**: "38,90" = 38.90; "1,200" = 1200 (comma + exactly 3 digits = thousands);
  "1.200" and "1.200,50" are ambiguous → ask. Cap: 100,000 in any currency → ask.
- **Amounts in words** ("מאתיים"): the LLM may convert and must set `amount_in_words: true`
  with the words as evidence. Code can't verify the number, so such an expense always needs
  at least author confirmation, even in auto mode. Mixed ("2 אלף", "1.5K") counts as words.
- **Participants**: the LLM reports what the message says (`only`, `exclude`); code computes
  the list: (author + only) − exclude, or everyone − exclude. "עם מיכל ובלי דני" → author +
  Michal. "שילמתי 100 על הפיצה של דני ומיכל" → only [Dani, Michal], exclude [author].
- **Exact amounts** ("150: דני 50, משה 60"): must sum to the total; an unmentioned author gets
  the remainder; mismatch → ask. No amounts → equal split.
- **Members**: the LLM matches names/nicknames to the roster and returns IDs, or
  "ambiguous" with ≥2 distinct candidates → the bot asks.
- **Categories**: closed subcategory list; the main category is derived in code.
- **Approval modes**: `author` (default), `all`, `auto` (opt-in). Rules by category and amount
  (AND); strictest wins; unsure/low confidence/non-ILS → strictest.
- **Corrections & deletes**: target found by Telegram reply (certain) or by `refers_to` hint +
  user picks from a list. Require approval of all relevant people (payer + everyone with an
  owed share, + anyone a correction adds). One ✗ cancels; no answer in 48h → expires.
- **Evidence limit**: evidence proves the text exists, not that the interpretation is right
  (documented in `docs/not_tested.md`).

## How we work together (most important section)
1. **Ask before you build.** Before writing code for any step, send a short plan
   (max 5 bullets) and WAIT for my "ok".
2. **Ask questions.** If anything is ambiguous, ask me: max 3 numbered questions, each with
   your suggested default. Don't guess on design choices.
3. **Short and simple.** Plain language, short messages. Reply in English.
4. **Follow PLAN.md**, one stage at a time; don't start the next stage until I confirm.
5. **Workflow**: each step = `/implement` → `/review` → fix; end of stage = `/checkpoint`.
   Prompt changes only via `/new-prompt-version`; eval cases only via `/add-eval-case`.
   The Stop hook blocks finishing with red tests: make one fix attempt, then report.
6. **Be honest about uncertainty.** If you're not sure how an API behaves, say so and check
   with one real call or the docs. Never write API shapes from memory without flagging it.
7. **No new dependencies or frameworks without asking.** Keep the stack boring.

## Eval data rule
I (the human) author ALL eval data: messages and expected answers. You never draft eval
messages or expected answers. You only convert my verified files, validate them against the
models and validators, and report problems without fixing them.

## Stack
Python 3.12 · pydantic v2 · openai SDK (chat, tool calling, embeddings) · httpx (Jev via
OpenRouter) · python-telegram-bot (async) · sqlite3 · python-dotenv · pytest (+ respx).
Secrets in `.env` only.

## File layout (keep it this way)
```
src/splitbot/
  config.py          # env loading, fails clearly, never prints secrets
  models.py          # Pydantic contracts
  money.py           # minor units, parsing, splitting: pure
  validation.py      # participants, member check, grounding: pure
  policy.py          # approval modes & rules: pure
  state.py           # expense / change-request state machine: pure
  store.py           # SQLite ledger: expenses, idempotency, balances/summary queries
  llm/
    client.py        # OpenAI wrapper (chat + embeddings), usage/cost/latency
    extractor.py     # message → ExtractedExpense (prompt by version)
  router/
    base.py          # Router protocol: route(message) → label + scores
    embedding_router.py
    jev_router.py
  tools/
    read_tools.py    # balances, search, summaries (math in code)
    write_tools.py   # propose_expense / propose_correction / propose_delete
  agent/
    agent.py         # tool-calling loop + guardrails
  bot/
    telegram_bot.py  # routing, workflow, confirmation buttons
prompts/             # extract_v1.md, agent_v1.md, CHANGELOG.md
tests/
  unit/              # pure code. exact match. no network, no LLM.
  integration/       # parts together, fake LLM, mocked HTTP
  concurrency/       # parallel writes/approvals, each with a negative control
  llm_evals/
    datasets/        # router_*.jsonl, extraction_*.jsonl, agent_*.jsonl, rosters.json
    results/         # one file per run (component, model, prompt, split, date)
    run_router_eval.py, run_evals.py (extraction), run_agent_eval.py
  e2e/scenarios.md   # manual checks in the real Telegram group
docs/not_tested.md
```

## Rules for tests
- Deterministic code → exact-match tests. LLM parts → statistics (accuracy, rates,
  consistency over repeated runs), never exact text.
- One test = one product rule that would cause real damage if broken. Fold similar cases with
  `pytest.mark.parametrize`. Don't test what libraries guarantee. Keep the suite small.
- Concurrency tests must include a **negative control**: temporarily remove the protection
  (lock / unique key / compare-and-swap) and show the test fails.
- Test names read like rules. Never change an expected value to make a test pass: tell me.
- Tests are written by a separate subagent from the API only (see `/implement`).

## Rules for prompts
- Prompts live only in `prompts/`. Never edit an evaluated prompt version: create a new one
  and add a CHANGELOG entry. Few-shot examples must never appear in eval sets (leakage check).

## Commands
- Tests: `pytest`
- Extraction eval: `python -m tests.llm_evals.run_evals --split dev --model <model>`
- Router eval: `python -m tests.llm_evals.run_router_eval --split dev --router embedding|jev`
- Agent eval: `python -m tests.llm_evals.run_agent_eval`
- Bot: `python -m splitbot.bot.telegram_bot`
