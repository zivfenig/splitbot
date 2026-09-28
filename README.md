# SplitBot

SplitBot is an expense-tracking assistant that lives inside a Telegram group chat for roommates
or friends (a group of one works too). It notices expense messages ("פיצה 140 בלי דני"), records
them after explicit confirmation, handles corrections and deletes, tracks debt repayments, and
answers questions about the group's money ("מי חייב למי?", "כמה הוצאנו החודש על אוכל בחוץ?"). All
data lives in its own SQLite ledger — never in Splitwise or any external service. The bot speaks
Hebrew; its language model also understands English.

## Architecture, in brief

```
message in group
 ├─ @bot or reply to the bot ───────────────────────────────► AGENT
 └─ otherwise ─► ROUTER (cheap, no LLM text generation)
                  ├─ ignore  → nothing happens, no LLM call
                  └─ pass    → AGENT immediately
AGENT (LLM with tools)
 ├─ read tools:  balances, member statement, search/list expenses, summaries (all math in code)
 └─ write tools: propose_expense / propose_correction / propose_delete / propose_settlement
                  → extractor → validators → confirmation (buttons) → ledger
```

- **Router**: a cheap first pass (currently the Jev decision model via OpenRouter) that only
  decides ignore vs. not-ignore. It never drops a real expense — anything it's unsure about, or
  any message that @-mentions the bot or replies to it, goes straight to the Agent.
- **Agent**: an OpenAI tool-calling loop. It chooses which tool to call; the tools themselves
  enforce every rule (money math, validation, approval) — the model never does arithmetic and
  never decides what gets approved.
- **Deterministic write pipeline**: every write tool (new expense, correction, delete,
  settlement) runs the same fixed path — extractor → validators → a code-built confirmation →
  the SQLite ledger. The confirmation the user sees is always built from a template, never
  written by the LLM, and every write requires the user's explicit approval (a button press or a
  free-text reply) before anything is stored.

Full design rationale, guardrails, and product decisions live in `CLAUDE.md`.

## Setup

Requires Python 3.12+.

```bash
git clone <this repo>
cd splitbot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Then fill in `.env`. Required keys to run the real bot (everything else in `.env.example` has a
working default):

| Key | What it's for |
|---|---|
| `OPENAI_API_KEY` | Extraction and the agent's own model calls |
| `OPENAI_MODEL` | The extractor's model (kept separate from the agent's model) |
| `AGENT_MODEL` | The agent's own tool-calling model |
| `OPENROUTER_API_KEY` | The Jev router (via OpenRouter) |
| `TELEGRAM_BOT_TOKEN` | The Telegram bot itself |

`TELEGRAM_BOT_TOKEN` is your own — there's no shared bot to connect to. Create one via
[@BotFather](https://t.me/BotFather) on Telegram (`/newbot`, pick a name and a username), copy
the token it gives you into `.env`, then add that new bot to your own Telegram group. Each
`TELEGRAM_BOT_TOKEN` is a fully separate, independent bot instance with its own ledger.

**Important**: with BotFather, turn Group Privacy **off** for your bot (`/mybots` → your bot →
"Bot Settings" → "Group Privacy" → "Turn off"). By default a bot only sees messages that
@-mention it or reply to it — this bot also needs to see every plain message in the group to
route it (ignore vs. an expense/question), so privacy mode must be off or it will silently miss
everything that doesn't @-mention it.

Never commit `.env` (it's gitignored).

## Running the bot

```bash
python -m splitbot.bot.telegram_bot
```

Polling, no public URL or webhook needed. The SQLite ledger (`DB_PATH` in `.env`, default
`splitbot.db`) is created automatically on first run — nothing to set up by hand. The group's
member roster is also built automatically: the first message from anyone in the chat registers
them, there's no config file to fill in.

## Running tests

```bash
pytest
```

This runs the unit, integration, concurrency, and local end-to-end suites — all offline, no real
API calls. `tests/llm_evals/` (extraction, router, and agent evaluation harnesses) is **not**
part of the default run: it costs real API money and is invoked explicitly, e.g.:

```bash
python -m tests.llm_evals.run_evals --split dev --model <model>
python -m tests.llm_evals.run_agent_eval
```
