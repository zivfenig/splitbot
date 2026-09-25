"""Pure validators for LLM output. No network, no LLM.

Order of use: check member references (real IDs, no ambiguity) -> resolve participants.
"""

from splitbot.models import KnownMember, MemberRef, Participants


def _ids(refs: list[MemberRef], member_ids: list[int]) -> set[int]:
    ids = set()
    for ref in refs:
        if not isinstance(ref, KnownMember):
            raise ValueError("ambiguous member reference: the bot must ask first")
        if ref.id not in member_ids:
            raise ValueError(f"unknown member id {ref.id}")
        ids.add(ref.id)
    return ids


def resolve_participants(participants: Participants, author_id: int, member_ids: list[int]) -> list[int]:
    """Final list of member IDs sharing the expense, in group order.

    `only` present -> author + `only` (`exclude` is ignored: the explicit list wins).
    Otherwise everyone minus `exclude`. Neither -> everyone.
    """
    if participants.only is not None:
        chosen = _ids(participants.only, member_ids) | {author_id}
        return [m for m in member_ids if m in chosen]
    excluded = _ids(participants.exclude, member_ids)
    return [m for m in member_ids if m not in excluded]
