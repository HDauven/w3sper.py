# SPDX-License-Identifier: MPL-2.0
"""Hex and Bitcoin-alphabet Base58 (no checksum)."""

import binascii

from ._validation import octets

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_DIGITS = {char: index for index, char in enumerate(_ALPHABET)}


def base16_encode(data: bytes) -> str:
    return octets(data, "data").hex()


def base16_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise TypeError("hex must be text")
    try:
        return binascii.unhexlify(text)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid hex") from exc


def base58_encode(data: bytes) -> str:
    data = octets(data, "data")
    zeros = len(data) - len(data.lstrip(b"\0"))
    number = int.from_bytes(data, "big")
    digits = []
    while number:
        number, digit = divmod(number, 58)
        digits.append(_ALPHABET[digit])
    return "1" * zeros + "".join(reversed(digits))


def base58_decode(text: str) -> bytes:
    if not isinstance(text, str):
        raise TypeError("base58 must be text")
    number = 0
    for char in text:
        if char not in _DIGITS:
            raise ValueError("invalid base58 character")
        number = number * 58 + _DIGITS[char]
    zeros = len(text) - len(text.lstrip("1"))
    return b"\0" * zeros + number.to_bytes((number.bit_length() + 7) // 8, "big")


def key_type(identifier: str) -> str:
    """Identify by decoded length only; this does not validate curve points."""
    try:
        size = len(base58_decode(identifier))
    except (TypeError, ValueError):
        return "undefined"
    return {64: "address", 96: "account"}.get(size, "undefined")
