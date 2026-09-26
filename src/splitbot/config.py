"""Loads settings from `.env`. Secrets are never printed or logged."""

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing {name}. Set it in .env (see .env.example).")
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


PRICES_PATH = Path(__file__).resolve().parents[2] / "config" / "prices.json"


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    in_per_mtok: Decimal
    out_per_mtok: Decimal


def load_prices(path: Path | None = None) -> dict[str, Price]:
    """Read the versioned price list (default `PRICES_PATH`, looked up at call time so tests can
    monkeypatch it). File shape: `{"version": 1, "unit": "...", "models": {"<model>": {"in":
    <number>, "out": <number>}}}`. Numbers become Decimal (never float). Returns model name ->
    Price. Raises ConfigError when the file is missing, is not valid JSON, has no "models"
    object, or a model's "in"/"out" is missing, not a number, or negative."""
    source = path if path is not None else PRICES_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"), parse_float=Decimal)
    except OSError:
        raise ConfigError(f"cannot read the price file {source.name}") from None
    except ValueError:
        raise ConfigError(f"the price file {source.name} is not valid JSON") from None
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        raise ConfigError(f"the price file {source.name} needs a \"models\" object")
    prices: dict[str, Price] = {}
    for name, entry in models.items():
        numbers = []
        for key in ("in", "out"):
            value = entry.get(key) if isinstance(entry, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0:
                raise ConfigError(f"price of {name!r}: \"{key}\" must be a non-negative number")
            numbers.append(Decimal(value))
        prices[name] = Price(in_per_mtok=numbers[0], out_per_mtok=numbers[1])
    return prices


def price_for(model: str, path: Path | None = None) -> Price | None:
    """The Price of `model` from the price list, or None when the model is not listed (its cost
    is then unknown, never guessed). Same errors as `load_prices`."""
    return load_prices(path).get(model)


def prices_fingerprint(path: Path | None = None) -> str:
    """First 12 hex characters of the SHA-256 of the price file's bytes: recorded in every eval
    result so it shows which prices were used. Missing file -> ConfigError."""
    source = path if path is not None else PRICES_PATH
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    except OSError:
        raise ConfigError(f"cannot read the price file {source.name}") from None


def pending_expiry() -> timedelta:
    """How long a pending write action (new expense, correction, delete) waits for a response
    before it expires: `PENDING_EXPIRY_HOURS` (hours, may be fractional, e.g. "0.5"), default 1.
    Read at call time. Raises ConfigError when the value is not a positive finite number."""
    return timedelta(hours=_positive_number("PENDING_EXPIRY_HOURS", "1"))


def auto_grace() -> timedelta:
    """`auto` mode's grace window: `AUTO_GRACE_SECONDS` (seconds), default 60. Read at call time.
    Raises ConfigError when the value is not a positive finite number."""
    return timedelta(seconds=_positive_number("AUTO_GRACE_SECONDS", "60"))


def _positive_number(name: str, default: str) -> float:
    raw = optional(name, default)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value
