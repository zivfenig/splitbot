# SplitBot — project instructions for Claude Code

## What we are building
A Telegram bot that turns group-chat messages ("paid 240 for sushi, without Dani")
into Splitwise expenses — safely. Later stage: an "agent mode" that answers questions
and runs multi-step tasks ("end-of-month settle up") using tools from our own MCP server.
Telegram = recording + approvals (workflow). A small dashboard (charts + chat panel) =
insights + questions (agent). Dashboard tech is decided in Stage 5; keep it minimal.
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

## Product decisions
- **Language:** the bot speaks Hebrew. The LLM must understand Hebrew AND English
  messages. Evals: ~80% Hebrew, ~20% English.
- **Members:** the LLM gets the group members (id + name) and returns member IDs from
  that list (it understands nicknames/transliterations: דניאל → דני, Dani → דני). No
  alias lists in code. If unsure it returns "ambiguous" with the candidate IDs. Code
  checks every returned ID is a real member; ambiguous or invalid → the bot asks.
- **Approval:** default mode = `author` (the person who reported confirms, with buttons:
  "דיווחת על הוצאה של ₪240 על פיצה, מחולקת בין כולם. נכון?"). Mode `all` is for rules
  (rent, bills, amounts over a threshold). `auto` is opt-in only. Unsure → strictest.
- **Participants** (used in the Stage 2 prompt and evals):
  "עם X" / "with X" = an explicit list: only the author + X.
  "בלי X" / "without X" = everyone in the group except X.
  Both together ("עם מיכל ובלי דני") → author + Michal (the exclusion applies too).
  Exclusions ALWAYS apply, also to the author: "שילמתי 100 על הפיצה של דני ומיכל" →
  only=[Dani, Michal], exclude=[author] → Dani + Michal, 50 each; the author paid and owes 0.
  Neither → everyone (`source: "default"`).
  The LLM never lists the whole group or computes set differences: it only reports what
  the message says, as two optional lists: `only` ("עם X", may be missing) and `exclude`
  ("בלי X"). Each named person is a member reference: a known ID, or "ambiguous" with
  candidate IDs. The payer uses the same reference. CODE builds the final list of member
  IDs: `only` present → (author + `only`) − `exclude`; else everyone − `exclude`;
  neither → everyone.
- **Exact amounts** ("150: דני 50, משה 60"): the LLM reports per-person amounts as written
  (member reference + amount text + evidence). CODE checks they sum exactly to the total;
  if not → the bot asks, never auto-fix. Author not mentioned and stated < total → the
  remainder is the author's share (shown in the author confirmation). Negative remainder,
  or author listed and sum ≠ total → the bot asks. No amounts → equal split (default).
- **Approval details:** a non-ILS expense always needs `all` (amount thresholds are ILS
  only). A rule with both a category and a min_amount means AND. The threshold is
  inclusive (>=). Several matching rules → the strictest wins (`all` > `author` >
  `auto`). A matching rule overrides the group default (it may loosen or tighten it), but
  unknown category, low confidence or non-ILS always give `all`.
- **Crash recovery:** an expense stuck in `submitting` is never blindly re-sent: first
  search Splitwise for our idempotency key; if found, mark it `submitted`.
- **Amount text:** a comma followed by exactly 3 digits is a thousands separator
  ("1,200" = 1200). A comma followed by 1–2 digits is a decimal separator
  ("38,90" = 38.90). "1.200" (dot + 3 digits) and "1.200,50" are ambiguous → the bot asks.
  Amounts above 100,000 (any currency) → the bot asks.
- **Currencies:** closed list ILS, USD, EUR. ILS if none is mentioned. Recorded in the
  original currency in Splitwise, NO conversion.
- **Categories:** closed subcategory list; the main category is derived from the
  subcategory IN CODE, so they can never contradict. Rules use main categories.
  electricity, gas, water, internet → utilities · rent → rent · arnona → arnona ·
  groceries → groceries · cleaning, supplies → household · restaurant, delivery →
  eating_out · other → other. The free-text description stays as written. Splitwise
  `category_id` mapping comes in Stage 3 from a script that fetches the real IDs
  (never guess them).
- **Grounding:** every field quoted from the message (amount, currency, payer,
  participants) has `evidence` (exact substring of the message) and `source` ("message"
  or "default"). Subcategory, description and message_type are inferred, so they carry
  no evidence. A pure validator checks: evidence appears in
  the message (after light normalization); the number in the amount's evidence equals
  the amount; a non-ILS currency has evidence; defaults are marked `source: "default"`.
  The quoted number must be a whole number in the message, never a slice of a longer
  one ("40" inside "240"). A default payer must be the author. A default ILS is refused
  when the message names a foreign currency (keyword heuristic: $ € £, USD, דולר, אירו…).
  Any failed check → needs clarification, never silently accepted.
  Limit (document it): evidence proves the text exists, not that the interpretation
  is right.

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

## Workflow
- Each step: `/implement` → `/review` → fix. End of stage: `/checkpoint`.
- Prompt changes ONLY via `/new-prompt-version`. Eval cases ONLY via `/add-eval-case`.
- A Stop hook runs `pytest` and blocks finishing with red tests. Make ONE fix attempt,
  then report exactly what fails and why. Never weaken, skip or delete a test to pass.

## Stack
Python 3.12 · pydantic v2 · openai SDK · python-telegram-bot · mcp (FastMCP)
· httpx · sqlite3 · python-dotenv · pytest (+ respx for HTTP mocking).
Secrets in `.env` only.
- LLM provider is OpenAI (not Anthropic). Extraction uses temperature 0, read from config
  (`OPENAI_TEMPERATURE`, default 0); the model name and temperature are recorded in every
  eval result file.
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
  validation.py      # pure: member-ID check + evidence (grounding) validator
  money.py           # amounts in minor units (int), splitting, rounding — pure functions
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
- Keep tests minimal: about 10 unit tests for all of Stage 1, one or two per layer, using
  `pytest.mark.parametrize` to fold similar cases. Don't test what libraries already
  guarantee. If a layer needs more tests, say why BEFORE writing them.
- Eval datasets: you may DRAFT cases, but I verify every expected answer by hand.
  Mark verified cases with `"verified": true`. Never change an expected answer to
  make a test pass — tell me instead.
- Expected answers are verified ONLY by the human, never by a model (otherwise the eval
  measures how much two models agree, not how right the extractor is).
- Eval messages: about a third written by the human (`source: "user"`), the rest drafted by
  Claude (`source: "synthetic"`). Claude drafting and OpenAI extracting are different model
  families, on purpose. Drafts must be varied: typos, slang, emojis, word order, mixed
  Hebrew/English, like real apartment chats. Real bot failures from Stage 4 are added as
  `source: "real-failure"`.
- No leakage: few-shot examples inside a prompt must never appear in the eval sets.
  `run_evals.py` fails if any eval text appears in the prompt file.

## Money rules
- Amounts are integers in minor units (agorot/cents). Never float.
- Shares must sum exactly to the total. Leftover minor units from rounding go to the payer
  if the payer is a participant; otherwise to the first participant in group order (a
  payer who is not a participant owes 0).
- Users always see and write shekels (e.g. "38.90"). Agorot are internal only; convert
  at the edges (LLM output, Splitwise API, bot replies).

## Commands
- Unit + integration tests: `pytest`
- LLM evals: `python -m tests.llm_evals.run_evals --prompt extract_v1`
- Run the bot: `python -m splitbot.bot.telegram_bot`
