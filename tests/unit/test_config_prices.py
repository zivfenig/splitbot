"""Unit tests for the versioned price file helpers in splitbot.config. No network."""

import hashlib
import re
from decimal import Decimal

import pytest

from splitbot.config import ConfigError, load_prices, price_for, prices_fingerprint

VALID = (
    '{"version": 1, "unit": "USD per 1M tokens", "models": '
    '{"cheap": {"in": 0.15, "out": 0}, "pricey": {"in": 2.5, "out": 10}}}'
)
REAL_PRICES = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0")),
    "typesafe/jev-1.13": (Decimal("0.042"), Decimal("0")),
}
BAD_FILES = {
    "missing-file": None,
    "invalid-json": "{not json",
    "no-models-object": '{"version": 1}',
    "models-is-not-an-object": '{"models": []}',
    "model-without-in": '{"models": {"m": {"out": 1}}}',
    "model-without-out": '{"models": {"m": {"in": 1}}}',
    "price-is-not-a-number": '{"models": {"m": {"in": "cheap", "out": 1}}}',
    "price-is-null": '{"models": {"m": {"in": 1, "out": null}}}',
    "price-is-negative": '{"models": {"m": {"in": -0.5, "out": 1}}}',
}


def write(tmp_path, text, name="prices.json"):
    path = tmp_path / name
    if text is not None:
        path.write_text(text)
    return path


def check_valid_file(tmp_path):
    path = write(tmp_path, VALID)
    prices = load_prices(path)
    assert set(prices) == {"cheap", "pricey"}
    cheap = prices["cheap"]
    assert isinstance(cheap.in_per_mtok, Decimal) and isinstance(cheap.out_per_mtok, Decimal)
    assert (cheap.in_per_mtok, cheap.out_per_mtok) == (Decimal("0.15"), Decimal("0"))
    assert price_for("pricey", path) == prices["pricey"]
    assert price_for("pricey", path).in_per_mtok == Decimal("2.5")
    assert price_for("not-listed", path) is None


def check_bad_file(tmp_path, name):
    path = write(tmp_path, BAD_FILES[name])
    with pytest.raises(ConfigError):
        load_prices(path)
    with pytest.raises(ConfigError):
        price_for("m", path)


def check_fingerprint(tmp_path):
    first = write(tmp_path, VALID, "a.json")
    copy = write(tmp_path, VALID, "b.json")
    changed = write(tmp_path, VALID.replace("2.5", "3.5"), "c.json")
    fingerprint = prices_fingerprint(first)
    assert re.fullmatch(r"[0-9a-f]{12}", fingerprint)
    assert fingerprint == hashlib.sha256(VALID.encode()).hexdigest()[:12]
    assert prices_fingerprint(copy) == fingerprint
    assert prices_fingerprint(changed) != fingerprint
    with pytest.raises(ConfigError):
        prices_fingerprint(tmp_path / "missing.json")


def check_real_file(tmp_path):
    prices = load_prices()
    assert {m: (p.in_per_mtok, p.out_per_mtok) for m, p in prices.items()} == REAL_PRICES
    assert all(isinstance(v, Decimal) for p in prices.values()
               for v in (p.in_per_mtok, p.out_per_mtok))
    assert price_for("gpt-4o-mini").out_per_mtok == Decimal("0.60")
    assert price_for("no-such-model") is None
    assert re.fullmatch(r"[0-9a-f]{12}", prices_fingerprint())


@pytest.mark.parametrize(
    "scenario",
    ["valid-file", *BAD_FILES, "fingerprint", "real-file"],
)
def test_prices_come_from_the_versioned_file_as_decimals_and_unknown_models_have_no_price(
        tmp_path, scenario):
    if scenario == "valid-file":
        check_valid_file(tmp_path)
    elif scenario in BAD_FILES:
        check_bad_file(tmp_path, scenario)
    elif scenario == "fingerprint":
        check_fingerprint(tmp_path)
    else:
        check_real_file(tmp_path)
