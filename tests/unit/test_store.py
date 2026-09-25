import pytest

from splitbot.models import Currency, Expense, ExpenseState, Share, Subcategory
from splitbot.state import IllegalTransition
from splitbot.store import DuplicateMessage, StateConflict, Store


def new_expense() -> Expense:
    return Expense(
        chat_id=1,
        message_id=10,
        group_id=7,
        author_id=1,
        description="פיצה",
        total=24001,
        currency=Currency.ILS,
        subcategory=Subcategory.restaurant,
        shares=[Share(user_id=1, paid=24001, owed=8001), Share(user_id=2, paid=0, owed=8000), Share(user_id=3, paid=0, owed=8000)],
        prompt_version="extract_v1",
    )


def test_store_ignores_duplicate_messages_and_lists_submitting_expenses(monkeypatch, tmp_path):
    path = str(tmp_path / "splitbot.db")
    store = Store(path)

    # Idempotency: the same (chat, message) is processed once.
    assert store.is_processed(1, 10) is False
    assert store.mark_processed(1, 10) is True
    assert store.mark_processed(1, 10) is False
    assert store.is_processed(1, 10) is True
    assert store.mark_processed(1, 11) is True
    assert store.mark_processed(2, 10) is True  # same message id in another chat is a different message

    # An expense survives a round trip, with its prompt version and exact shares.
    saved = store.save_expense(new_expense())
    assert saved.id is not None
    assert store.get_expense(saved.id) == saved
    assert saved.prompt_version == "extract_v1"

    # One expense per message, and saving marks the message processed in the same transaction.
    with pytest.raises(DuplicateMessage):
        store.save_expense(new_expense())
    other = new_expense().model_copy(update={"message_id": 50})
    store.save_expense(other)
    assert store.is_processed(1, 50) is True

    # Everything survives a restart.
    reopened = Store(path)
    assert reopened.get_expense(saved.id) == saved
    assert reopened.mark_processed(1, 10) is False

    # The outbox is a query over expenses that still have to reach Splitwise.
    assert store.outbox() == []
    store.set_state(saved.id, ExpenseState.approved)
    assert [e.id for e in store.outbox()] == [saved.id]

    # Stuck in `submitting` (crash) -> listed, so the workflow searches Splitwise for our key first.
    assert store.find_submitting() == []
    store.set_state(saved.id, ExpenseState.submitting)
    assert [e.id for e in store.find_submitting()] == [saved.id]

    # State changes go through the state machine.
    with pytest.raises(IllegalTransition):
        store.set_state(saved.id, ExpenseState.pending_approval)

    # Another writer moved it on while we hold a stale copy: conflict, never a silent overwrite.
    stale = store.get_expense(saved.id)
    store.set_state(saved.id, ExpenseState.failed)
    monkeypatch.setattr(store, "get_expense", lambda _id: stale)
    with pytest.raises(StateConflict):
        store.set_state(saved.id, ExpenseState.submitted, splitwise_id=1)
    monkeypatch.undo()

    # Retry after a failure is allowed; `submitted` needs the Splitwise id.
    store.set_state(saved.id, ExpenseState.submitting)
    with pytest.raises(ValueError):
        store.set_state(saved.id, ExpenseState.submitted)
    done = store.set_state(saved.id, ExpenseState.submitted, splitwise_id=4705569717)
    assert done.splitwise_id == 4705569717
    assert store.get_expense(saved.id).state == ExpenseState.submitted
    assert store.find_submitting() == [] and store.outbox() == []
