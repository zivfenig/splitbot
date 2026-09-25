import pytest

from splitbot.models import Participants
from splitbot.validation import resolve_participants

MEMBERS = [1, 2, 3, 4]  # 1 = author, 2 = Dani, 3 = Michal, 4 = Moshe
AUTHOR = 1


def known(member_id: int) -> dict:
    return {"kind": "known", "id": member_id}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, [1, 2, 3, 4]),
        ({"exclude": [known(2)]}, [1, 3, 4]),
        ({"only": [known(3)]}, [1, 3]),
        ({"only": [known(1)]}, [1]),
        ({"only": [known(3)], "exclude": [known(2)]}, [1, 3]),
        ({"exclude": [known(1)]}, [2, 3, 4]),
    ],
    ids=[
        "neither_means_everyone",
        "exclude_means_everyone_except",
        "only_means_author_plus_listed",
        "only_author_appears_once",
        "explicit_only_list_wins_over_exclude",
        "excluding_the_author_removes_the_author",
    ],
)
def test_participants_are_built_by_code_and_explicit_list_wins(raw, expected):
    participants = Participants.model_validate(raw)
    assert resolve_participants(participants, AUTHOR, MEMBERS) == expected
