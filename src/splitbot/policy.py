"""Who must approve an expense. Pure functions: the LLM never decides approval.

Strictness: all > author > auto. When unsure, take the strictest.
"""

from splitbot.models import ApprovalMode, ApprovalRule, Category, Confidence, Currency, GroupConfig

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
) -> ApprovalMode:
    """Unknown category, low confidence or a non-ILS currency (thresholds are ILS only)
    -> `all`. Otherwise the strictest matching rule, else the group default."""
    if category is None or confidence == Confidence.low or currency != Currency.ILS:
        return ApprovalMode.all
    matching = [r.mode for r in config.rules if _matches(r, category, total)]
    return max(matching, key=_STRICTNESS.__getitem__) if matching else config.default_mode
