from __future__ import annotations

import pytest

from flowpilot.config import ConfigError, load_settings

REQUIRED = {
    "GARAGEFLOW_BASE_URL": "http://localhost:5080/",
    "GARAGEFLOW_EMAIL": "advisor@garageflow.local",
    "GARAGEFLOW_PASSWORD": "secret-value",
}


@pytest.fixture
def env(monkeypatch):
    for key in ("GARAGEFLOW_TIMEOUT_SECONDS", "GARAGEFLOW_MAX_RETRIES", "SHOP_TIMEZONE", *REQUIRED):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_loads_and_trims_the_url(env):
    settings = load_settings(dotenv=False)
    assert settings.garageflow_base_url == "http://localhost:5080"
    assert settings.shop_timezone == "Asia/Kolkata"


def test_password_never_appears_in_repr(env):
    assert "secret-value" not in repr(load_settings(dotenv=False))


def test_missing_setting_fails_fast(env):
    env.delenv("GARAGEFLOW_PASSWORD")
    with pytest.raises(ConfigError, match="GARAGEFLOW_PASSWORD"):
        load_settings(dotenv=False)


@pytest.mark.parametrize(
    ("key", "value"),
    [("GARAGEFLOW_TIMEOUT_SECONDS", "abc"), ("GARAGEFLOW_MAX_RETRIES", "50"), ("SHOP_TIMEZONE", "Mars/Olympus")],
)
def test_bad_values_are_rejected(env, key, value):
    env.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings(dotenv=False)
