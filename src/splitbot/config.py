"""Loads settings from `.env`. Secrets are never printed or logged."""

import os

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
