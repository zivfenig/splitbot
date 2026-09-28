"""Local end-to-end tests through real python-telegram-bot Update objects.

Only the network boundaries are fake. The Telegram adapter, BotLogic, Agent, WriteTools,
validation, confirmation callback, and file-backed SQLite Store are the production code.
"""

import asyncio
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from telegram import Message, Update

from splitbot.bot.telegram_bot import BotLogic, _on_callback, _on_message
from splitbot.llm.client import ChatResult, ToolCall
from splitbot.models import ExpenseState
from splitbot.router.base import RouteResult
from splitbot.store import Store
from tests.fakes import FakeLLM


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
CHAT_ID = -100123
USER_ID = 898328077


def _chat_result(*, text=None, tool=None):
    calls = () if tool is None else (ToolCall(id="call-1", name=tool, arguments={}),)
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [{
            "id": "call-1", "type": "function",
            "function": {"name": tool, "arguments": "{}"},
        }]
    return ChatResult(
        content=text, tool_calls=calls, message=message, model="fake-agent", temperature=0,
        input_tokens=1, output_tokens=1, latency_s=0, cost_usd=Decimal("0"),
    )


class ScriptedChat:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        return self.results.pop(0)


class FixedRouter:
    name = "fake"

    def __init__(self, ignore_score):
        self.ignore_score = ignore_score
        self.calls = []

    def route(self, message):
        self.calls.append(message)
        return RouteResult(
            label="ignore" if self.ignore_score >= 0.5 else "expense",
            scores={"expense": 1 - self.ignore_score, "query": 0, "ignore": self.ignore_score},
            latency_s=0, cost_usd=None,
        )


class FakeTelegramBot:
    username = "SplitBot"

    def __init__(self):
        self.sent = []
        self.edited = []
        self.answers = []
        self._next_message_id = 900

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self._next_message_id += 1
        return Message.de_json({
            "message_id": self._next_message_id,
            "date": int(NOW.timestamp()),
            "chat": {"id": kwargs["chat_id"], "type": "group", "title": "דירה"},
            "text": kwargs["text"],
        }, self)

    async def answer_callback_query(self, callback_query_id, **kwargs):
        self.answers.append({"id": callback_query_id, **kwargs})

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True


def _expense_extraction():
    return json.dumps({
        "confidence": "high",
        "amount": {"value": "50", "evidence": "50", "source": "message"},
        "amount_in_words": False,
        "currency": {"value": "ILS", "evidence": "שקל", "source": "message"},
        "payer": {"value": {"kind": "known", "id": USER_ID}, "evidence": None, "source": "default"},
        "participants": {"value": {"only": None, "exclude": []}, "evidence": None, "source": "default"},
        "exact_amounts": None,
        "refers_to": None,
        "subcategory": "restaurant",
        "description": "פיצה",
    }, ensure_ascii=False)


def _message_update(bot, *, message_id, text):
    return Update.de_json({
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "date": int(NOW.timestamp()),
            "chat": {"id": CHAT_ID, "type": "group", "title": "דירה"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "זיו", "last_name": "פניגשטיין"},
            "text": text,
        },
    }, bot)


def _callback_update(bot, *, data, confirmation_message_id=901):
    return Update.de_json({
        "update_id": 999,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "זיו"},
            "chat_instance": "local-test",
            "data": data,
            "message": {
                "message_id": confirmation_message_id,
                "date": int(NOW.timestamp()),
                "chat": {"id": CHAT_ID, "type": "group", "title": "דירה"},
                "text": "confirmation",
            },
        },
    }, bot)


def _context(bot, logic, store):
    return SimpleNamespace(bot=bot, bot_data={"bot_logic": logic, "store": store})


def _logic(store, chat, extractor, router):
    return BotLogic(
        store, chat, extractor, router, threshold=0.5,
        agent_prompt_version="agent_v4", extractor_prompt_version="extract_v2", clock=lambda: NOW,
    )


def test_ignored_telegram_update_stops_before_agent_and_sends_nothing(tmp_path):
    store = Store(tmp_path / "ledger.db")
    chat = ScriptedChat()
    router = FixedRouter(0.9)
    bot = FakeTelegramBot()

    asyncio.run(_on_message(
        _message_update(bot, message_id=1, text="מה נשמע?"),
        _context(bot, _logic(store, chat, FakeLLM([]), router), store),
    ))

    assert router.calls == ["מה נשמע?"]
    assert chat.calls == []
    assert bot.sent == []
    assert store.get_members(CHAT_ID)[0].id == USER_ID


def test_passed_update_creates_confirmation_and_callback_commits_to_sqlite(tmp_path):
    store = Store(tmp_path / "ledger.db")
    chat = ScriptedChat(_chat_result(tool="propose_expense"))
    bot = FakeTelegramBot()
    logic = _logic(store, chat, FakeLLM([_expense_extraction()]), FixedRouter(0.1))
    context = _context(bot, logic, store)

    asyncio.run(_on_message(_message_update(bot, message_id=10, text="שילמתי 50 שקל על פיצה"), context))

    assert len(bot.sent) == 1
    markup = bot.sent[0]["reply_markup"]
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["expense:1:yes", "expense:1:no"], ["expense:1:edit", "expense:1:delete"]
    ]
    assert store.get_expense(1).state == ExpenseState.pending_confirmation
    assert store.get_bot_message(CHAT_ID, 901) == ("expense", 1)

    asyncio.run(_on_callback(_callback_update(bot, data="expense:1:yes"), context))

    assert store.get_expense(1).state == ExpenseState.confirmed
    assert bot.edited[-1]["text"] == "✓ אושר"


def test_pending_confirmation_survives_process_restart_and_can_be_cancelled(tmp_path):
    database = tmp_path / "ledger.db"
    first_store = Store(database)
    first_bot = FakeTelegramBot()
    first_context = _context(
        first_bot,
        _logic(
            first_store, ScriptedChat(_chat_result(tool="propose_expense")),
            FakeLLM([_expense_extraction()]), FixedRouter(0.1),
        ),
        first_store,
    )
    asyncio.run(_on_message(
        _message_update(first_bot, message_id=20, text="שילמתי 50 שקל על פיצה"), first_context
    ))
    first_store._db.close()  # simulate the process releasing its SQLite connection

    restarted_store = Store(database)
    restarted_bot = FakeTelegramBot()
    restarted_context = _context(
        restarted_bot,
        _logic(restarted_store, ScriptedChat(), FakeLLM([]), FixedRouter(0.1)),
        restarted_store,
    )
    asyncio.run(_on_callback(
        _callback_update(restarted_bot, data="expense:1:no", confirmation_message_id=901),
        restarted_context,
    ))

    assert restarted_store.get_expense(1).state == ExpenseState.rejected
    assert restarted_store.search_expenses(CHAT_ID) == []
    assert restarted_bot.edited[-1]["text"] == "✗ בוטל"
