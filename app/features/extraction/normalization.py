from __future__ import annotations

import re

_FI_CODE_NOISE_RE = re.compile(r"[^A-Za-z0-9]+")

_ACCOUNT_NUMBER_NOISE_RE = re.compile(r"[^A-Za-z0-9/]+")

FI_CODE_LENGTH = 9


def normalize_fi_code(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = _FI_CODE_NOISE_RE.sub("", str(value))
    if not cleaned:
        return None

    if len(cleaned) < FI_CODE_LENGTH:
        cleaned = cleaned.rjust(FI_CODE_LENGTH, "0")

    return cleaned


def normalize_account_number(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = _ACCOUNT_NUMBER_NOISE_RE.sub("", str(value))
    if not cleaned:
        return None

    return cleaned