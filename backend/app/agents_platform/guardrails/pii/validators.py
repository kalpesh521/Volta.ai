"""
Checksums and format rules. A bare run of digits is not an identifier: these checks are what stop an energy
reading from being mistaken for a card number.
"""
from __future__ import annotations

import re

# Verhoeff (used by Aadhaar). Tables from the original algorithm.
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)

_PAN = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_PAN_HOLDER_TYPES = frozenset("ABCFGHLJPT")
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def luhn_valid(number: str) -> bool:
    digits = digits_only(number)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def verhoeff_valid(number: str) -> bool:
    digits = digits_only(number)
    if not digits:
        return False
    check = 0
    for index, char in enumerate(reversed(digits)):
        check = _D[check][_P[index % 8][int(char)]]
    return check == 0


def verhoeff_check_digit(payload: str) -> str:
    """The digit that makes `payload + digit` pass `verhoeff_valid`. Used to build test identifiers."""
    check = 0
    for index, char in enumerate(reversed(digits_only(payload))):
        check = _D[check][_P[(index + 1) % 8][int(char)]]
    return str(_INV[check])


def aadhaar_valid(number: str) -> bool:
    digits = digits_only(number)
    return len(digits) == 12 and digits[0] in "23456789" and verhoeff_valid(digits)


def pan_valid(value: str) -> bool:
    upper = value.upper()
    return bool(_PAN.match(upper)) and upper[3] in _PAN_HOLDER_TYPES


def ifsc_valid(value: str) -> bool:
    return bool(_IFSC.match(value.upper()))


def gps_valid(latitude: float, longitude: float) -> bool:
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0
