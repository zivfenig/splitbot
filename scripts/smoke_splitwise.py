"""Stage 0 smoke test: create one expense in the test group, read it back, delete it.

Run: python scripts/smoke_splitwise.py
You verify in the Splitwise app: the expense appears, then disappears.
"""

from splitbot.config import optional, require
from splitbot.splitwise.base import NewExpense, Share
from splitbot.splitwise.client import SplitwiseClient

TOTAL = 1000  # 10.00 ILS, in agorot
DESCRIPTION = "SplitBot smoke test"
KEY = "sb:smoke"


def main() -> None:
    client = SplitwiseClient(require("SPLITWISE_API_KEY"))
    group_id = client.find_group_id(optional("SPLITWISE_GROUP_NAME", "SplitBot Test"))
    me = client.get_current_user()
    members = client.get_members(group_id)
    print(f"Me: {me.name} ({me.id})")
    print(f"Group {group_id} members: {[m.name for m in members]}")
    assert me.id in {m.id for m in members}, "current user is not in the test group"

    # Equal split; leftover agorot go to the payer (the real logic lands in money.py, Stage 1).
    base, leftover = divmod(TOTAL, len(members))
    shares = [
        Share(
            user_id=m.id,
            paid=TOTAL if m.id == me.id else 0,
            owed=base + (leftover if m.id == me.id else 0),
        )
        for m in members
    ]
    created = client.add_expense(
        NewExpense(group_id=group_id, description=DESCRIPTION, total=TOTAL, shares=shares, details=KEY)
    )
    print(f"Created expense {created.id}")
    try:
        read_back = client.get_expense(created.id)
        print(f"Read back: {read_back.description!r} {read_back.total} agorot {read_back.currency_code}")
        assert read_back.total == TOTAL
        assert read_back.currency_code == "ILS"
        assert read_back.details == KEY
        assert sum(s.owed for s in read_back.shares) == TOTAL
        input("Check the Splitwise app: the expense should be there. Press Enter to delete it... ")
    finally:
        client.delete_expense(created.id)
        print(f"Deleted expense {created.id}. Check the app: it should be gone.")
    assert client.get_expense(created.id).deleted, "soft delete should set deleted_at"
    print("Read back after delete: deleted_at is set (soft delete).")


if __name__ == "__main__":
    main()
