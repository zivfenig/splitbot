import copy
from datetime import date

import pytest
from pydantic import ValidationError

from splitbot.models import Currency, Expense, ExtractedExpense, Share, Subcategory, category_of

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
        with_change(("exact_amounts",), [{"member": {"kind": "known", "id": 2}, "amount": "50"}]),
        with_change(("refers_to",), {"value": "240", "evidence": "240", "source": "guess"}),
        with_change(("amount_in_words",), "yes"),
        with_change(("amount_in_words",), 1),
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
        "exact_amount_without_evidence",
        "refers_to_source_guess",
        "amount_in_words_string_yes",
        "amount_in_words_number_one",
    ],
)
def test_extraction_rejects_values_outside_the_closed_lists(bad_payload):
    ExtractedExpense.model_validate(valid_payload())  # the base payload itself is fine
    assert ExtractedExpense.model_validate(with_change(("amount_in_words",), True)).amount_in_words is True
    assert ExtractedExpense.model_validate(valid_payload()).amount_in_words is False  # default
    with pytest.raises(ValidationError):
        ExtractedExpense.model_validate(bad_payload)


def expense(total: int, shares: list[Share]) -> Expense:
    return Expense(
        chat_id=1,
        message_id=1,
        spent_on=date(2026, 9, 1),
        author_id=1,
        description="פיצה",
        total=total,
        currency=Currency.ILS,
        subcategory=Subcategory.restaurant,
        shares=shares,
        prompt_version="extract_v1",
    )


@pytest.mark.parametrize(
    "total, shares",
    [
        (1000, [Share(user_id=1, paid=900, owed=500), Share(user_id=2, paid=0, owed=500)]),
        (1000, [Share(user_id=1, paid=1000, owed=500), Share(user_id=2, paid=0, owed=400)]),
        (0, [Share(user_id=1, paid=0, owed=0)]),
        (-1000, [Share(user_id=1, paid=-1000, owed=-1000)]),
        (1000, []),
        (1000, [Share(user_id=1, paid=1000, owed=1500), Share(user_id=2, paid=0, owed=-500)]),
        (1000, [Share(user_id=1, paid=1500, owed=500), Share(user_id=2, paid=-500, owed=500)]),
        (1000, [Share(user_id=1, paid=1000, owed=500), Share(user_id=1, paid=0, owed=500)]),
    ],
    ids=[
        "paid_not_total",
        "owed_not_total",
        "zero_total",
        "negative_total",
        "no_shares",
        "negative_owed_share",
        "negative_paid_share",
        "same_member_twice",
    ],
)
def test_expense_rejects_shares_that_do_not_sum_to_total(total, shares):
    good = [Share(user_id=1, paid=1000, owed=500), Share(user_id=2, paid=0, owed=500)]
    expense(1000, good)  # the base expense itself is fine
    with pytest.raises(ValidationError):
        expense(total, shares)
