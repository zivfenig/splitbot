import copy

import pytest

from splitbot.models import ExtractedExpense, Participants
from splitbot.validation import check_grounding, check_members, mentions_foreign_currency, resolve_participants

MEMBERS = [1, 2, 3, 4]  # 1 = author, 2 = Dani, 3 = Michal, 4 = Moshe
AUTHOR = 1


def known(member_id: int) -> dict:
    return {"kind": "known", "id": member_id}


def ambiguous(*candidates: int) -> dict:
    return {"kind": "ambiguous", "candidates": list(candidates)}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, [1, 2, 3, 4]),
        ({"exclude": [known(2)]}, [1, 3, 4]),
        ({"only": [known(3)]}, [3]),
        ({"only": [known(1)]}, [1]),
        ({"only": [known(3)], "exclude": [known(2)]}, [3]),
        ({"exclude": [known(1)]}, [2, 3, 4]),
        # exclusions always apply, also to the `only` list
        ({"only": [known(2), known(3)], "exclude": [known(1)]}, [2, 3]),  # "the pizza of Dani and Moshe"
        ({"only": [known(3), known(4)], "exclude": [known(3)]}, [4]),
        # nothing left: the resolver returns the list, the workflow must ask
        ({"exclude": [known(1), known(2), known(3), known(4)]}, []),
        ({"only": [known(1)], "exclude": [known(1)]}, []),
        ({"only": [known(3)], "exclude": [known(3)]}, []),
    ],
    ids=[
        "neither_means_everyone",
        "exclude_means_everyone_except",
        "only_means_exactly_the_listed_members",
        "only_author_appears_once",
        "only_and_exclude_are_both_applied",
        "excluding_the_author_removes_the_author",
        "author_pays_for_others_and_owes_nothing",
        "exclusion_also_removes_someone_from_the_only_list",
        "excluding_everyone_gives_an_empty_list",
        "only_the_author_but_excluded_gives_an_empty_list",
        "only_and_exclude_the_same_person_gives_an_empty_list",
    ],
)
def test_participants_are_built_by_code_from_only_and_exclude(raw, expected):
    participants = Participants.model_validate(raw)
    assert resolve_participants(participants, AUTHOR, MEMBERS) == expected


# --- member references -------------------------------------------------------


def base_extraction() -> dict:
    return {
        "confidence": "high",
        "amount": {"value": "240", "evidence": "240", "source": "message"},
        "currency": {"value": "ILS", "evidence": None, "source": "default"},
        "payer": {"value": known(1), "evidence": None, "source": "default"},
        "participants": {
            "value": {"only": [known(3)], "exclude": []},
            "evidence": "עם מיכל",
            "source": "message",
        },
    }


def extraction(**changes) -> ExtractedExpense:
    payload = copy.deepcopy(base_extraction())
    payload.update(changes)
    return ExtractedExpense.model_validate(payload)


def participants_field(**value) -> dict:
    return {"value": value, "evidence": "עם מיכל", "source": "message"}


@pytest.mark.parametrize(
    "changes, expected_issue",
    [
        ({}, None),
        ({"payer": {"value": known(9), "evidence": None, "source": "default"}}, "unknown"),
        ({"participants": participants_field(only=[known(9)])}, "unknown"),
        ({"participants": participants_field(exclude=[known(9)])}, "unknown"),
        ({"exact_amounts": [{"member": known(9), "amount": "50", "evidence": "50"}]}, "unknown"),
        ({"payer": {"value": ambiguous(2, 3), "evidence": None, "source": "default"}}, "ambiguous"),
        ({"participants": participants_field(exclude=[ambiguous(2, 3)])}, "ambiguous"),
        ({"participants": participants_field(only=[ambiguous(2, 9)])}, "unknown"),
    ],
    ids=[
        "all_members_real_and_clear",
        "unknown_payer",
        "unknown_in_only",
        "unknown_in_exclude",
        "unknown_in_exact_amounts",
        "ambiguous_payer",
        "ambiguous_in_exclude",
        "ambiguous_candidate_outside_the_group",
    ],
)
def test_unknown_or_ambiguous_members_need_clarification(changes, expected_issue):
    issues = check_members(extraction(**changes), MEMBERS)
    if expected_issue is None:
        assert issues == []
    else:
        assert any(expected_issue in issue for issue in issues)


# --- grounding (evidence) ----------------------------------------------------

MESSAGE = "שילמתי 240 על פיצה עם מיכל"
EXACT = [
    {"member": known(2), "amount": "50", "evidence": "דני 50"},
    {"member": known(4), "amount": "60", "evidence": "משה 60"},
]


def grounded(message: str, **changes) -> list[str]:
    return check_grounding(extraction(**changes), message, author_id=AUTHOR)


def amount_field(value: str, evidence: str | None, source: str = "message") -> dict:
    return {"value": value, "evidence": evidence, "source": source}


@pytest.mark.parametrize(
    "message, changes, expected_issue",
    [
        (MESSAGE, {}, None),
        (MESSAGE, {"amount": amount_field("260", "260")}, "evidence"),  # not in the message
        (MESSAGE, {"amount": amount_field("260", "240")}, "amount"),  # differs from evidence
        ("שילמתי 1,200 על שכירות", {"amount": amount_field("1200", "1,200"), "participants": None}, None),  # comma rule
        ("שילמתי 1,200 על שכירות", {"amount": amount_field("120", "1,200"), "participants": None}, "amount"),
        ("שילמתי 240 על 3 פיצות עם מיכל", {"amount": amount_field("240", "240 על 3")}, "numeric"),
        (MESSAGE, {"amount": amount_field("240", "שילמתי")}, "numeric"),  # no number at all
        (MESSAGE, {"amount": amount_field("240", "240", source="default")}, "amount"),
        (MESSAGE, {"currency": {"value": "USD", "evidence": None, "source": "default"}}, "currency"),
            (
                "שילמתי 240$ על פיצה עם מיכל",
                {"currency": {"value": "USD", "evidence": "$", "source": "message"}},
                None,
        ),
        (MESSAGE, {"participants": participants_field(only=[known(3)]) | {"evidence": None}}, "participants"),
        (
            MESSAGE,
            {"participants": participants_field(only=[known(3)]) | {"source": "default"}},
            "participants",  # a default must mean "everyone"
        ),
        (
            MESSAGE,
            {"exact_amounts": EXACT, "participants": None},
            "exact_amounts",  # "דני 50" is not in the message
        ),
            (
                "150: דני 50, משה 60",
                {"amount": amount_field("150", "150"), "exact_amounts": EXACT, "participants": None},
                None,
        ),
        (
            "  Paid 240\nwith  MICHAL ",
            {"participants": participants_field(only=[known(3)]) | {"evidence": "with michal"}},
            None,
        ),
        (
            "שִׁלַּמְתִּי 240 עִם מִיכָל",
            {"participants": participants_field(only=[known(3)]) | {"evidence": "עם מיכל"}},
            None,
        ),
        # the quoted number must be a whole number in the message, never a slice of a longer one
        ("שילמתי 240", {"amount": amount_field("40", "40"), "participants": None}, "amount"),
        ("שילמתי 2400", {"amount": amount_field("240", "240"), "participants": None}, "amount"),
        ("שילמתי 1,200", {"amount": amount_field("1.20", "1,20"), "participants": None}, "amount"),
        (MESSAGE, {"payer": {"value": known(1), "evidence": "   ", "source": "message"}}, "payer"),
        (MESSAGE, {"payer": {"value": known(3), "evidence": None, "source": "default"}}, "payer"),
        # a default ILS must not hide a foreign currency written in the message
        ("שילמתי 240$ על פיצה עם מיכל", {}, "currency"),
        ("שילמתי 240 דולר על פיצה עם מיכל", {}, "currency"),
        ("paid 240 USD", {"participants": None}, "currency"),
        ("שילמתי 240 על אירוע עם מיכל", {}, None),  # "אירוע" (event) is not "אירו" (euro)
        # refers_to: which expense a correction/delete means
        (
            "sorry, it was 62 not 26",
            {
                "amount": amount_field("62", "62"),
                "refers_to": {"value": "26", "evidence": "26", "source": "message"},
                "participants": None,
            },
            None,
        ),
        (
            "delete the groceries from monday",
            {
                "amount": None,
                "currency": None,
                "payer": None,
                "participants": None,
                "refers_to": {
                    "value": "the groceries from monday",
                    "evidence": "the groceries from monday",
                    "source": "message",
                },
            },
            None,
        ),
        (
            "sorry, it was 62 not 26",
            {
                "amount": amount_field("62", "62"),
                "refers_to": {"value": "27", "evidence": "27", "source": "message"},
                "participants": None,
            },
            "refers_to",
        ),
        (
            "sorry, it was 62 not 26",
            {
                "amount": amount_field("62", "62"),
                "refers_to": {"value": "26", "evidence": "26", "source": "default"},
                "participants": None,
            },
            "refers_to",
        ),
        (
            "sorry, it was 62 not 26",
            {
                "amount": amount_field("62", "62"),
                "refers_to": {"value": "26", "evidence": "   ", "source": "message"},
                "participants": None,
            },
            "refers_to",
        ),
        (
            MESSAGE,
            {"refers_to": {"value": "פיצה", "evidence": "פיצה", "source": "message"}},
            None,
        ),
        (
            "sorry, it was 62 not 26",
            {
                "amount": None,
                "currency": None,
                "payer": None,
                "participants": None,
                "refers_to": {"value": "26", "evidence": "26", "source": "message"},
            },
            None,
        ),
        (
            "sorry, it was 62 not 26",
            {
                "amount": amount_field("62", "62"),
                "refers_to": None,
                "participants": None,
            },
            None,
        ),
        # amount in words: the LLM converted it, grounding only checks the evidence exists
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("150", "מאה וחמישים"), "amount_in_words": True, "participants": None},
            None,
        ),
        (
            "שילמתי 2 אלף על שכירות",
            {"amount": amount_field("2000", "2 אלף"), "amount_in_words": True, "participants": None},
            None,
        ),
        (
            "paid 1.5K for rent",
            {"amount": amount_field("1500", "1.5K"), "amount_in_words": True, "participants": None},
            None,
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("150", "מאתיים"), "amount_in_words": True, "participants": None},
            "amount",  # evidence not in the message
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("abc", "מאה וחמישים"), "amount_in_words": True, "participants": None},
            "amount",  # value is not a readable amount
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("0", "מאה וחמישים"), "amount_in_words": True, "participants": None},
            "amount",  # value must be positive
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("150", "   "), "amount_in_words": True, "participants": None},
            "amount",  # blank evidence
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {
                "amount": amount_field("150", "מאה וחמישים", source="default"),
                "amount_in_words": True,
                "participants": None,
            },
            "amount",  # an amount cannot be a default
        ),
        (
            "שילמתי מאה וחמישים על פיצה",
            {"amount": amount_field("150", "מאה וחמישים"), "amount_in_words": False, "participants": None},
            "amount",  # control: without the flag, digitless evidence is still an issue
        ),
        (
            MESSAGE,
            {"amount": amount_field("240", "240"), "amount_in_words": True},
            None,  # control: the flag alone is not an issue
        ),
    ],
    ids=[
        "fully_grounded",
        "evidence_not_in_message",
        "amount_differs_from_its_evidence",
        "thousands_comma_matches",
        "thousands_comma_mismatch",
        "evidence_with_two_numbers",
        "evidence_with_no_number",
        "amount_cannot_be_a_default",
        "non_ils_currency_without_evidence",
        "non_ils_currency_with_evidence",
        "message_source_without_evidence",
        "default_participants_must_mean_everyone",
        "exact_amount_evidence_missing_from_message",
        "exact_amounts_grounded",
        "case_and_whitespace_are_ignored",
        "niqqud_is_ignored",
        "amount_is_a_slice_of_a_longer_number",
        "amount_is_a_prefix_of_a_longer_number",
        "thousands_comma_cannot_be_cut_short",
        "whitespace_only_evidence",
        "default_payer_must_be_the_author",
        "default_ils_but_message_has_a_dollar_sign",
        "default_ils_but_message_says_dollars_in_hebrew",
        "default_ils_but_message_says_usd",
        "word_containing_euro_letters_is_not_a_currency",
        "correction_with_grounded_refers_to",
        "delete_with_grounded_refers_to",
        "refers_to_evidence_not_in_message",
        "refers_to_source_default",
        "refers_to_blank_evidence",
        "refers_to_on_a_new_expense",
        "refers_to_on_a_chat_message",
        "correction_without_refers_to_is_fine",
        "words_amount_is_accepted",
        "mixed_digits_and_words_amount_is_accepted",
        "shorthand_amount_is_accepted",
        "words_evidence_not_in_message",
        "words_value_not_a_readable_amount",
        "words_value_zero",
        "words_blank_evidence",
        "words_amount_cannot_be_a_default",
        "digitless_evidence_without_the_flag_is_an_issue",
        "flag_with_digit_evidence_is_not_an_issue",
    ],
)
def test_ungrounded_fields_need_clarification(message, changes, expected_issue):
    issues = grounded(message, **changes)
    if expected_issue is None:
        assert issues == []
    else:
        assert any(expected_issue in issue for issue in issues)


@pytest.mark.parametrize(
    "message, expected",
    [
        # symbols
        ("שילמתי 20$", True),
        ("50 €", True),
        ("£30 taxi", True),
        # codes
        ("paid 20 USD", True),
        ("50 EUR for dinner", True),
        ("40 GBP", True),
        # Hebrew words, with and without a one-letter prefix
        ("שילמתי 20 דולר", True),
        ("30 דולרים", True),
        ("שילמתי 20 בדולר", True),
        ("50 אירו", True),
        ("50 יורו", True),
        ("30 פאונד", True),
        ("30 פאונדים", True),
        # English words, any case
        ("20 dollar", True),
        ("20 Dollars", True),
        ("50 euro", True),
        ("50 EUROS", True),
        ("30 pound", True),
        ("30 Pounds", True),
        # not a foreign currency
        ("פיצה 120", False),
        ("אירוע 120", False),  # "event", not the euro
        ("אירוח 200", False),  # "hosting", not the euro
        ("", False),
    ],
)
def test_foreign_currency_words_are_recognised_in_hebrew_and_english(message, expected):
    assert mentions_foreign_currency(message) is expected
