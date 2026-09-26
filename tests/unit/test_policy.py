import pytest

from splitbot.models import ApprovalMode, ApprovalRule, Category, Confidence, Currency, GroupConfig, Member
from splitbot.policy import required_approval

AUTHOR, ALL, AUTO = ApprovalMode.author, ApprovalMode.all, ApprovalMode.auto
RENT, GROCERIES = Category.rent, Category.groceries


def group(default: ApprovalMode = AUTHOR, rules: list[ApprovalRule] | None = None) -> GroupConfig:
    return GroupConfig(
        chat_id=1,
        members=[Member(id=1, name="זיו")],
        default_mode=default,
        rules=rules or [],
    )


def rule(mode: ApprovalMode, category: Category | None = None, min_amount: int | None = None) -> ApprovalRule:
    return ApprovalRule(mode=mode, category=category, min_amount=min_amount)


@pytest.mark.parametrize(
    "default, rules, category, total, expected, amount_in_words",
    [
        (AUTHOR, [], GROCERIES, 10000, AUTHOR, False),
        (AUTHOR, [rule(ALL, category=RENT)], RENT, 10000, ALL, False),
        (AUTHOR, [rule(ALL, category=RENT)], GROCERIES, 10000, AUTHOR, False),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 60000, ALL, False),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 40000, AUTHOR, False),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 50000, ALL, False),  # the threshold itself counts
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], RENT, 60000, ALL, False),  # AND: both true
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], RENT, 40000, AUTHOR, False),
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], GROCERIES, 60000, AUTHOR, False),
        (AUTHOR, [rule(AUTHOR, category=GROCERIES), rule(ALL, min_amount=50000)], GROCERIES, 60000, ALL, False),
        (AUTHOR, [rule(ALL, min_amount=50000), rule(AUTHOR, category=GROCERIES)], GROCERIES, 60000, ALL, False),
        (AUTO, [], GROCERIES, 100, AUTO, False),  # auto only because the group opted in
        (AUTHOR, [rule(AUTO, category=GROCERIES)], GROCERIES, 100, AUTO, False),  # opted in by a rule
        # the amount was converted from words: at least author confirmation, a stricter mode is kept
        (AUTO, [], GROCERIES, 100, AUTHOR, True),
        (AUTHOR, [], GROCERIES, 100, AUTHOR, True),
        (AUTHOR, [rule(ALL, category=RENT)], RENT, 100, ALL, True),
        (AUTHOR, [rule(AUTO, category=GROCERIES)], GROCERIES, 100, AUTHOR, True),
    ],
    ids=[
        "default_is_author_confirmation",
        "category_rule_applies",
        "category_rule_ignores_other_categories",
        "amount_rule_applies_over_threshold",
        "amount_rule_ignores_small_amounts",
        "amount_rule_includes_the_threshold",
        "category_and_amount_rule_needs_both",
        "category_and_amount_rule_fails_when_amount_is_low",
        "category_and_amount_rule_fails_when_category_differs",
        "strictest_matching_rule_wins",
        "strictest_matching_rule_wins_in_any_order",
        "auto_when_group_default_is_auto",
        "auto_when_a_rule_opts_in",
        "converted_amount_raises_auto_default_to_author",
        "converted_amount_keeps_author",
        "converted_amount_does_not_loosen_an_all_rule",
        "converted_amount_raises_an_auto_rule_to_author",
    ],
)
def test_approval_mode_follows_group_default_and_rules(default, rules, category, total, expected, amount_in_words):
    mode = required_approval(
        group(default, rules),
        category=category,
        total=total,
        currency=Currency.ILS,
        confidence=Confidence.high,
        amount_in_words=amount_in_words,
    )
    assert mode == expected


@pytest.mark.parametrize(
    "default, rules, category, currency, confidence, expected, amount_in_words",
    [
        (AUTO, [], GROCERIES, Currency.ILS, Confidence.low, ALL, False),
        (AUTO, [], None, Currency.ILS, Confidence.high, ALL, False),
        (AUTO, [], GROCERIES, Currency.USD, Confidence.high, ALL, False),
        (AUTHOR, [], GROCERIES, Currency.EUR, Confidence.high, ALL, False),
        (AUTHOR, [rule(AUTO, category=GROCERIES)], GROCERIES, Currency.USD, Confidence.high, ALL, False),
        (AUTO, [], GROCERIES, Currency.ILS, Confidence.high, AUTO, False),  # control: sure and ILS is not strict
        (AUTHOR, [], GROCERIES, Currency.ILS, Confidence.low, ALL, True),
        (AUTHOR, [], GROCERIES, Currency.USD, Confidence.high, ALL, True),
    ],
    ids=[
        "low_confidence_needs_everyone",
        "unknown_category_needs_everyone",
        "usd_needs_everyone_even_when_auto",
        "eur_needs_everyone",
        "usd_needs_everyone_even_if_a_rule_says_auto",
        "sure_ils_expense_keeps_the_auto_mode",
        "converted_amount_with_low_confidence_needs_everyone",
        "converted_amount_in_usd_needs_everyone",
    ],
)
def test_unsure_or_non_ils_expense_gets_the_strictest_approval(
    default, rules, category, currency, confidence, expected, amount_in_words
):
    mode = required_approval(
        group(default, rules),
        category=category,
        total=100,
        currency=currency,
        confidence=confidence,
        amount_in_words=amount_in_words,
    )
    assert mode == expected


from datetime import date  # noqa: E402

from splitbot.models import Expense, Share, Subcategory  # noqa: E402
from splitbot.policy import relevant_approvers  # noqa: E402


def expense_with(shares: list[tuple[int, int, int]]) -> Expense:
    """shares: (user_id, paid, owed); paid and owed each sum to the total."""
    return Expense(
        chat_id=1,
        message_id=1,
        author_id=1,
        description="x",
        total=sum(paid for _, paid, _ in shares),
        currency=Currency.ILS,
        subcategory=Subcategory.other,
        shares=[Share(user_id=u, paid=p, owed=o) for u, p, o in shares],
        prompt_version="extract_v1",
        spent_on=date(2026, 1, 1),
    )


@pytest.mark.parametrize(
    "shares, added, expected",
    [
        ([(1, 9000, 3000), (2, 0, 3000), (3, 0, 3000)], [], [1, 2, 3]),  # payer + everyone who owes
        ([(1, 6000, 0), (2, 0, 3000), (3, 0, 3000)], [], [1, 2, 3]),  # a payer who owes 0 still counts
        ([(1, 6000, 3000), (2, 0, 3000), (3, 0, 0)], [], [1, 2]),  # paid 0 and owes 0: not involved
        ([(1, 6000, 3000), (2, 0, 3000), (3, 0, 0)], [4], [1, 2, 4]),  # a correction adds a new person
        ([(1, 6000, 3000), (2, 0, 3000), (3, 0, 0)], [3], [1, 2, 3]),  # added user with no share
        ([(1, 6000, 3000), (2, 0, 3000)], [2, 2, 4, 4], [1, 2, 4]),  # duplicates collapse
        ([(5, 4000, 0), (2, 0, 4000)], [7, 3], [2, 3, 5, 7]),  # sorted, whatever the input order
    ],
)
def test_relevant_approvers_are_the_payer_everyone_who_owes_and_anyone_added(shares, added, expected):
    assert relevant_approvers(expense_with(shares), added) == expected
