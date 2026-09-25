import pytest

from splitbot.models import ApprovalMode, ApprovalRule, Category, Confidence, Currency, GroupConfig, Member
from splitbot.policy import required_approval

AUTHOR, ALL, AUTO = ApprovalMode.author, ApprovalMode.all, ApprovalMode.auto
RENT, GROCERIES = Category.rent, Category.groceries


def group(default: ApprovalMode = AUTHOR, rules: list[ApprovalRule] | None = None) -> GroupConfig:
    return GroupConfig(
        group_id=1,
        chat_id=1,
        members=[Member(id=1, name="זיו")],
        default_mode=default,
        rules=rules or [],
    )


def rule(mode: ApprovalMode, category: Category | None = None, min_amount: int | None = None) -> ApprovalRule:
    return ApprovalRule(mode=mode, category=category, min_amount=min_amount)


@pytest.mark.parametrize(
    "default, rules, category, total, expected",
    [
        (AUTHOR, [], GROCERIES, 10000, AUTHOR),
        (AUTHOR, [rule(ALL, category=RENT)], RENT, 10000, ALL),
        (AUTHOR, [rule(ALL, category=RENT)], GROCERIES, 10000, AUTHOR),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 60000, ALL),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 40000, AUTHOR),
        (AUTHOR, [rule(ALL, min_amount=50000)], GROCERIES, 50000, ALL),  # the threshold itself counts
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], RENT, 60000, ALL),  # AND: both true
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], RENT, 40000, AUTHOR),
        (AUTHOR, [rule(ALL, category=RENT, min_amount=50000)], GROCERIES, 60000, AUTHOR),
        (AUTHOR, [rule(AUTHOR, category=GROCERIES), rule(ALL, min_amount=50000)], GROCERIES, 60000, ALL),
        (AUTHOR, [rule(ALL, min_amount=50000), rule(AUTHOR, category=GROCERIES)], GROCERIES, 60000, ALL),
        (AUTO, [], GROCERIES, 100, AUTO),  # auto only because the group opted in
        (AUTHOR, [rule(AUTO, category=GROCERIES)], GROCERIES, 100, AUTO),  # opted in by a rule
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
    ],
)
def test_approval_mode_follows_group_default_and_rules(default, rules, category, total, expected):
    mode = required_approval(
        group(default, rules), category=category, total=total, currency=Currency.ILS, confidence=Confidence.high
    )
    assert mode == expected


@pytest.mark.parametrize(
    "default, rules, category, currency, confidence, expected",
    [
        (AUTO, [], GROCERIES, Currency.ILS, Confidence.low, ALL),
        (AUTO, [], None, Currency.ILS, Confidence.high, ALL),
        (AUTO, [], GROCERIES, Currency.USD, Confidence.high, ALL),
        (AUTHOR, [], GROCERIES, Currency.EUR, Confidence.high, ALL),
        (AUTHOR, [rule(AUTO, category=GROCERIES)], GROCERIES, Currency.USD, Confidence.high, ALL),
        (AUTO, [], GROCERIES, Currency.ILS, Confidence.high, AUTO),  # control: sure and ILS is not strict
    ],
    ids=[
        "low_confidence_needs_everyone",
        "unknown_category_needs_everyone",
        "usd_needs_everyone_even_when_auto",
        "eur_needs_everyone",
        "usd_needs_everyone_even_if_a_rule_says_auto",
        "sure_ils_expense_keeps_the_auto_mode",
    ],
)
def test_unsure_or_non_ils_expense_gets_the_strictest_approval(default, rules, category, currency, confidence, expected):
    mode = required_approval(group(default, rules), category=category, total=100, currency=currency, confidence=confidence)
    assert mode == expected
