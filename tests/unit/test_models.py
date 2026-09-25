import copy

import pytest
from pydantic import ValidationError

from splitbot.models import ExtractedExpense, Subcategory, category_of

# Agreed mapping (CLAUDE.md "Categories"). Rules use the main category.
EXPECTED_CATEGORY = {
    "electricity": "utilities",
    "gas": "utilities",
    "water": "utilities",
    "internet": "utilities",
    "rent": "rent",
    "arnona": "arnona",
    "groceries": "groceries",
    "cleaning": "household",
    "supplies": "household",
    "restaurant": "eating_out",
    "delivery": "eating_out",
    "other": "other",
}


def test_every_subcategory_maps_to_its_agreed_main_category():
    # One dict comparison covers both "nothing unmapped" and "nothing extra".
    actual = {sub.value: category_of(sub).value for sub in Subcategory}
    assert actual == EXPECTED_CATEGORY


def valid_payload() -> dict:
    return {
        "message_type": "new",
        "confidence": "high",
        "amount": {"value": "140", "evidence": "140", "source": "message"},
        "currency": {"value": "ILS", "evidence": None, "source": "default"},
        "payer": {"value": {"kind": "known", "id": 1}, "evidence": None, "source": "default"},
        "participants": {
            "value": {"only": [{"kind": "known", "id": 3}], "exclude": []},
            "evidence": "עם מיכל",
            "source": "message",
        },
        "subcategory": "restaurant",
        "description": "פיצה",
    }


def with_change(path: tuple, value) -> dict:
    payload = copy.deepcopy(valid_payload())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return payload


@pytest.mark.parametrize(
    "bad_payload",
    [
        with_change(("currency", "value"), "GBP"),
        with_change(("subcategory",), "sushi"),
        with_change(("message_type",), "pizza"),
        with_change(("payer", "value"), {"kind": "ambiguous", "candidates": [2]}),
        with_change(("amount", "source"), "guess"),
        with_change(("payer", "value"), {"kind": "ambiguous", "candidates": [2, 2]}),
        with_change(("participants", "value", "only"), []),
        with_change(("payer", "value"), {"kind": "maybe", "id": 1}),
        with_change(("total_in_words",), "one hundred forty"),
    ],
    ids=[
        "currency_gbp",
        "unknown_subcategory",
        "unknown_message_type",
        "ambiguous_one_candidate",
        "source_guess",
        "ambiguous_duplicate_candidates",
        "only_empty_list",
        "unknown_member_ref_kind",
        "extra_key",
    ],
)
def test_extraction_rejects_values_outside_the_closed_lists(bad_payload):
    ExtractedExpense.model_validate(valid_payload())  # the base payload itself is fine
    with pytest.raises(ValidationError):
        ExtractedExpense.model_validate(bad_payload)
