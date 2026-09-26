"""EmbeddingRouter with a fake OpenAI-shaped client. Offline: hand-made 3-d vectors, one axis per
class (x = expense, y = query, z = ignore), so every cosine similarity is known by hand."""

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import openai
import pytest

from splitbot.config import ConfigError
from splitbot.router.base import MAX_MESSAGE_CHARS, RouteResult
from splitbot.router.embedding_router import EmbeddingRouter, load_references

MODEL = "embed-test"
PRICE = Decimal("0.02")  # USD per 1M tokens
TOKENS_PER_TEXT = 10

# 4 expense references (top_k matters), 2 query references (fewer than 3), 3 ignore references.
# Listed in mixed order on purpose, so the label of each reference must be honored.
REFERENCES = [
    ("paid 100 for dinner", "expense"),  # vs [1,0,0]: cos 1.0
    ("who owes what?", "query"),  # cos 0.0
    ("lol", "ignore"),  # cos 0.0
    ("I covered the taxi", "expense"),  # [3,4,0]: cos 0.6
    ("what is the balance", "query"),  # [1,1,0]: cos 0.7071
    ("good morning", "ignore"),  # cos 0.0
    ("expense sideways", "expense"),  # [0,1,0]: cos 0.0
    ("haha", "ignore"),  # cos 0.0
    ("reverse expense", "expense"),  # [-1,0,0]: cos -1.0 (the one top_k=3 leaves out)
]
VECTORS = {
    "paid 100 for dinner": [1, 0, 0],
    "I covered the taxi": [3, 4, 0],
    "expense sideways": [0, 1, 0],
    "reverse expense": [-1, 0, 0],
    "who owes what?": [0, 1, 0],
    "what is the balance": [1, 1, 0],
    "lol": [0, 0, 1],
    "good morning": [0, 0, 2],
    "haha": [0, 0, 3],
    # routed messages
    "dinner was 100": [1, 0, 0],  # expense class: mean(1.0, 0.6, 0.0) = 0.5333; query: mean(0, 0.7071) = 0.3536; ignore: 0
    "ok great": [0, 0, 1],  # expense 0, query 0, ignore 1.0
    "secret-lunch-42": [1, 0, 0],
}
REQUEST = httpx.Request("POST", "https://x.invalid")


class FakeEmbeddings:
    def __init__(self, errors=None, malformed=()):
        self.calls: list[dict] = []
        self.errors = errors or {}  # call number (0 = setup) -> exception to raise
        self.malformed = set(malformed)  # call numbers that answer without `.data`

    def create(self, *, model, input):
        number = len(self.calls)
        self.calls.append({"model": model, "input": list(input)})
        if number in self.errors:
            raise self.errors[number]
        usage = SimpleNamespace(prompt_tokens=TOKENS_PER_TEXT * len(input))
        if number in self.malformed:
            return SimpleNamespace(usage=usage)
        return SimpleNamespace(data=[SimpleNamespace(embedding=VECTORS[t]) for t in input], usage=usage)


class FakeClient:
    def __init__(self, **kwargs):
        self.embeddings = FakeEmbeddings(**kwargs)

    @property
    def calls(self):
        return self.embeddings.calls


def _router(client, price=PRICE, **kwargs) -> EmbeddingRouter:
    return EmbeddingRouter(client=client, model=MODEL, references=REFERENCES, price_in_per_mtok=price, **kwargs)


def _assert_error_result(result: RouteResult, message: str):
    assert isinstance(result, RouteResult)
    assert result.error  # a non-empty reason
    assert result.label == "expense"
    assert result.scores == {"expense": 1.0, "query": 0.0, "ignore": 0.0}
    assert result.cost_usd is None
    assert result.latency_s >= 0
    if message.strip():
        assert message not in result.error  # a fixed reason, never the user's text


# ---------------------------------------------------------------- scoring


def _check_scores_follow_similarity_to_labeled_references(tmp_path, monkeypatch):
    router = _router(FakeClient())
    result = router.route("dinner was 100")
    assert result.error is None
    assert set(result.scores) == {"expense", "query", "ignore"}
    assert all(0.0 <= s <= 1.0 for s in result.scores.values())
    assert sum(result.scores.values()) == pytest.approx(1.0, abs=1e-6)
    # class scores 0.5333 / 0.3536 / 0.0, softmax at temperature 0.05 -> about 0.973 / 0.027 / 0.0
    assert result.label == "expense"
    assert result.scores["expense"] == pytest.approx(0.9733, abs=2e-3)
    assert result.scores["query"] == pytest.approx(0.0267, abs=2e-3)
    assert result.scores["ignore"] == pytest.approx(0.0, abs=1e-4)
    assert result.scores["expense"] > result.scores["query"] > result.scores["ignore"]


def _check_label_is_the_argmax_for_another_class(tmp_path, monkeypatch):
    result = _router(FakeClient()).route("ok great")
    assert result.label == "ignore"
    assert result.scores["ignore"] == pytest.approx(1.0, abs=1e-6)  # ignore 1.0 vs 0.0 / 0.0
    assert result.scores["expense"] == pytest.approx(0.0, abs=1e-6)
    assert result.scores["query"] == pytest.approx(0.0, abs=1e-6)


def _check_an_exact_match_is_sharper_than_a_partial_match(tmp_path, monkeypatch):
    router = _router(FakeClient())
    partial = router.route("dinner was 100")  # winner's class score 0.5333 vs 0.3536
    exact = router.route("ok great")  # winner's class score 1.0 vs 0.0
    assert exact.scores["ignore"] > partial.scores["expense"]
    assert exact.scores["ignore"] > 0.999


def _check_only_the_top_3_similarities_of_a_class_count(tmp_path, monkeypatch):
    # expense has 4 references (cos 1.0, 0.6, 0.0, -1.0): top-3 mean 0.5333 makes expense win.
    # A mean over all 4 would be 0.15 and let "query" (0.3536) win; top-1 would give 1.0.
    result = _router(FakeClient()).route("dinner was 100")
    assert result.label == "expense"
    assert result.scores["query"] < 0.05


def _check_a_class_with_fewer_than_3_references_averages_the_ones_it_has(tmp_path, monkeypatch):
    # query has 2 references (cos 0.0 and 0.7071): mean 0.3536, not 0.2357 (dividing by 3).
    # The gap to expense (0.5333) is then 3.6 logits, so query keeps about 2.7 percent.
    result = _router(FakeClient()).route("dinner was 100")
    assert result.scores["query"] == pytest.approx(0.0267, abs=2e-3)
    assert result.scores["query"] > 0.02  # dividing by 3 would give about 0.003


# ---------------------------------------------------------------- calls, setup, cost, latency


def _check_references_are_embedded_once_in_one_call_and_reused(tmp_path, monkeypatch):
    client = FakeClient()
    router = _router(client)
    router.prepare()
    router.prepare()  # idempotent
    assert len(client.calls) == 1
    for message in ("dinner was 100", "ok great", "dinner was 100"):
        router.route(message)
    assert len(client.calls) == 1 + 3
    setup_call = client.calls[0]
    assert setup_call["model"] == MODEL
    assert setup_call["input"] == [text for text, _ in REFERENCES]  # all references, file order
    for call, message in zip(client.calls[1:], ("dinner was 100", "ok great", "dinner was 100")):
        assert call == {"model": MODEL, "input": [message]}
    router.prepare()  # still no new setup call after routing
    assert len(client.calls) == 4


def _check_the_first_route_runs_setup_automatically(tmp_path, monkeypatch):
    client = FakeClient()
    router = _router(client)
    router.route("dinner was 100")
    assert len(client.calls) == 2  # 1 setup + 1 message
    assert client.calls[0]["input"] == [text for text, _ in REFERENCES]


def _check_setup_figures_are_kept_apart_from_the_message_figures(tmp_path, monkeypatch):
    router = _router(FakeClient())
    result = router.route("dinner was 100")
    assert router.setup["tokens"] == 9 * TOKENS_PER_TEXT
    assert router.setup["latency_s"] >= 0
    assert isinstance(router.setup["cost_usd"], Decimal)
    assert router.setup["cost_usd"] == Decimal("0.0000018")  # 90 tokens * 0.02 / 1e6
    # the message's own cost does not include the setup tokens
    assert result.cost_usd == Decimal("0.0000002")  # 10 tokens * 0.02 / 1e6


def _check_cost_is_prompt_tokens_times_price_per_million_as_decimal(tmp_path, monkeypatch):
    result = _router(FakeClient()).route("dinner was 100")
    assert isinstance(result.cost_usd, Decimal)
    assert result.cost_usd == Decimal(TOKENS_PER_TEXT) * PRICE / 1_000_000
    assert result.latency_s >= 0


def _check_without_a_price_the_cost_is_unknown(tmp_path, monkeypatch):
    router = _router(FakeClient(), price=None)
    result = router.route("dinner was 100")
    assert result.error is None
    assert result.cost_usd is None
    assert router.setup["cost_usd"] is None
    assert router.setup["tokens"] == 9 * TOKENS_PER_TEXT
    assert result.latency_s >= 0


# ---------------------------------------------------------------- failures never raise, always pass


def _check_an_openai_error_becomes_an_error_result(tmp_path, monkeypatch):
    message = "secret-lunch-42"
    for error in (openai.APITimeoutError(request=REQUEST), openai.APIConnectionError(request=REQUEST)):
        client = FakeClient(errors={1: error})  # setup (call 0) works, the message call fails
        _assert_error_result(_router(client).route(message), message)


def _check_a_malformed_response_becomes_an_error_result(tmp_path, monkeypatch):
    message = "secret-lunch-42"
    client = FakeClient(malformed={1})  # the message answer has no `.data`
    _assert_error_result(_router(client).route(message), message)


def _check_a_failing_setup_call_becomes_an_error_result(tmp_path, monkeypatch):
    message = "secret-lunch-42"
    client = FakeClient(errors={0: openai.APITimeoutError(request=REQUEST)})
    _assert_error_result(_router(client).route(message), message)


def _check_empty_blank_and_too_long_messages_are_rejected_without_a_call(tmp_path, monkeypatch):
    for message in ("", "   \n\t ", "x" * (MAX_MESSAGE_CHARS + 1)):
        client = FakeClient()
        router = _router(client)
        router.prepare()
        assert len(client.calls) == 1  # only the setup call
        _assert_error_result(router.route(message), message)
        assert len(client.calls) == 1  # nothing was sent for the message


# ---------------------------------------------------------------- load_references


def _write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _check_load_references_returns_message_label_pairs_in_file_order(tmp_path, monkeypatch):
    path = tmp_path / "refs.jsonl"
    _write_rows(
        path,
        [
            {"message": "b second", "label": "query", "verified": True},
            {"message": "a first", "label": "expense", "verified": True},
            {"message": "c third", "label": "ignore", "verified": True},
        ],
    )
    assert load_references(path) == [("b second", "query"), ("a first", "expense"), ("c third", "ignore")]


def _check_load_references_rejects_an_unverified_row(tmp_path, monkeypatch):
    path = tmp_path / "refs.jsonl"
    _write_rows(
        path,
        [
            {"message": "ok", "label": "expense", "verified": True},
            {"message": "not checked by the human", "label": "query", "verified": False},
        ],
    )
    with pytest.raises(ValueError):
        load_references(path)


def _check_load_references_rejects_an_unknown_label(tmp_path, monkeypatch):
    path = tmp_path / "refs.jsonl"
    _write_rows(path, [{"message": "hello", "label": "spam", "verified": True}])
    with pytest.raises(ValueError):
        load_references(path)


def _check_load_references_rejects_an_empty_file(tmp_path, monkeypatch):
    path = tmp_path / "refs.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_references(path)


# ---------------------------------------------------------------- from_env


def _valid_files(tmp_path):
    refs = tmp_path / "refs.jsonl"
    _write_rows(refs, [{"message": "paid 100", "label": "expense", "verified": True}])
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"version": 1, "unit": "usd per 1M tokens", "models": {MODEL: {"in": 0.02, "out": 0}}}))
    return refs, prices


def _check_from_env_needs_the_embedding_model(tmp_path, monkeypatch):
    refs, prices = _valid_files(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_EMBEDDING_MODEL", raising=False)
    with pytest.raises(ConfigError):
        EmbeddingRouter.from_env(references_path=refs, prices_path=prices)


def _check_from_env_needs_the_api_key(tmp_path, monkeypatch):
    refs, prices = _valid_files(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", MODEL)
    with pytest.raises(ConfigError):
        EmbeddingRouter.from_env(references_path=refs, prices_path=prices)


SCENARIOS = [
    pytest.param(_check_scores_follow_similarity_to_labeled_references, id="scores-follow-cosine-similarity-to-labeled-references"),
    pytest.param(_check_label_is_the_argmax_for_another_class, id="label-is-the-argmax-of-the-scores"),
    pytest.param(_check_an_exact_match_is_sharper_than_a_partial_match, id="exact-match-gives-sharper-probabilities"),
    pytest.param(_check_only_the_top_3_similarities_of_a_class_count, id="class-score-uses-only-the-top-3-similarities"),
    pytest.param(_check_a_class_with_fewer_than_3_references_averages_the_ones_it_has, id="class-with-2-references-averages-its-2"),
    pytest.param(_check_references_are_embedded_once_in_one_call_and_reused, id="references-embedded-once-and-reused"),
    pytest.param(_check_the_first_route_runs_setup_automatically, id="first-route-runs-setup-automatically"),
    pytest.param(_check_setup_figures_are_kept_apart_from_the_message_figures, id="setup-tokens-latency-cost-are-separate"),
    pytest.param(_check_cost_is_prompt_tokens_times_price_per_million_as_decimal, id="cost-is-exact-decimal-and-latency-not-negative"),
    pytest.param(_check_without_a_price_the_cost_is_unknown, id="no-price-means-cost-none"),
    pytest.param(_check_an_openai_error_becomes_an_error_result, id="openai-error-gives-error-result"),
    pytest.param(_check_a_malformed_response_becomes_an_error_result, id="malformed-response-gives-error-result"),
    pytest.param(_check_a_failing_setup_call_becomes_an_error_result, id="failing-setup-gives-error-result"),
    pytest.param(_check_empty_blank_and_too_long_messages_are_rejected_without_a_call, id="empty-blank-too-long-give-error-result-no-call"),
    pytest.param(_check_load_references_returns_message_label_pairs_in_file_order, id="load_references-keeps-file-order"),
    pytest.param(_check_load_references_rejects_an_unverified_row, id="load_references-rejects-unverified-row"),
    pytest.param(_check_load_references_rejects_an_unknown_label, id="load_references-rejects-unknown-label"),
    pytest.param(_check_load_references_rejects_an_empty_file, id="load_references-rejects-empty-file"),
    pytest.param(_check_from_env_needs_the_embedding_model, id="from_env-missing-embedding-model"),
    pytest.param(_check_from_env_needs_the_api_key, id="from_env-missing-api-key"),
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_embedding_router_scores_classes_by_similarity_to_labeled_references_and_reports_cost_and_latency(
    scenario, tmp_path, monkeypatch
):
    scenario(tmp_path, monkeypatch)
