"""GSTIN rules: format, checksum and state code. Pure functions, no I/O."""

from __future__ import annotations

import re

GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATE_NAMES = {
    "07": "Delhi",
    "27": "Maharashtra",
    "29": "Karnataka",
    "33": "Tamil Nadu",
}


def gstin_check_char(first14: str) -> str:
    """The 15th character for the first 14.

    Map 0-9A-Z to 0..35. For index i (0-based), factor = 1 if i is even else 2;
    p = value * factor; sum += p // 36 + p % 36. Check = (36 - sum % 36) % 36.
    """
    if len(first14) != 14:
        raise ValueError(f"need 14 characters, got {len(first14)}")
    total = 0
    for i, ch in enumerate(first14):
        p = _CHARSET.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return _CHARSET[(36 - total % 36) % 36]


def is_valid_format(gstin: str) -> bool:
    return bool(GSTIN_PATTERN.fullmatch(gstin))


def is_valid_gstin(gstin: str) -> bool:
    """Format and checksum both hold."""
    return is_valid_format(gstin) and gstin_check_char(gstin[:14]) == gstin[14]


def make_gstin(state_code: str, pan: str, entity: str = "1") -> str:
    """Build a valid GSTIN from a state code and PAN (used for demo data)."""
    first14 = f"{state_code}{pan}{entity}Z"
    gstin = first14 + gstin_check_char(first14)
    if not is_valid_gstin(gstin):
        raise ValueError(f"built an invalid GSTIN {gstin}")
    return gstin


def state_code(gstin: str) -> str:
    return gstin[:2]
