"""Cleaning legacy values into what GarageFlow accepts. Each function returns (value, warnings) or raises
`Rejected` with a reason a shop manager can understand."""

from __future__ import annotations

import re
from datetime import datetime, timezone


class Rejected(ValueError):
    """This record cannot be migrated as it is. The message says why and what to fix."""


def clean_name(raw: str) -> tuple[str, list[str]]:
    name = re.sub(r"\s+", " ", raw).strip()
    if not name:
        raise Rejected("name is empty")
    if len(name) > 200:
        raise Rejected("name is longer than GarageFlow's 200 characters")
    warnings = []
    if name.isupper() or name.islower():
        fixed = name.title()
        warnings.append(f"name {name!r} changed to {fixed!r}")
        name = fixed
    return name, warnings


def clean_email(raw: str) -> tuple[str, list[str]]:
    email = raw.strip().lower()
    if not email:
        raise Rejected("email is missing (GarageFlow requires one): ask the shop for it")
    if email.count("@") != 1 or email.startswith("@") or email.endswith("@") or " " in email:
        raise Rejected(f"email {raw!r} is not valid")
    if len(email) > 256:
        raise Rejected("email is longer than 256 characters")
    warnings = []
    if "." not in email.split("@")[1]:
        warnings.append(f"email {email!r} has no domain suffix (like .com): GarageFlow accepts it, but check it with the shop")
    return email, warnings


def clean_phone(raw: str) -> tuple[str, list[str]]:
    """Indian mobile numbers (10 digits starting 6-9) → '+91 XXXXX XXXXX'. Other plausible numbers (landlines
    with an STD code, foreign numbers) are kept as written, with a warning."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise Rejected("phone is missing (GarageFlow requires one)")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91 {digits[:5]} {digits[5:]}", []
    if 7 <= len(digits) <= 15:
        return raw.strip()[:30], [f"phone {raw!r} is not an Indian mobile number; kept as it is"]
    raise Rejected(f"phone {raw!r} is not a valid phone number")


def clean_registration(raw: str) -> str:
    """Exactly GarageFlow's Vehicle.NormaliseRegistration: remove spaces and dashes, upper case."""
    reg = raw.replace(" ", "").replace("-", "").upper()
    if not reg:
        raise Rejected("registration number is missing")
    if len(reg) > 20:
        raise Rejected(f"registration {raw!r} is longer than 20 characters")
    return reg


def clean_year(raw: str, *, now: datetime | None = None) -> tuple[int, list[str]]:
    this_year = (now or datetime.now(timezone.utc)).year
    text = raw.strip().lstrip("'")
    if not text.isdigit():
        raise Rejected(f"year {raw!r} is not a number")
    warnings = []
    year = int(text)
    if len(text) == 2:
        guess = 2000 + year if 2000 + year <= this_year + 1 else 1900 + year
        warnings.append(f"two-digit year {raw!r} read as {guess}")
        year = guess
    if not 1980 <= year <= this_year + 1:
        raise Rejected(f"year {year} is outside GarageFlow's allowed range (1980 to {this_year + 1})")
    return year, warnings


def expand_make(code: str, codes: dict[str, str]) -> tuple[str, list[str]]:
    value = code.strip()
    if not value:
        raise Rejected("make is missing")
    if value.upper() in codes:
        return codes[value.upper()], []
    if len(value) <= 4 and value.isupper():
        return value, [f"unknown make code {value!r} kept as it is: ask the shop what it means"]
    return value, []
