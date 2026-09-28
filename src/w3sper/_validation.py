# SPDX-License-Identifier: MPL-2.0
"""Shared boundary checks. Never include seed/key contents in errors."""


def integer(value: int, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer (not bool or float)")
    return value


def uint(value: int, bits: int, name: str) -> int:
    integer(value, name)
    if not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit in unsigned {bits} bits")
    return value


def octets(value: bytes, name: str, size: int | None = None) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be bytes-like")
    result = bytes(value)
    if size is not None and len(result) != size:
        raise ValueError(f"{name} must be {size} bytes, got {len(result)}")
    return result
