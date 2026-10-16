"""Fax number handling. eFax wants North American numbers as 1 + area code + number."""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_fax_number(value: str) -> str:
    """'(612) 555-0142', '+1 612.555.0142', '6125550142' -> '16125550142'.

    Raises ValueError for anything that is not a valid NANP number; international fax is not a
    use case yet, and failing loudly beats dialling the wrong digits.
    """
    digits = _NON_DIGITS.sub("", value or "")
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"not a North American fax number: {mask_number(value)}")
    area, exchange = digits[1:4], digits[4:7]
    if area[0] in "01" or exchange[0] in "01":
        raise ValueError(f"not a North American fax number: {mask_number(value)}")
    return digits


def mask_number(value: str | None) -> str:
    """Keep the last four digits for logs: '16125550142' -> '*******0142'."""
    digits = _NON_DIGITS.sub("", value or "")
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]
