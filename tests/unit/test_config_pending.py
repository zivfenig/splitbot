from datetime import timedelta

import pytest

from splitbot import config
from splitbot.config import ConfigError

BAD_VALUES = ["0", "-1", "abc", "nan", "inf"]


@pytest.mark.parametrize(
    "function, variable, default, good",
    [
        (config.pending_expiry, "PENDING_EXPIRY_HOURS", timedelta(hours=1), [("2", timedelta(hours=2)), ("0.5", timedelta(minutes=30))]),
        (config.auto_grace, "AUTO_GRACE_SECONDS", timedelta(seconds=60), [("30", timedelta(seconds=30))]),
    ],
    ids=["pending_expiry", "auto_grace"],
)
def test_pending_expiry_and_auto_grace_come_from_config_with_safe_defaults(monkeypatch, function, variable, default, good):
    monkeypatch.delenv(variable, raising=False)
    assert function() == default
    for text, expected in good:
        monkeypatch.setenv(variable, text)
        assert function() == expected
    for text in BAD_VALUES:
        monkeypatch.setenv(variable, text)
        with pytest.raises(ConfigError):
            function()
