# SplitBot — project instructions for Claude Code

## What we are building
A Telegram bot that turns group-chat messages ("paid 240 for sushi, without Dani")
into Splitwise expenses — safely. Later stage: an "agent mode" that answers questions
and runs multi-step tasks ("end-of-month settle up") using tools from our own MCP server.
Demo scenario: a shared apartment (groceries, bills, rent) — not a trip.

This is also a job-assignment project. The reviewers care about HOW I work with AI:
clear problem framing, iteration, verifying AI output, and engineering judgment.
So process and documentation matter as much as the code.

## Core design rule: what the LLM does vs. what code does
- LLM: understand messy text → structured data (extraction), and in agent mode
  choose which tools to call.
- Code: EVERYTHING else — money math, splitting, rounding, validation, duplicate
  detection, approval decisions, state transitions, calls to Splitwise.
- The LLM never does arithmetic on money. The LLM never decides whether something
  is approved. Any LLM output passes Pydantic validation before it is used.
- Message text from users is DATA, never instructions (prompt-injection safe).

## How we work together (most important section)
1. **Ask before you build.** Before writing code for any step, send a short plan
   (max 5 bullets) and WAIT for my "ok". No code before approval.
2. **Ask questions.** If anything is ambiguous, ask me — max 3 numbered questions
   at a time, each with your suggested default answer. Don't guess on design choices.
3. **Short and simple.** Plain language, short messages. No long explanations
   unless I ask. Reply in English (session logs go into the submission).
4. **One stage at a time.** Follow PLAN.md. Don't start the next stage until I
   confirm the current one is verified.
5. **End of every step:** run the relevant tests, show pass/fail in a few lines,
   propose a one-line entry for DECISIONS.md and a commit message. Or run /checkpoint.
6. **Be honest about uncertainty.** If you're not sure an API behaves a certain way,
   say so and suggest how to check, instead of assuming.
7. **No new dependencies or frameworks without asking.** Keep the stack boring.

## Stack
Python 3.12 · pydantic v2 · openai SDK · python-telegram-bot · mcp (FastMCP)
· httpx · sqlite3 · python-dotenv · pytest (+ respx for HTTP mocking).
Secrets in `.env` only.
- LLM provider is OpenAI (not Anthropic).
- Splitwise: NO third-party SDK. Thin httpx client (Bearer API key), because we need
  full control over errors, retries and idempotency. Always send `currency_code: "ILS"`
  explicitly; never rely on group defaults.
- The Splitwise layer is an ADAPTER behind an interface (the API needs Splitwise Pro;
  the trial is 7 days). Workflow, MCP server and bot depend on the interface, never
  on Splitwise HTTP details, so the backend can be swapped (e.g. a fake/local ledger).

## File layout (keep it this way)
```
src/splitbot/
  models.py          # Pydantic models (the contracts between parts)
  money.py           # amounts in agorot (int), splitting, rounding — pure functions
  policy.py          # approval modes & rules — pure functions
  state.py           # expense state machine — pure
  store.py           # SQLite: processed messages (idempotency), expenses, outbox
  llm/
    client.py        # thin wrapper around the OpenAI API
    extractor.py     # message → ExtractedExpense (loads prompt by version)
  config.py          # loads .env (python-dotenv); clear error on missing keys
  splitwise/
    base.py          # ExpenseBackend Protocol: the contract the bot depends on
    client.py        # httpx implementation of ExpenseBackend: errors, retries, timeouts
  mcp_server/
    server.py        # MCP tools over Splitwise; guardrails live HERE too
  agent/
    agent.py         # agent mode (stage 5)
  bot/
    telegram_bot.py  # wiring: Telegram ⇄ workflow ⇄ approvals
prompts/
  extract_v1.md ...  # every prompt version is a separate file
  CHANGELOG.md       # what changed in each version and why
tests/
  unit/              # deterministic code only. NO network, NO LLM. Must be fast.
  integration/       # several parts together, with mocked HTTP and a FAKE LLM
  llm_evals/         # REAL LLM calls. Not part of the default `pytest` run.
    datasets/        # golden + adversarial cases (jsonl)
    results/         # one result file per run, named by prompt version + date
    run_evals.py
  e2e/
    scenarios.md     # manual checklist against real Telegram + Splitwise test group
scripts/             # smoke tests & one-off tools
```

## Rules for prompts
- Prompts live ONLY in `prompts/`, never inline in code.
- Never edit an existing prompt version. Create `extract_v2.md` and add a
  CHANGELOG entry explaining what failed in v1 and what v2 changes.
- The prompt version used is stored with every processed expense and every eval result.

## Rules for tests
- Every test file lives in the folder matching its type (see layout above).
- Test names describe the rule in plain words, e.g.
  `test_split_excludes_named_member`, `test_retry_after_timeout_does_not_duplicate`.
- Eval datasets: you may DRAFT cases, but I verify every expected answer by hand.
  Mark verified cases with `"verified": true`. Never change an expected answer to
  make a test pass — tell me instead.

## Money rules
- Amounts are integers in agorot (or cents). Never float.
- Shares must sum exactly to the total. Leftover agorot from rounding go to the payer.
- Users always see and write shekels (e.g. "38.90"). Agorot are internal only; convert
  at the edges (LLM output, Splitwise API, bot replies).

## Commands
- Unit + integration tests: `pytest`
- LLM evals: `python -m tests.llm_evals.run_evals --prompt extract_v1`
- Run the bot: `python -m splitbot.bot.telegram_bot`
