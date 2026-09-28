"""BotLogic (contract tests): routing (mention/reply bypass, router threshold), auto-registered
roster (`Store.upsert_member`/`get_members`), reply-target resolution, confirmation buttons, and
button-press handling (approve/reject/errors).

Offline: in-memory Store, `BotLogic` built with a scripted FakeChat (as `chat_llm`) and a scripted
FakeLLM (as `write_llm`) -- `BotLogic` itself builds a fresh `Agent`/`WriteTools`/`GroupConfig` per
`handle_message` call from the store's CURRENT roster, so there is no long-lived Agent/WriteTools
for tests to hold onto; a fake Router; a fixed clock. Helpers below are copied in this file's own
style from tests/integration/test_agent.py and tests/unit/test_router.py (not imported from
another test file, per the test-writing rules).

Roster: unless a test pre-registers other members via `store.upsert_member`, the only member is
whoever sends the message in that test (auto-registered by `handle_message` itself). Sender 1 is
always named "זיו" in these tests.
"""

import copy
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from splitbot.agent.agent import Agent
from splitbot.bot.telegram_bot import BotLogic, OutgoingMessage, _configure_correction_debug_log
from splitbot.llm.client import ChatResult, ToolCall
from splitbot.models import (
    ApprovalMode,
    ChangeKind,
    ChangeRequest,
    Currency,
    Expense,
    ExpenseState,
    Member,
    Settlement,
    Share,
    Subcategory,
)
from splitbot.router.base import RouteResult
from splitbot.store import Store
from tests.fakes import FakeLLM

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
CHAT = 100
THRESHOLD = 0.5


@pytest.fixture(autouse=True)
def _agent_prompt(monkeypatch):
    """The agent prompt file's own content is not what these tests are about."""
    monkeypatch.setattr("splitbot.agent.agent.load_prompt", lambda version: "AGENT-PROMPT-MARKER")


# --- helpers: the scripted agent model (copied style from test_agent.py) --------------------------------

_ids = iter(range(1, 10_000))


def _tc(name, arguments=None) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, arguments=arguments if arguments is not None else {})


def _result(content, calls, cost) -> ChatResult:
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in calls
        ]
    return ChatResult(
        content=content, tool_calls=tuple(calls), message=message, model="fake-chat", temperature=0.0,
        input_tokens=10, output_tokens=5, latency_s=0.0, cost_usd=cost,
    )


def _calls(*calls, content=None, cost=Decimal("0")) -> ChatResult:
    return _result(content, list(calls), cost)


def _say(text, cost=Decimal("0")) -> ChatResult:
    return _result(text, [], cost)


class FakeChat:
    """A scripted ChatLLM. Each call pops the next ChatResult; every call is recorded. `BotLogic`
    reuses this single instance as `chat_llm` across every fresh `Agent` it builds per call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, tools):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if not self._script:
            raise AssertionError("FakeChat: more calls than scripted replies")
        return self._script.pop(0)


# --- helpers: a fake Router (copied style from tests/unit/test_router.py) -------------------------------


class FakeRouter:
    """A scripted Router. Records every message it was asked to route."""

    name = "fake"

    def __init__(self, result: RouteResult):
        self._result = result
        self.calls: list[str] = []

    def route(self, message: str) -> RouteResult:
        self.calls.append(message)
        return self._result


def _route(ignore: float, label: str = "ignore", error: str | None = None) -> RouteResult:
    scores = {"expense": 1.0 - ignore, "query": 0.0, "ignore": ignore}
    return RouteResult(label=label, scores=scores, latency_s=0.01, cost_usd=None, error=error)


# --- helpers: extractor replies (copied style from test_agent.py) ----------------------------------------


def _ref(x):
    return x if isinstance(x, dict) else {"kind": "known", "id": x}


def _with(only, evidence, exclude=()):
    return {
        "value": {"only": None if only is None else [_ref(i) for i in only], "exclude": [_ref(i) for i in exclude]},
        "evidence": evidence,
        "source": "message",
    }


def _reply(message_type="new", *, amount="120", evidence=None, confidence="high", payer=None, participants=None,
           subcategory="restaurant", description=None) -> str:
    data = {
        "confidence": confidence,
        "amount": None if amount is None else {"value": amount, "evidence": evidence or amount, "source": "message"},
        "amount_in_words": False,
        "currency": {"value": "ILS", "evidence": None, "source": "default"},
        "payer": payer or {"value": {"kind": "known", "id": 1}, "evidence": None, "source": "default"},
        "participants": participants,
        "exact_amounts": None,
        "refers_to": None,
        "subcategory": subcategory,
        "description": description,
    }
    return json.dumps(data, ensure_ascii=False)


_NEW_PIZZA = _reply(participants=_with([1, 2], "עם דני"))
_NEW_PIZZA_TEXT = "פיצה 120 עם דני"
_DELETE_REPLY = _reply("delete", amount=None)
_DELETE_TEXT = "תמחק את הפיצה"


# --- helpers: world (a real Store, a fake chat/router, a BotLogic that builds Agent/WriteTools per call) --


def _make(chat_script, extractor=(), *, threshold=THRESHOLD, clock=None, route_result=None,
           default_mode=ApprovalMode.author):
    store = Store(":memory:")
    llm = FakeLLM(list(extractor))
    chat = FakeChat(chat_script)
    router = FakeRouter(route_result if route_result is not None else _route(0.0))
    bot = BotLogic(
        store, chat, llm, router,
        default_mode=default_mode, agent_prompt_version="agent_v4", extractor_prompt_version="extract_v2",
        threshold=threshold, clock=clock or (lambda: NOW),
    )
    return SimpleNamespace(store=store, llm=llm, chat=chat, router=router, bot=bot)


def _seed_confirmed(store, message_id, shares, *, description="פיצה", subcategory=Subcategory.restaurant,
                     chat_id=CHAT, author_id=1) -> int:
    total = sum(s.paid for s in shares)
    return store.save_expense(
        Expense(
            chat_id=chat_id, message_id=message_id, author_id=author_id, description=description, total=total,
            currency=Currency.ILS, subcategory=subcategory, shares=shares, prompt_version="extract_v2",
            spent_on=date(2026, 2, 20), created_at=NOW - timedelta(days=1), state=ExpenseState.confirmed,
        )
    ).id


def _seed_pending(store, message_id, shares, *, description="פיצה", subcategory=Subcategory.restaurant,
                   chat_id=CHAT, author_id=1) -> int:
    total = sum(s.paid for s in shares)
    return store.save_expense(
        Expense(
            chat_id=chat_id, message_id=message_id, author_id=author_id, description=description, total=total,
            currency=Currency.ILS, subcategory=subcategory, shares=shares, prompt_version="extract_v2",
            spent_on=date(2026, 2, 20), created_at=NOW, state=ExpenseState.pending_confirmation,
        )
    ).id


def _seed_change_request(store, message_id, *, expense_id, kind=ChangeKind.delete, requested_by, required_approvers,
                          chat_id=CHAT, proposed=None) -> int:
    request = ChangeRequest(
        chat_id=chat_id, message_id=message_id, expense_id=expense_id, kind=kind, requested_by=requested_by,
        proposed=proposed, required_approvers=required_approvers, created_at=NOW,
    )
    return store.create_change_request(request).id


class _Spy:
    def __init__(self):
        self.calls: list[dict] = []  # kwargs of each call
        self.results: list = []  # the AgentReply each call returned


def _spy_agent_run_turn(monkeypatch) -> _Spy:
    """`BotLogic` builds a fresh `Agent` per `handle_message` call, so there is no single instance
    to attach a spy to: instead this patches `Agent.run_turn` at the CLASS level (reverted
    automatically by the `monkeypatch` fixture), recording every call's kwargs and return value
    across every `Agent` instance `BotLogic` builds during the test. The real method still runs."""
    spy = _Spy()
    original = Agent.run_turn

    def wrapper(self, **kwargs):
        spy.calls.append(kwargs)
        out = original(self, **kwargs)
        spy.results.append(out)
        return out

    monkeypatch.setattr(Agent, "run_turn", wrapper)
    return spy


def _count(store) -> int:
    n = 0
    while True:
        try:
            store.get_expense(n + 1)
        except KeyError:
            return n
        n += 1


def _sparse_reply(**fields) -> str:
    return json.dumps({"confidence": "high", **fields}, ensure_ascii=False)


_THREE = [Share(user_id=1, paid=15000, owed=5000), Share(user_id=2, paid=0, owed=5000), Share(user_id=3, paid=0, owed=5000)]
_ONE = [Share(user_id=1, paid=5000, owed=5000)]
_TWO = [Share(user_id=1, paid=10000, owed=5000), Share(user_id=2, paid=0, owed=5000)]


# === 1. mention / reply always reach the agent, bypassing the router =====================================


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(is_mention=True, reply_to_telegram_message_id=None), id="mention"),
        pytest.param(dict(is_mention=False, reply_to_telegram_message_id=4242), id="reply"),
    ],
)
def test_a_mention_or_reply_always_reaches_the_agent_even_when_the_router_would_ignore_it(kwargs):
    # the router is scripted to say "ignore" with a score far above any reasonable threshold
    env = _make([_say("שלום")], route_result=_route(0.99, label="ignore"))

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="לא קשור לכסף", **kwargs)

    assert len(env.chat.calls) == 1  # the agent WAS called
    assert env.router.calls == []  # the router was NEVER called


# === 2/3. plain messages go through the router, thresholded on the ignore score ==========================


def test_a_plain_message_below_the_ignore_threshold_goes_to_the_router_then_the_agent():
    env = _make([_say("שלום")], route_result=_route(THRESHOLD - 0.01, label="expense"))

    out = env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="פיצה 50",
                                  is_mention=False, reply_to_telegram_message_id=None)

    assert env.router.calls == ["פיצה 50"]  # the router was called exactly once
    assert len(env.chat.calls) == 1
    assert out[0].text == "שלום"
    assert out[0].buttons == ()


def test_a_plain_message_at_or_above_the_ignore_threshold_is_dropped_with_no_agent_call():
    env = _make([], route_result=_route(THRESHOLD, label="ignore"))

    out = env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="מה נשמע",
                                  is_mention=False, reply_to_telegram_message_id=None)

    assert out == []
    assert env.chat.calls == []  # the agent was never called
    assert not env.store.is_processed(CHAT, 1)  # nothing written to the store
    assert _count(env.store) == 0


def test_obsolete_router_confirmation_callback_is_rejected():
    env = _make([])
    result = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data="route:1:yes")
    assert result.alert is True


# === 4-7. reply-target resolution ==========================================================================


def test_reply_to_a_bot_expense_message_resolves_the_reply_target_to_that_expense(monkeypatch):
    env = _make([_say("שלום")])
    pending_id = _seed_pending(env.store, 1, _ONE)
    env.store.record_bot_message(CHAT, 555, "expense", pending_id)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2, text="כן",
                            is_mention=False, reply_to_telegram_message_id=555)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] == pending_id


def test_reply_to_a_bot_change_request_message_resolves_the_reply_target_to_the_change_requests_own_expense_id(monkeypatch):
    env = _make([_say("שלום")])
    _seed_confirmed(env.store, 1, _ONE)  # a throwaway expense first, so ids below cannot coincide by luck
    expense_id = _seed_confirmed(env.store, 2, _ONE)
    request_id = _seed_change_request(env.store, 3, expense_id=expense_id, requested_by=1, required_approvers=[1])
    assert request_id != expense_id  # expenses and change_requests are separate id sequences: force them apart
    env.store.record_bot_message(CHAT, 556, "change_request", request_id)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=4, text="כן",
                            is_mention=False, reply_to_telegram_message_id=556)

    assert len(spy.calls) == 1
    resolved = spy.calls[0]["reply_target_expense_id"]
    assert resolved == expense_id
    assert resolved != 556
    assert resolved != request_id


def test_reply_to_an_unrecorded_telegram_message_id_is_the_same_as_no_reply(monkeypatch):
    env = _make([_say("שלום")])
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="כן",
                            is_mention=False, reply_to_telegram_message_id=999_999)

    assert len(spy.calls) == 1  # a reply always reaches the agent
    assert spy.calls[0]["reply_target_expense_id"] is None


def test_reply_to_the_original_message_that_created_a_pending_expense_also_resolves_the_target(monkeypatch):
    # message_id=700 stands in for the USER'S OWN original report message (not a bot message):
    # record_bot_message is deliberately never called for it, so the bot_messages lookup misses
    # and only the get_expense_by_message fallback can resolve this reply.
    env = _make([_say("שלום")])
    pending_id = _seed_pending(env.store, 700, _ONE)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=701, text="זה היה 100 ולא 50",
                            is_mention=False, reply_to_telegram_message_id=700)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] == pending_id


def test_reply_to_a_change_request_that_no_longer_exists_falls_back_to_no_reply_target(monkeypatch):
    env = _make([_say("שלום")])
    env.store.record_bot_message(CHAT, 557, "change_request", 99_999)  # not a real change request id
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="כן",
                            is_mention=False, reply_to_telegram_message_id=557)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] is None  # no exception, just a fallback


# === 8. a pending confirmation gets the yes/no buttons and is recorded ====================================


def test_a_turn_that_ends_in_a_pending_confirmation_gets_yes_no_buttons_and_is_recorded_for_a_new_expense(monkeypatch):
    env = _make([_calls(_tc("propose_expense"))], [_NEW_PIZZA])
    env.store.upsert_member(CHAT, 2, "דני")  # named as a participant in the message; must be on the roster
    spy = _spy_agent_run_turn(monkeypatch)

    out = env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text=_NEW_PIZZA_TEXT,
                                  is_mention=True, reply_to_telegram_message_id=None)

    (reply,) = spy.results
    (proposal,) = reply.proposals
    assert proposal.status == "pending_confirmation" and proposal.change_request_id is None
    expense_id = proposal.expense_id

    assert len(out) == 1
    message = out[0]
    assert "האם להוסיף הוצאה חדשה" in message.text
    assert "סכום: 120 שקל חדש (₪)" in message.text
    assert "משתתפים: דני, זיו" in message.text
    assert f'\"{_NEW_PIZZA_TEXT}\"' in message.text
    assert message.buttons == (
        ("✅ אישור", f"expense:{expense_id}:yes"),
        ("❌ ביטול", f"expense:{expense_id}:no"),
        ("✏️ תיקון", f"expense:{expense_id}:edit"),
        ("🗑️ מחיקה", f"expense:{expense_id}:delete"),
    )
    assert message.record_as == ("expense", expense_id)


def test_a_turn_that_ends_in_a_pending_confirmation_gets_yes_no_buttons_and_is_recorded_for_a_change_request(monkeypatch):
    # the first expense saved into a fresh store always gets id 1, so the scripted propose_delete
    # call can reference it as the model would after seeing it in this turn's search_expenses result
    env = _make(
        [_calls(_tc("search_expenses", {"text": "פיצה"})), _calls(_tc("propose_delete", {"target_expense_id": 1}))],
        [_DELETE_REPLY],
    )
    original_id = _seed_confirmed(env.store, 10, _THREE, description="פיצה")
    env.store.upsert_member(CHAT, 2, "דני")
    env.store.upsert_member(CHAT, 3, "משה")
    assert original_id == 1
    spy = _spy_agent_run_turn(monkeypatch)

    out = env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=50, text=_DELETE_TEXT,
                                  is_mention=True, reply_to_telegram_message_id=None)

    (reply,) = spy.results
    (proposal,) = reply.proposals
    assert proposal.status == "pending_confirmation" and proposal.expense_id == original_id
    request_id = proposal.change_request_id
    assert request_id is not None

    assert len(out) == 1
    message = out[0]
    assert message.text == (
        "🗑️ האם למחוק את ההוצאה?\n\n"
        "תיאור: פיצה\n"
        "סכום: 150 שקל חדש (₪)\n"
        "תאריך: 2026-02-20\n"
        "שילם/ה: זיו\n"
        "משתתפים: זיו, דני, משה\n"
        "חלוקה:\n"
        "• זיו: 50 שקל חדש (₪)\n"
        "• דני: 50 שקל חדש (₪)\n"
        "• משה: 50 שקל חדש (₪)\n\n"
        f"בקשת המשתמש: \"{_DELETE_TEXT}\""
    )
    assert message.buttons == (("✅ אישור", f"change_request:{request_id}:yes"),
                               ("❌ ביטול", f"change_request:{request_id}:no"))
    assert message.record_as == ("change_request", request_id)


# === 9. a plain answer has no buttons and is not recorded ==================================================


def test_a_plain_answer_or_ask_has_no_buttons_and_is_not_recorded():
    env = _make([_say("אין חובות פתוחים כרגע.")])

    out = env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="מי חייב למי?",
                                  is_mention=True, reply_to_telegram_message_id=None)

    assert len(out) == 1
    message = out[0]
    assert message.buttons == ()
    assert message.record_as is None


# === 10. handle_callback: confirm / reject a pending expense ==============================================


def test_handle_callback_confirms_a_pending_expense():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:yes")

    assert out.text == "✓ אושר"
    assert out.edit_original is True
    assert out.buttons == (
        ("✏️ תיקון", f"expense:{expense_id}:edit"),
        ("🗑️ מחיקה", f"expense:{expense_id}:delete"),
    )
    assert env.store.get_expense(expense_id).state == ExpenseState.confirmed


def test_handle_callback_rejects_a_pending_expense():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:no")

    assert out.text == "✗ בוטל"
    assert env.store.get_expense(expense_id).state == ExpenseState.rejected


# === 11. handle_callback: a pending change request =========================================================


def test_handle_callback_confirms_a_change_request_a_group_of_one_applies_it_immediately():
    env = _make([])
    expense_id = _seed_confirmed(env.store, 1, _ONE, author_id=1)
    request_id = _seed_change_request(env.store, 2, expense_id=expense_id, requested_by=1, required_approvers=[1])

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"change_request:{request_id}:yes")

    assert out.text == "✓ אושר"
    assert env.store.get_change_request(request_id).state == ExpenseState.confirmed
    assert env.store.get_expense(expense_id).deleted is True


def test_handle_callback_records_one_vote_of_a_change_request_that_needs_several_approvers():
    env = _make([])
    expense_id = _seed_confirmed(env.store, 1, _TWO, author_id=1)  # two people share it: 1 and 2
    request_id = _seed_change_request(env.store, 2, expense_id=expense_id, requested_by=1, required_approvers=[1, 2])

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"change_request:{request_id}:yes")

    assert out.text == "✓ אושר"  # the ack is fixed even though the request is not yet resolved
    assert env.store.get_change_request(request_id).state == ExpenseState.pending_confirmation
    assert env.store.get_expense(expense_id).deleted is False


# === 12-15. handle_callback errors: never a crash, never a leaked reason ===================================


def test_handle_callback_rejects_a_button_from_someone_who_is_not_a_relevant_approver():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)  # only user 1 may answer this

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=2, callback_data=f"expense:{expense_id}:yes")

    assert isinstance(out, OutgoingMessage)
    assert out.text and out.text not in ("✓ אושר", "✗ בוטל")
    assert out.edit_original is False  # adapter must answer privately and leave the real owner's buttons intact
    assert env.store.get_expense(expense_id).state == ExpenseState.pending_confirmation  # unchanged


def test_handle_callback_on_an_already_answered_item_is_a_fixed_error_not_a_crash():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    first = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:yes")
    assert first.text == "✓ אושר"  # sanity: the first press worked

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:yes")

    assert isinstance(out, OutgoingMessage)
    assert out.text and out.text not in ("✓ אושר", "✗ בוטל")
    assert env.store.get_expense(expense_id).state == ExpenseState.confirmed  # no double-processing


_MALFORMED = [
    pytest.param("bogus", id="no-colons-at-all"),
    pytest.param("expense:1", id="wrong-number-of-parts-too-few"),
    pytest.param("expense:1:yes:extra", id="wrong-number-of-parts-too-many"),
    pytest.param("expense:not-a-number:yes", id="unparseable-id"),
    pytest.param("something_else:1:yes", id="unknown-kind"),
    pytest.param("expense:1:maybe", id="unparseable-yes-no-action"),
]


@pytest.mark.parametrize("callback_data", _MALFORMED)
def test_handle_callback_with_malformed_callback_data_is_a_fixed_error(callback_data):
    env = _make([])

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=callback_data)

    assert isinstance(out, OutgoingMessage)
    assert out.text and out.text not in ("✓ אושר", "✗ בוטל")


def test_handle_callback_never_reveals_the_specific_reason_in_the_error_text():
    """The docstring's own wording: NotRelevantApprover, IllegalTransition (already answered or
    expired), StateConflict, and an unknown id (KeyError) all become the SAME fixed error
    message -- a person who is not a relevant approver should not learn that from the error
    text. This test checks exactly that: the texts from distinct failure causes are identical,
    not merely "similarly vague"."""
    env = _make([])

    # cause 1: not a relevant approver
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    not_approver = env.bot.handle_callback(chat_id=CHAT, sender_id=2, callback_data=f"expense:{expense_id}:yes")

    # cause 2: already answered (IllegalTransition)
    other_id = _seed_pending(env.store, 2, _ONE, author_id=1)
    env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{other_id}:yes")
    already_answered = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{other_id}:yes")

    # cause 3: unknown id (KeyError)
    unknown_id = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data="expense:999999:yes")

    # cause 4: malformed data entirely
    malformed = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data="bogus")

    texts = {not_approver.text, already_answered.text, unknown_id.text, malformed.text}
    assert len(texts) == 1  # every distinct cause produces the exact same text


# === 16-19. the roster is auto-registered from incoming messages, never a fixed list ======================


def test_a_first_time_sender_is_registered_and_immediately_usable_in_the_same_call():
    env = _make([_say("אין חובות פתוחים כרגע.")])  # a fresh store: nobody has ever posted here

    out = env.bot.handle_message(chat_id=CHAT, sender_id=777, sender_name="נועה", message_id=1,
                                  text="מי חייב למי?", is_mention=False, reply_to_telegram_message_id=None)

    assert env.store.get_members(CHAT) == [Member(id=777, name="נועה")]
    assert len(env.chat.calls) == 1
    assert out[0].text == "אין חובות פתוחים כרגע."


def test_the_roster_grows_across_messages_and_a_later_message_sees_everyone_so_far():
    # non-monotonic ids on purpose: an accidental "order by id" implementation would fail this
    env = _make([], route_result=_route(0.99, label="ignore"))  # ignored: registration is all we test here

    env.bot.handle_message(chat_id=CHAT, sender_id=50, sender_name="נועה", message_id=1, text="א",
                            is_mention=False, reply_to_telegram_message_id=None)
    env.bot.handle_message(chat_id=CHAT, sender_id=3, sender_name="זיו", message_id=2, text="ב",
                            is_mention=False, reply_to_telegram_message_id=None)
    env.bot.handle_message(chat_id=CHAT, sender_id=200, sender_name="דני", message_id=3, text="ג",
                            is_mention=False, reply_to_telegram_message_id=None)

    assert env.store.get_members(CHAT) == [
        Member(id=50, name="נועה"), Member(id=3, name="זיו"), Member(id=200, name="דני"),
    ]  # first-seen order, NOT sorted by id (50, 3, 200 is not ascending)


def test_a_returning_senders_display_name_is_refreshed_not_duplicated():
    env = _make([], route_result=_route(0.99, label="ignore"))

    env.bot.handle_message(chat_id=CHAT, sender_id=42, sender_name="דנה", message_id=1, text="שלום",
                            is_mention=False, reply_to_telegram_message_id=None)
    env.bot.handle_message(chat_id=CHAT, sender_id=42, sender_name="דנה כהן", message_id=2, text="שוב שלום",
                            is_mention=False, reply_to_telegram_message_id=None)

    members = env.store.get_members(CHAT)
    assert members == [Member(id=42, name="דנה כהן")]  # exactly one row, the LATEST name


def test_handle_message_still_ignores_correctly_even_though_the_sender_gets_registered():
    env = _make([], route_result=_route(0.99, label="ignore"))

    out = env.bot.handle_message(chat_id=CHAT, sender_id=5, sender_name="אור", message_id=1, text="מה נשמע",
                                  is_mention=False, reply_to_telegram_message_id=None)

    assert out == []
    assert env.chat.calls == []  # the agent was never called: the ignore path is unaffected
    assert env.store.get_members(CHAT) == [Member(id=5, name="אור")]  # but registration is unconditional


# === 20-25. conversation history and the "sole pending expense" reply-target fallback =====================


def test_conversation_history_is_logged_for_both_the_user_and_the_bot_and_passed_to_the_agent(monkeypatch):
    env = _make([_say("שלום זיו"), _say("בסדר גמור")])
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="היי",
                            is_mention=True, reply_to_telegram_message_id=None)

    history = env.store.recent_messages(CHAT)
    assert history == ["זיו: היי", "bot: שלום זיו"]  # the user's line is logged BEFORE the bot's own reply
    assert spy.calls[0]["recent_messages"] == []  # nothing happened before the very first message

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2, text="מה נשמע",
                            is_mention=True, reply_to_telegram_message_id=None)

    assert len(spy.calls) == 2
    second_recent = spy.calls[1]["recent_messages"]
    assert second_recent == ["זיו: היי", "bot: שלום זיו"]  # includes the first exchange
    assert "מה נשמע" not in second_recent  # a call never sees its own text in its own history


def test_history_grows_across_multiple_turns_and_stays_capped_at_the_configured_limit(monkeypatch):
    n = 11
    env = _make([_say(f"תגובה {i}") for i in range(n)])
    spy = _spy_agent_run_turn(monkeypatch)

    for i in range(n):
        env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=i + 1, text=f"הודעה {i}",
                                is_mention=True, reply_to_telegram_message_id=None)

    latest_recent = spy.calls[-1]["recent_messages"]
    assert len(latest_recent) == 8  # capped, even though 20 lines have been logged by now
    assert latest_recent == [
        "זיו: הודעה 6", "bot: תגובה 6", "זיו: הודעה 7", "bot: תגובה 7",
        "זיו: הודעה 8", "bot: תגובה 8", "זיו: הודעה 9", "bot: תגובה 9",
    ]  # the MOST RECENT 8 lines, not the oldest


def test_a_plain_correction_with_no_reply_defaults_to_the_senders_sole_pending_expense(monkeypatch):
    env = _make([_say("בסדר")])
    pending_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2, text="זה היה 100 ולא 150",
                            is_mention=False, reply_to_telegram_message_id=None)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] == pending_id  # defaulted even with no reply link at all


def test_no_pending_has_no_fallback_but_multiple_pending_default_to_the_senders_latest(monkeypatch):
    env = _make([_say("בסדר"), _say("בסדר")])
    spy = _spy_agent_run_turn(monkeypatch)

    # zero pending expenses of the sender's own -> no fallback, same as before
    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=1, text="הודעה כלשהי",
                            is_mention=False, reply_to_telegram_message_id=None)
    assert spy.calls[0]["reply_target_expense_id"] is None

    # An immediate no-reply correction belongs to the latest proposal; older open proposals do
    # not make that natural conversational continuation ambiguous.
    _seed_pending(env.store, 2, _ONE, author_id=1)
    latest_id = _seed_pending(env.store, 3, _ONE, author_id=1)
    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=4, text="הודעה כלשהי",
                            is_mention=False, reply_to_telegram_message_id=None)
    assert spy.calls[1]["reply_target_expense_id"] == latest_id


def test_an_explicit_reply_still_wins_over_the_sole_pending_fallback(monkeypatch):
    env = _make([_say("בסדר")])
    sole_pending_id = _seed_pending(env.store, 1, _ONE, author_id=1)  # would trigger the fallback alone
    other_expense_id = _seed_confirmed(env.store, 2, _ONE, author_id=1)
    env.store.record_bot_message(CHAT, 900, "expense", other_expense_id)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=3, text="כן",
                            is_mention=False, reply_to_telegram_message_id=900)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] == other_expense_id
    assert spy.calls[0]["reply_target_expense_id"] != sole_pending_id


def test_the_sole_pending_fallback_is_scoped_to_the_sender_not_the_whole_chat(monkeypatch):
    env = _make([_say("בסדר")])
    b_pending_id = _seed_pending(env.store, 1, _ONE, author_id=2)  # sender B's OWN pending expense
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2, text="הודעה כלשהי",
                            is_mention=False, reply_to_telegram_message_id=None)

    assert len(spy.calls) == 1
    assert spy.calls[0]["reply_target_expense_id"] is None
    assert spy.calls[0]["reply_target_expense_id"] != b_pending_id


def test_an_expired_pending_expense_does_not_make_the_senders_recent_pending_expense_ambiguous(monkeypatch):
    """Expiry is part of resolving a no-reply follow-up, not a separate maintenance assumption."""
    env = _make([_say("בסדר")])
    stale = Expense(
        chat_id=CHAT,
        message_id=1,
        author_id=1,
        description="קפה ישן",
        total=5000,
        currency=Currency.ILS,
        subcategory=Subcategory.restaurant,
        shares=_ONE,
        prompt_version="extract_v2",
        spent_on=date(2026, 2, 20),
        created_at=NOW - timedelta(hours=2),
        state=ExpenseState.pending_confirmation,
    )
    env.store.save_expense(stale)
    recent_id = _seed_pending(env.store, 2, _ONE, description="קפה חדש", author_id=1)
    spy = _spy_agent_run_turn(monkeypatch)

    env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=3,
        text="בעצם זה היה 40 ולא 50",
        is_mention=False,
        reply_to_telegram_message_id=None,
    )

    assert spy.calls[0]["reply_target_expense_id"] == recent_id


@pytest.mark.parametrize(
    ("target_state", "allowed_tool"),
    [
        pytest.param(ExpenseState.pending_confirmation, "revise_pending", id="pending-is-revised-in-place"),
        pytest.param(ExpenseState.confirmed, "propose_correction", id="confirmed-gets-a-change-request"),
    ],
)
def test_the_agent_context_distinguishes_a_pending_target_from_a_confirmed_target(target_state, allowed_tool):
    env = _make([_say("בסדר")])
    if target_state == ExpenseState.pending_confirmation:
        expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    else:
        expense_id = _seed_confirmed(env.store, 1, _ONE, author_id=1)
    env.store.record_bot_message(CHAT, 900, "expense", expense_id)

    env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=2,
        text="בעצם זה היה 40 ולא 50",
        is_mention=False,
        reply_to_telegram_message_id=900,
    )

    system_context = env.chat.calls[0]["messages"][0]["content"]
    assert f"Target expense state: {target_state.value}" in system_context
    expected = (
        f"Pending correction candidate: {expense_id} (use revise_pending ONLY if the current message corrects it; "
        "a newly reported payment is still propose_expense)"
        if target_state == ExpenseState.pending_confirmation
        else f"Confirmed target candidate: {expense_id}"
    )
    assert expected in system_context


def test_an_expense_found_in_one_turn_can_be_selected_for_deletion_in_the_next_turn():
    env = _make(
        [
            _calls(_tc("search_expenses", {"text": "פיצה"})),
            _say("מצאתי"),
            _calls(_tc("propose_delete", {"target_expense_id": 1})),
        ],
        [_DELETE_REPLY],
    )
    expense_id = _seed_confirmed(env.store, 1, _ONE, description="פיצה", author_id=1)

    env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=2,
        text="תמצא לי את הפיצה",
        is_mention=True,
        reply_to_telegram_message_id=None,
    )
    out = env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=3,
        text="תמחק את 1",
        is_mention=True,
        reply_to_telegram_message_id=None,
    )

    request = env.store.get_change_request(1)
    assert request.expense_id == expense_id
    assert request.state == ExpenseState.pending_confirmation
    assert out[0].record_as == ("change_request", request.id)
    assert "Selection 1: expense id 1" in env.chat.calls[2]["messages"][0]["content"]


def test_a_pure_search_is_rendered_as_a_numbered_hebrew_list_in_tool_result_order():
    env = _make([_calls(_tc("search_expenses", {})), _say("ניסוח שרירותי של המודל")])
    _seed_confirmed(env.store, 1, _ONE, description="פיצה", author_id=1)
    _seed_confirmed(env.store, 2, _ONE, description="סושי", author_id=1)

    out = env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=3,
        text="תציג לי את כל ההוצאות",
        is_mention=True,
        reply_to_telegram_message_id=None,
    )

    assert out[0].text == (
        "🔎 מצאתי את ההוצאות הבאות:\n\n"
        "1. סושי\nסכום: ₪50\nתאריך: 2026-02-20\nשילם/ה: זיו\n"
        "משתתפים: זיו\nחלוקה:\n   • זיו: ₪50\n\n"
        "2. פיצה\nסכום: ₪50\nתאריך: 2026-02-20\nשילם/ה: זיו\n"
        "משתתפים: זיו\nחלוקה:\n   • זיו: ₪50\n"
        "אפשר לכתוב למשל: „תתקן את 2” או „תמחק את 1”."
    )
    assert "ניסוח שרירותי" not in out[0].text
    assert out[0].buttons == (
        ("✏️ 1", "expense:2:edit"),
        ("🗑️ 1", "expense:2:delete"),
        ("✏️ 2", "expense:1:edit"),
        ("🗑️ 2", "expense:1:delete"),
    )


def test_propose_correction_for_a_pending_target_is_safely_dispatched_as_a_pending_revision():
    env = _make([_calls(_tc("propose_correction"))], [_reply("correction", amount="40")])
    pending_id = _seed_pending(env.store, 1, _ONE, description="קפה", author_id=1)
    env.store.record_bot_message(CHAT, 900, "expense", pending_id)

    out = env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=2,
        text="בעצם זה היה 40 ולא 50",
        is_mention=False,
        reply_to_telegram_message_id=900,
    )

    revised = env.store.get_expense(pending_id)
    assert revised.state == ExpenseState.pending_confirmation
    assert revised.total == 4000
    assert out[0].record_as == ("expense", pending_id)
    with pytest.raises(KeyError):
        env.store.get_change_request(1)


def test_telegram_renders_and_confirms_a_pending_settlement():
    payer = {"value": {"kind": "known", "id": 2}, "evidence": None, "source": "default"}
    extracted = _reply(amount="50", payer=payer, participants=_with([1], "לזיו"), description=None)
    env = _make([_calls(_tc("propose_settlement"))], [extracted])
    env.store.upsert_member(CHAT, 1, "זיו")
    env.store.upsert_member(CHAT, 2, "דני")
    _seed_confirmed(
        env.store,
        1,
        [Share(user_id=1, paid=10000, owed=5000), Share(user_id=2, paid=0, owed=5000)],
        author_id=1,
    )

    out = env.bot.handle_message(
        chat_id=CHAT, sender_id=2, sender_name="דני", message_id=2,
        text="החזרתי לזיו 50", is_mention=True, reply_to_telegram_message_id=None,
    )

    assert out[0].text == (
        "💸 האם לרשום החזר חוב?\n\n"
        "מאת: דני\nאל: זיו\nסכום: 50 שקל חדש (₪)\n\n"
        "הודעת המקור: \"החזרתי לזיו 50\""
    )
    assert out[0].buttons == (("✅ אישור", "settlement:1:yes"), ("❌ ביטול", "settlement:1:no"))
    assert out[0].record_as == ("settlement", 1)

    acknowledged = env.bot.handle_callback(chat_id=CHAT, sender_id=2, callback_data="settlement:1:yes")

    assert env.store.get_settlement(1).state == ExpenseState.confirmed
    assert env.store.balances(CHAT)[Currency.ILS] == {1: 0, 2: 0}
    assert acknowledged.text == "✓ אושר"


def test_clear_incoming_settlement_is_selected_by_the_agent_after_router_passes_it():
    payer = {"value": {"kind": "known", "id": 2}, "evidence": "ירדן", "source": "message"}
    extracted = _reply(
        amount="20", payer=payer, participants=_with([1], "לי"), description=None,
    )
    env = _make([_calls(_tc("propose_settlement"))], [extracted], route_result=_route(0.0, label="expense"))
    env.store.upsert_member(CHAT, 2, "ירדן")
    _seed_confirmed(
        env.store, 10,
        [Share(user_id=1, paid=10000, owed=5000), Share(user_id=2, paid=0, owed=5000)],
        author_id=1,
    )

    result = env.bot.handle_message(
        chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=11,
        text="ירדן החזירה לי 20 שקל", is_mention=False, reply_to_telegram_message_id=None,
    )[0]

    assert len(env.chat.calls) == 1
    assert result.record_as == ("settlement", 1)
    assert "מאת: ירדן" in result.text and "אל: זיו" in result.text and "20" in result.text


def test_correction_debug_log_is_idempotent_useful_and_isolated_from_telegram_http_secrets(tmp_path):
    log_path = tmp_path / "corrections.log"
    loggers = [
        logging.getLogger("splitbot.tools.write_tools"),
        logging.getLogger("splitbot.bot.telegram_bot"),
        logging.getLogger("splitbot.agent.agent"),
    ]
    original_states = {
        logger: (list(logger.handlers), logger.level, logger.propagate)
        for logger in loggers
    }
    for logger in loggers:
        for existing in list(logger.handlers):
            logger.removeHandler(existing)

    try:
        first = _configure_correction_debug_log(str(log_path))
        second = _configure_correction_debug_log(str(log_path))
        assert second is first
        for logger in loggers:
            assert [
                handler for handler in logger.handlers
                if getattr(handler, "_splitbot_correction_debug", False)
            ] == [first]

        fake_token = "123456789:FAKE_TELEGRAM_SECRET"
        fake_api_url = f"https://api.telegram.org/bot{fake_token}/sendMessage"
        logging.getLogger("httpx").warning("HTTP Request: POST %s", fake_api_url)

        env = _make([_calls(_tc("propose_correction"))], [_reply("correction", amount="40")])
        pending_id = _seed_pending(env.store, 1, _ONE, description="קפה", author_id=1)
        env.store.record_bot_message(CHAT, 900, "expense", pending_id)
        env.bot.handle_message(
            chat_id=CHAT,
            sender_id=1,
            sender_name="זיו",
            message_id=2,
            text="בעצם זה היה 40 ולא 50",
            is_mention=False,
            reply_to_telegram_message_id=900,
        )
        first.flush()
        logged = log_path.read_text(encoding="utf-8")

        assert "correction_extraction start" in logged
        assert "agent_turn start" in logged
        assert "prompt_version=agent_v4" in logged
        assert "propose_settlement" in logged
        assert "agent_turn model_result" in logged
        assert "agent_turn tool_result" in logged
        assert "mode=pending" in logged
        assert f"chat_id={CHAT}" in logged and "message_id=2" in logged and "sender_id=1" in logged
        assert f"target_expense_id={pending_id}" in logged
        assert "prompt_version=correct_v2" in logged
        assert "הוצאה ממתינה לאישור" in logged
        assert "תיקון: בעצם זה היה 40 ולא 50" in logged
        assert "correction_extraction result" in logged
        assert "status=ok" in logged and "extractor_issues=[]" in logged
        assert "parsed=" in logged and "raw=" in logged and '"amount"' in logged
        assert fake_token not in logged
        assert fake_api_url not in logged
        assert "api.telegram.org" not in logged
    finally:
        attached = {
            handler
            for logger in loggers
            for handler in logger.handlers
            if getattr(handler, "_splitbot_correction_debug", False)
        }
        for logger in loggers:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
        for handler in attached:
            handler.close()
        for logger, (handlers, level, propagate) in original_states.items():
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate


def test_an_existing_pending_proposal_does_not_hijack_a_clearly_new_expense():
    """The pending id is conversational context, not an instruction to revise it.

    Tool choice belongs to the agent: when it calls ``propose_expense``, the write must create a
    second proposal even though BotLogic supplied the sender's latest pending proposal as context.
    """
    extracted = json.loads(_reply(amount="80", description="חשמל"))
    extracted.pop("message_type", None)
    env = _make([_calls(_tc("propose_expense"))], [json.dumps(extracted, ensure_ascii=False)])
    old_id = _seed_pending(env.store, 1, _ONE, description="פיצה", author_id=1)

    out = env.bot.handle_message(
        chat_id=CHAT,
        sender_id=1,
        sender_name="זיו",
        message_id=2,
        text="שילמתי 80 על חשמל בלי זיו",
        is_mention=True,
        reply_to_telegram_message_id=None,
    )

    old = env.store.get_expense(old_id)
    created = env.store.get_expense_by_message(CHAT, 2)
    assert old.description == "פיצה" and old.total == 5000 and old.version == 0
    assert created is not None and created.id != old_id
    assert created.description == "חשמל" and created.total == 8000
    assert out[0].record_as == ("expense", created.id)


def test_bot_logic_defaults_to_the_v4_agent_and_v3_expense_extractor_prompts():
    """Unlike `_make()` (which pins its own fixed prompt versions for test stability), this
    constructs `BotLogic` with no `agent_prompt_version`/`extractor_prompt_version` override, so
    it actually checks the class's own real defaults -- the ones the running bot gets."""
    bot = BotLogic(Store(":memory:"), None, None, None, clock=lambda: NOW)

    assert bot._agent_prompt_version == "agent_v4"
    assert bot._extractor_prompt_version == "extract_v3"


def test_editing_a_pending_expense_bypasses_router_and_agent_and_revises_the_explicit_target():
    participants = _with([2], "רק דני משתתף", exclude=[1])
    env = _make([], [_sparse_reply(amount=None, participants=participants)])
    env.store.upsert_member(CHAT, 2, "דני")
    expense_id = _seed_pending(env.store, 1, _TWO, description="פיצה", author_id=1)

    clicked = env.bot.handle_callback(
        chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:edit"
    )
    out = env.bot.handle_message(
        chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2,
        text="רק דני משתתף", is_mention=False, reply_to_telegram_message_id=None,
    )

    assert clicked.edit_original is False
    assert env.router.calls == []
    assert env.chat.calls == []
    revised = env.store.get_expense(expense_id)
    assert revised.total == 10000
    assert [share.user_id for share in revised.shares if share.owed] == [2]
    assert out[0].record_as == ("expense", expense_id)
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) is None


def test_editing_a_confirmed_expense_bypasses_router_and_agent_and_proposes_a_correction():
    env = _make([], [_sparse_reply(amount={"value": "40", "evidence": "40", "source": "message"})])
    env.store.upsert_member(CHAT, 2, "דני")
    expense_id = _seed_confirmed(env.store, 1, _TWO, description="פיצה", author_id=1)

    env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:edit")
    out = env.bot.handle_message(
        chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2,
        text="הסכום הוא 40", is_mention=False, reply_to_telegram_message_id=None,
    )

    assert env.router.calls == []
    assert env.chat.calls == []
    request = env.store.get_change_request(1)
    assert request.expense_id == expense_id and request.proposed.total == 4000
    assert out[0].record_as == ("change_request", request.id)
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) is None


def test_edit_targets_are_scoped_by_chat_and_sender_and_expire():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    env.store.begin_pending_edit(CHAT, 1, expense_id, now=NOW)

    assert env.store.pending_edit_target(CHAT, 1, now=NOW) == expense_id
    assert env.store.pending_edit_target(CHAT, 2, now=NOW) is None
    assert env.store.pending_edit_target(CHAT + 1, 1, now=NOW) is None
    assert env.store.pending_edit_target(CHAT, 1, now=NOW + timedelta(minutes=16)) is None
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) is None


# === deleting via the 🗑️ button is deterministic too: no router, no Agent tool choice =====================


def test_deleting_a_confirmed_expense_via_button_bypasses_router_and_agent_regardless_of_text():
    env = _make([])
    expense_id = _seed_confirmed(env.store, 1, _ONE, description="קפה", author_id=1)

    clicked = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:delete")
    # The confirming message's actual wording never matters: propose_delete extracts nothing
    # from it. Arbitrary text proves the target alone drives this, not the message content.
    out = env.bot.handle_message(
        chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2,
        text="בטח, תמחק", is_mention=False, reply_to_telegram_message_id=None,
    )

    assert clicked.edit_original is False
    assert env.router.calls == []
    assert env.chat.calls == []
    request = env.store.get_change_request(1)
    assert request.kind == ChangeKind.delete and request.expense_id == expense_id
    assert out[0].record_as == ("change_request", request.id)
    assert env.store.pending_delete_target(CHAT, 1, now=NOW) is None


def test_pressing_delete_on_a_still_pending_expense_self_invalidates_and_falls_through_to_the_agent():
    """A still-pending expense was never deletable this way (only `propose_delete`'s existing
    confirmed-only check applied before); `pending_delete_target` now self-invalidates instead of
    surfacing that as a downstream tool failure, and the sender's next message gets the normal
    router/Agent treatment, same as if no button had ever been pressed."""
    env = _make([_calls(_tc("get_balances")), _say("אין חובות פתוחים כרגע.")])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)

    env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:delete")
    assert env.store.pending_delete_target(CHAT, 1, now=NOW) is None  # already invalid: not confirmed

    out = env.bot.handle_message(
        chat_id=CHAT, sender_id=1, sender_name="זיו", message_id=2,
        text="מי חייב למי?", is_mention=True, reply_to_telegram_message_id=None,
    )

    assert env.chat.calls != []  # the Agent DID run this time -- no deterministic bypass applied
    assert out[0].record_as is None


def test_delete_targets_are_scoped_by_chat_and_sender_and_expire():
    env = _make([])
    expense_id = _seed_confirmed(env.store, 1, _ONE, author_id=1)
    env.store.begin_pending_delete(CHAT, 1, expense_id, now=NOW)

    assert env.store.pending_delete_target(CHAT, 1, now=NOW) == expense_id
    assert env.store.pending_delete_target(CHAT, 2, now=NOW) is None
    assert env.store.pending_delete_target(CHAT + 1, 1, now=NOW) is None
    assert env.store.pending_delete_target(CHAT, 1, now=NOW + timedelta(minutes=16)) is None
    assert env.store.pending_delete_target(CHAT, 1, now=NOW) is None


def test_approving_an_unrelated_expense_does_not_clear_a_different_delete_target_but_its_own_does():
    env = _make([])
    delete_target_id = _seed_confirmed(env.store, 1, _ONE, author_id=1)
    other_id = _seed_pending(env.store, 2, _ONE, author_id=1, description="קפה")
    env.store.begin_pending_delete(CHAT, 1, delete_target_id, now=NOW)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{other_id}:yes")
    assert out.text == "✓ אושר"
    assert env.store.pending_delete_target(CHAT, 1, now=NOW) == delete_target_id  # untouched

    # Now a change request against the SAME expense the delete-target points to gets answered --
    # this should clear the stored delete-target too, same rule as the edit-target one.
    request_id = _seed_change_request(env.store, 3, expense_id=delete_target_id, requested_by=1, required_approvers=[1])
    env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"change_request:{request_id}:yes")
    assert env.store.pending_delete_target(CHAT, 1, now=NOW) is None


# === handle_callback clears a pending edit-target ONLY when it answers that SAME expense ==================


def test_approving_an_unrelated_expense_does_not_clear_a_different_edit_target():
    env = _make([])
    edit_target_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    other_id = _seed_pending(env.store, 2, _ONE, author_id=1, description="קפה")
    env.store.begin_pending_edit(CHAT, 1, edit_target_id, now=NOW)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{other_id}:yes")

    assert out.text == "✓ אושר"
    assert env.store.get_expense(other_id).state == ExpenseState.confirmed
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) == edit_target_id  # untouched


def test_approving_an_unrelated_change_request_does_not_clear_a_different_edit_target():
    env = _make([])
    edit_target_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    other_expense_id = _seed_confirmed(env.store, 2, _ONE, author_id=1, description="קפה")
    request_id = _seed_change_request(
        env.store, 3, expense_id=other_expense_id, requested_by=1, required_approvers=[1]
    )
    env.store.begin_pending_edit(CHAT, 1, edit_target_id, now=NOW)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"change_request:{request_id}:yes")

    assert out.text == "✓ אושר"
    assert env.store.get_change_request(request_id).state == ExpenseState.confirmed
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) == edit_target_id  # untouched


def test_approving_the_expense_its_own_edit_target_points_to_still_clears_it():
    env = _make([])
    expense_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    env.store.begin_pending_edit(CHAT, 1, expense_id, now=NOW)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"expense:{expense_id}:yes")

    assert out.text == "✓ אושר"
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) is None


def test_approving_a_settlement_never_touches_a_pending_edit_target():
    env = _make([])
    edit_target_id = _seed_pending(env.store, 1, _ONE, author_id=1)
    env.store.upsert_member(CHAT, 2, "דני")
    settlement_id = env.store.create_settlement(
        Settlement(
            chat_id=CHAT, message_id=2, from_user=1, to_user=2, amount=2000,
            currency=Currency.ILS, requested_by=1, created_at=NOW,
        )
    ).id
    env.store.begin_pending_edit(CHAT, 1, edit_target_id, now=NOW)

    out = env.bot.handle_callback(chat_id=CHAT, sender_id=1, callback_data=f"settlement:{settlement_id}:yes")

    assert out.text == "✓ אושר"
    assert env.store.get_settlement(settlement_id).state == ExpenseState.confirmed
    assert env.store.pending_edit_target(CHAT, 1, now=NOW) == edit_target_id  # never touched: no expense id at all
