"""Settings, read once at start-up from environment variables (and a .env file in development).

C# comparison: this is FlowPilot's `appsettings.json` + `IOptions<T>`. A frozen dataclass is an
immutable record, so nothing can change a setting halfway through a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import find_dotenv, load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing or invalid. Raised at start-up, never halfway through a request."""


@dataclass(frozen=True, slots=True)
class Settings:
    garageflow_base_url: str
    garageflow_email: str
    garageflow_password: str
    timeout_seconds: float = 10.0
    max_retries: int = 3
    shop_timezone: str = "Asia/Kolkata"

    def __repr__(self) -> str:  # never print the password into a log
        return (
            f"Settings(garageflow_base_url={self.garageflow_base_url!r}, "
            f"garageflow_email={self.garageflow_email!r}, garageflow_password='***', "
            f"timeout_seconds={self.timeout_seconds}, max_retries={self.max_retries}, "
            f"shop_timezone={self.shop_timezone!r})"
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_settings(*, dotenv: bool = True) -> Settings:
    """Fail fast: a missing setting stops the program at start-up with a clear message."""
    if dotenv:
        # Look for .env from the current folder upwards. Missing .env is fine; real environment variables win.
        load_dotenv(find_dotenv(usecwd=True))

    try:
        timeout = float(os.environ.get("GARAGEFLOW_TIMEOUT_SECONDS", "10"))
        retries = int(os.environ.get("GARAGEFLOW_MAX_RETRIES", "3"))
    except ValueError as exc:
        raise ConfigError(f"GARAGEFLOW_TIMEOUT_SECONDS / GARAGEFLOW_MAX_RETRIES must be numbers: {exc}") from exc
    if timeout <= 0 or not 0 <= retries <= 10:
        raise ConfigError("GARAGEFLOW_TIMEOUT_SECONDS must be > 0 and GARAGEFLOW_MAX_RETRIES between 0 and 10.")

    tz = os.environ.get("SHOP_TIMEZONE", "Asia/Kolkata")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"SHOP_TIMEZONE {tz!r} is not a known IANA time zone.") from exc

    return Settings(
        garageflow_base_url=_required("GARAGEFLOW_BASE_URL").rstrip("/"),
        garageflow_email=_required("GARAGEFLOW_EMAIL"),
        garageflow_password=_required("GARAGEFLOW_PASSWORD"),
        timeout_seconds=timeout,
        max_retries=retries,
        shop_timezone=tz,
    )
