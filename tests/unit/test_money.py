import pytest

from splitbot.money import format_amount, parse_amount, split_expense

# --- amount text -----------------------------------------------------------


@pytest.mark.parametrize(
    "text, minor",
    [
        ("38.90", 3890),
        ("38,90", 3890),  # comma + 1-2 digits = decimal
        ("38,9", 3890),
        ("240", 24000),
        ("1,200", 120000),  # comma + exactly 3 digits = thousands
        ("1,200.50", 120050),
        # rejected: never guess an amount, the bot asks instead
        ("", None),
        ("abc", None),
        ("0", None),
        ("-5", None),
        ("1.200", None),  # dot + 3 digits is ambiguous
        ("1,2345", None),
        ("1.200,50", None),  # European style
    ],
)
def test_amount_text_becomes_minor_units_or_is_rejected(text, minor):
    if minor is None:
        with pytest.raises(ValueError):
            parse_amount(text)
        return
    assert parse_amount(text) == minor
    assert format_amount(minor) == f"{minor // 100}.{minor % 100:02d}"


# --- splitting -------------------------------------------------------------
# ids: 1 = author, 2 = Dani, 3 = Michal, 4 = Moshe. `expected` = owed per member id,
# including the payer (owed 0 if not a participant). None = must be rejected.


@pytest.mark.parametrize(
    "total, payer, author, participants, exact, expected",
    [
        # equal split
        (24001, 1, 1, [1, 2, 3], None, {1: 8001, 2: 8000, 3: 8000}),  # leftover -> payer
        (10000, 3, 1, [1, 2, 3], None, {1: 3333, 2: 3333, 3: 3334}),  # payer is not first
        (10000, 1, 1, [2, 3, 4], None, {1: 0, 2: 3334, 3: 3333, 4: 3333}),  # payer excluded
        (900, 1, 1, [2, 3, 4], None, {1: 0, 2: 300, 3: 300, 4: 300}),
        (1000, 1, 1, [1, 2, 3, 4], None, {1: 250, 2: 250, 3: 250, 4: 250}),
        (1, 1, 1, [1, 2], None, {1: 1, 2: 0}),
        (1000, 1, 1, [], None, None),  # nobody to split with
        # exact amounts
        (15000, 1, 1, [], {2: 5000, 4: 6000}, {1: 4000, 2: 5000, 4: 6000}),  # author gets 40
        (15000, 1, 1, [], {2: 9000, 4: 6000}, {1: 0, 2: 9000, 4: 6000}),
        (15000, 1, 1, [], {1: 4000, 2: 5000, 4: 6000}, {1: 4000, 2: 5000, 4: 6000}),
        (15000, 2, 1, [], {4: 6000}, {1: 9000, 2: 0, 4: 6000}),  # payer is not the author
        (15000, 1, 1, [], {2: 9000, 4: 7000}, None),  # stated more than the total
        (15000, 1, 1, [], {1: 3000, 2: 5000}, None),  # author listed, sum does not match
    ],
    ids=[
        "leftover_goes_to_payer",
        "leftover_goes_to_payer_even_if_not_first",
        "excluded_payer_owes_zero_and_first_participant_gets_leftover",
        "excluded_payer_owes_zero",
        "even_split_no_leftover",
        "one_agora_goes_to_payer",
        "nobody_to_split_with_is_rejected",
        "exact_unmentioned_author_gets_remainder",
        "exact_sum_equals_total_author_owes_zero",
        "exact_author_listed_and_sum_matches",
        "exact_remainder_goes_to_author_not_payer",
        "exact_over_total_is_rejected",
        "exact_author_listed_but_sum_mismatch_is_rejected",
    ],
)
def test_split_shares_sum_exactly_and_leftover_goes_to_payer(total, payer, author, participants, exact, expected):
    if expected is None:
        with pytest.raises(ValueError):
            split_expense(total, payer_id=payer, author_id=author, participants=participants, exact=exact)
        return
    shares = split_expense(total, payer_id=payer, author_id=author, participants=participants, exact=exact)
    assert {s.user_id: s.owed for s in shares} == expected
    assert sum(s.owed for s in shares) == total
    assert sum(s.paid for s in shares) == total
    assert {s.user_id for s in shares if s.paid} == {payer}  # only the payer paid
