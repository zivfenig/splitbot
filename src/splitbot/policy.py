"""How a new expense is approved. Pure functions: the LLM never decides approval.

The ONLY required approver of a new expense is its sender (the author); the confirmation is
always shown (posted in the group) whatever the mode. Two modes: `author` waits for the sender's
approval; `auto` (opt-in) commits after a grace window unless the sender corrects or rejects.
Strictness: author > auto. When unsure, take the strictest (`author`). There is no `all` mode
for new expenses (corrections and deletes use `relevant_approvers`).
"""

from collections.abc import Iterable

from splitbot.models import ApprovalMode, ApprovalRule, Category, Confidence, Currency, Expense, GroupConfig

_STRICTNESS = {ApprovalMode.auto: 0, ApprovalMode.author: 1}


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
    -> `author`. Otherwise the strictest matching rule, else the group default. The result is
    always `author` or `auto`.

    `amount_in_words=True` (the LLM converted a number written in words): the result is
    `author` even when the group default or a matching rule says `auto` (a converted amount
    never auto-commits)."""
    if category is None or confidence == Confidence.low or currency != Currency.ILS:
        return ApprovalMode.author
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
