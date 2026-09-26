"""Who must approve an expense. Pure functions: the LLM never decides approval.

Strictness: all > author > auto. When unsure, take the strictest.
"""

from collections.abc import Iterable

from splitbot.models import ApprovalMode, ApprovalRule, Category, Confidence, Currency, Expense, GroupConfig

_STRICTNESS = {ApprovalMode.auto: 0, ApprovalMode.author: 1, ApprovalMode.all: 2}


def _matches(rule: ApprovalRule, category: Category, total: int) -> bool:
    """A rule with both a category and a min_amount needs both (AND). The threshold counts."""
    if rule.category is not None and rule.category != category:
        return False
    return rule.min_amount is None or total >= rule.min_amount


def required_approval(
    config: GroupConfig,
    *,
    category: Category | None,
    total: int,
    currency: Currency,
    confidence: Confidence,
    amount_in_words: bool = False,
) -> ApprovalMode:
    """Unknown category, low confidence or a non-ILS currency (thresholds are ILS only)
    -> `all`. Otherwise the strictest matching rule, else the group default.

    `amount_in_words=True` (the LLM converted a number written in words): the result is at
    least `author`, even when the group default or a matching rule says `auto`; a stricter
    result (`all`) is never loosened. It does not by itself force `all`."""
    if category is None or confidence == Confidence.low or currency != Currency.ILS:
        return ApprovalMode.all
    matching = [r.mode for r in config.rules if _matches(r, category, total)]
    mode = max(matching, key=_STRICTNESS.__getitem__) if matching else config.default_mode
    if amount_in_words and mode == ApprovalMode.auto:
        return ApprovalMode.author  # code cannot verify the conversion: a human confirms
    return mode


def relevant_approvers(expense: Expense, added: Iterable[int] = ()) -> list[int]:
    """Who must approve a correction or delete of `expense`: everyone who paid (paid > 0) plus
    everyone with an owed share > 0, plus anyone the correction adds (`added`). Distinct user
    ids, sorted ascending."""
    involved = {s.user_id for s in expense.shares if s.paid > 0 or s.owed > 0}
    return sorted(involved | set(added))
