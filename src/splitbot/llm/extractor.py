"""message -> ExtractedExpense. The prompt text lives in prompts/, never in this file."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from splitbot.llm.client import LLMClient, LLMResult
from splitbot.models import Ambiguous, Evidenced, ExtractedExpense, KnownMember, Member, Participants, PersonAmount
from splitbot.validation import check_grounding, check_members

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_VERSION_NAME = re.compile(r"[a-z][a-z0-9_]*_v[0-9]+")
_MAX_VERSION_LEN = 60
_MAX_ERROR_LEN = 500
_MAX_ERRORS = 10
# The only names allowed to appear in a retry error. Any other key name from the reply is
# model-chosen text and is shown as "?".
_KNOWN_NAMES = (
    set(ExtractedExpense.model_fields)
    | set(Evidenced.model_fields)
    | set(Participants.model_fields)
    | set(PersonAmount.model_fields)
    | set(KnownMember.model_fields)
    | set(Ambiguous.model_fields)
    | {"known", "ambiguous"}  # discriminator branch names
)


@dataclass(frozen=True)
class Extraction:
    status: Literal["ok", "needs_clarification"]
    expense: ExtractedExpense | None  # parsed model; kept even when validators found issues
    issues: list[str]  # parse errors + check_members + check_grounding issues
    prompt_version: str
    llm_calls: list[LLMResult]  # 1 call, or 2 after a retry (cost/latency for the evals)


def load_prompt(version: str) -> str:
    """Text of `prompts/<version>.md` (the repo's prompts folder), e.g. "extract_v1".

    Raises FileNotFoundError when that prompt version does not exist, and ValueError when
    `version` is not a plain name like "extract_v1" (no path separators or dots, at most
    60 characters).
    """
    if len(version) > _MAX_VERSION_LEN or not _VERSION_NAME.fullmatch(version):
        raise ValueError(f"not a prompt version name: {version!r}")
    return (_PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def extract(
    message: str,
    *,
    sender_id: int,
    members: list[Member],
    llm: LLMClient,
    prompt_version: str = "extract_v1",
) -> Extraction:
    """Turn one chat message into an Extraction.

    Call 1: `llm.complete(system=load_prompt(prompt_version), user=<json>)`, where <json> is
    `json.dumps({"members": [{"id": ..., "name": ...}, ...], "sender_id": sender_id,
    "message": message}, ensure_ascii=False)`. The message is only ever a JSON string value
    in the user turn, never part of the system prompt.

    The reply must be JSON that validates as `ExtractedExpense`. If it is not valid JSON, or
    fails validation, there is exactly ONE retry: call 2 uses the same system prompt and the
    same user JSON plus an extra key `"previous_reply_error"`: a non-empty string built ONLY
    from schema error types and known field names (any other key name found in the reply is
    shown as "?"), at most 10 errors and at most 500 characters. It never contains text the
    model or the message chose (extra key names, bad tags, values). Never more than 2 calls.
    A reply that is not valid JSON, is nested so deeply that parsing hits the recursion
    limit, or is not an object matching the schema, counts as invalid.
      * valid on call 1 or 2 -> validators run (below).
      * invalid after call 2 -> Extraction(status="needs_clarification", expense=None,
        issues=[...non-empty...], llm_calls=[both calls]).

    Validators (no retry for these, a retry cannot fix a hallucination): with a valid
    expense, `check_members(expense, [member ids])` and
    `check_grounding(expense, message, author_id=sender_id)` are run. Any issue ->
    status="needs_clarification", the parsed `expense` is still returned, `issues` lists them.
    No issue -> status="ok" and `issues == []`.

    `prompt_version` is recorded on the result. `LLMError` from `llm.complete` is NOT caught:
    an outage propagates to the caller (it is not a clarification).
    """
    system = load_prompt(prompt_version)
    context = {
        "members": [{"id": m.id, "name": m.name} for m in members],
        "sender_id": sender_id,
        "message": message,
    }
    calls: list[LLMResult] = []
    problems: list[str] = []
    for _attempt in range(2):
        payload = dict(context)
        if problems:
            payload["previous_reply_error"] = problems[-1]
        result = llm.complete(system, json.dumps(payload, ensure_ascii=False))
        calls.append(result)
        try:
            expense = _parse_reply(result.text)
        except _BadReply as exc:
            problems.append(str(exc))
            continue
        # A retry cannot fix a hallucination, so these checks never trigger one.
        issues = check_members(expense, [m.id for m in members])
        issues += check_grounding(expense, message, author_id=sender_id)
        return Extraction(
            status="needs_clarification" if issues else "ok",
            expense=expense,
            issues=issues,
            prompt_version=prompt_version,
            llm_calls=calls,
        )
    return Extraction(
        status="needs_clarification",
        expense=None,
        issues=problems,
        prompt_version=prompt_version,
        llm_calls=calls,
    )


class _BadReply(ValueError):
    """The reply is not usable; the text says where, never what (no offending values)."""


def _parse_reply(text: str) -> ExtractedExpense:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        raise _BadReply("the reply is not valid JSON") from None
    try:
        return ExtractedExpense.model_validate(data)
    except ValidationError as exc:
        raise _BadReply(_error_summary(exc)) from None
    except RecursionError:
        raise _BadReply("the reply is nested too deeply") from None


def _error_summary(exc: ValidationError) -> str:
    """Error TYPES and known field names only: never a key, tag or value chosen by the model
    (that text would be fed back into the next prompt). Capped in count and length."""
    parts = []
    for error in exc.errors()[:_MAX_ERRORS]:
        where = ".".join(str(p) if isinstance(p, int) or p in _KNOWN_NAMES else "?" for p in error["loc"])
        parts.append(f"{where or '(top level)'}: {error['type']}")
    return ("the reply does not match the schema: " + "; ".join(parts))[:_MAX_ERROR_LEN]
