"""Answer grounding (pure): every number written in an answer must appear, by VALUE, in the sources.

No network, no LLM. Every expected value is written by hand from the `numbers_are_grounded` docstring.
"""

import pytest

from splitbot.agent.agent import numbers_are_grounded

_CASES = [
    # the same amount in different spellings is the same number (both directions)
    pytest.param("1,200", ["1200"], True, id="thousands-comma-equals-plain"),
    pytest.param("₪1200", ["1200"], True, id="currency-symbol-is-not-part-of-the-number"),
    pytest.param("1200.00", ["1200"], True, id="trailing-zero-decimals-equal-the-integer"),
    pytest.param("1200", ["1,200"], True, id="plain-answer-grounded-by-a-thousands-source"),
    pytest.param("1200", ["1200.00"], True, id="integer-answer-grounded-by-a-decimal-source"),
    pytest.param("1200", ['{"total": "₪1,200.00"}'], True, id="all-spellings-together"),
    # comma rules
    pytest.param("38,90", ["38.90"], True, id="decimal-comma-equals-decimal-point"),
    pytest.param("38.90", ["38,90"], True, id="decimal-point-answer-grounded-by-decimal-comma-source"),
    pytest.param("38.9", ["38.90"], True, id="decimal-equal-by-value-not-by-digits"),
    pytest.param("0,5", ["0.5"], True, id="zero-point-five-with-comma"),
    pytest.param("1,200", ["1.2"], False, id="comma-plus-three-digits-is-thousands-not-a-decimal"),
    pytest.param("1,20", ["120"], False, id="comma-plus-two-digits-is-a-decimal-not-thousands"),
    pytest.param("1,20", ["1.2"], True, id="comma-plus-two-digits-equals-the-decimal"),
    # a date is several numbers
    pytest.param("2026-09", ["2026-09-15"], True, id="year-month-grounded-by-a-full-date"),
    pytest.param("2026-09", ["9 items in 2026"], True, id="year-month-is-two-numbers-found-anywhere"),
    pytest.param("2026-09", ["the year 2026"], False, id="year-month-needs-the-month-number-too"),
    pytest.param("9", ["2026-09"], True, id="leading-zero-does-not-matter"),
    pytest.param("09", ["9"], True, id="leading-zero-in-the-answer-does-not-matter"),
    # no numbers at all
    pytest.param("אין הוצאות בחודש הזה", [], True, id="answer-without-numbers-and-no-sources"),
    pytest.param("אין הוצאות", ["500", "12"], True, id="answer-without-numbers-is-grounded"),
    # slices of longer numbers do not count
    pytest.param("40", ["240"], False, id="40-is-not-inside-240"),
    pytest.param("40", ["2400"], False, id="40-is-not-inside-2400"),
    pytest.param("40", ["1.40"], False, id="40-is-not-the-decimal-part-of-1.40"),
    pytest.param("12", ["1,200"], False, id="12-is-not-inside-1200"),
    pytest.param("40", ["240 and 40"], True, id="the-whole-number-elsewhere-in-a-source-counts"),
    # invented numbers
    pytest.param("777", ["500", "12"], False, id="invented-number-fails"),
    pytest.param("500 and 777", ["500"], False, id="one-invented-number-among-grounded-ones-fails"),
    pytest.param("5", [], False, id="number-with-no-sources-fails"),
    pytest.param("5", [""], False, id="number-with-an-empty-source-fails"),
    # every number may come from a different source
    pytest.param("100 ו-50", ["100", "x 50"], True, id="numbers-may-come-from-different-sources"),
    # what surrounds a number is not part of it
    pytest.param("שילמת 120.", ["120"], True, id="sentence-final-dot-is-not-part-of-the-number"),
    pytest.param("שילמת 120, ואז 50", ["120", "50"], True, id="comma-followed-by-a-space-is-not-part-of-the-number"),
    pytest.param("38.90", ['"net": "-38.90"'], False, id="a-different-sign-is-a-different-number"),
    pytest.param("-38.90", ['"net": "-38.90"'], True, id="matching-negative-numbers-still-ground"),
    pytest.param("50", ["100 ו-50"], True, id="hebrew-and-hyphen-prefix-is-not-a-sign"),
]


@pytest.mark.parametrize(("answer", "sources", "expected"), _CASES)
def test_numbers_are_grounded_by_value_not_spelling(answer, sources, expected):
    assert numbers_are_grounded(answer, sources) is expected
