# SPDX-License-Identifier: MPL-2.0
import unittest
from dataclasses import FrozenInstanceError

from w3sper import (
    Bookmark, Gas, Profile, base16_decode, base16_encode, base58_decode,
    base58_encode, format_to_dusk, key_type, parse_from_dusk, validate_chain_id,
)


class ValueTests(unittest.TestCase):
    def test_go_lux_vectors(self):
        for lux, dusk in [(0, "0"), (11_000_000_000, "11"), (11_000_000_001, "11.000000001"),
                          (1_000_000_000, "1"), (1, "0.000000001"),
                          (2**64 - 1, "18446744073.709551615")]:
            with self.subTest(lux=lux):
                self.assertEqual(format_to_dusk(lux), dusk)
                self.assertEqual(parse_from_dusk(dusk), lux)

    def test_exact_large_and_noncanonical_decimals(self):
        self.assertEqual(parse_from_dusk("0001.100"), 1_100_000_000)
        self.assertEqual(parse_from_dusk("1."), 1_000_000_000)
        for n in (10**100, 10**100 + 1, 999):
            self.assertEqual(parse_from_dusk(format_to_dusk(n)), n)

    def test_bad_dusk(self):
        for value in ("", ".1", ".", "1.0000000001", "-1", "+1", "1e9", " 1", "1\n", "1.2.3", "１", "1_000", "nan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_from_dusk(value)
        for value in (None, True, 1, 1.1):
            with self.subTest(value=value), self.assertRaises(TypeError):
                parse_from_dusk(value)

    def test_bad_lux(self):
        with self.assertRaises(ValueError):
            format_to_dusk(-1)
        for value in (None, True, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                format_to_dusk(value)

    def test_gas(self):
        self.assertEqual(Gas().limit, 2_900_000_000)
        self.assertEqual(Gas().price, 1)
        self.assertEqual(Gas(0, -5), Gas())
        self.assertEqual(Gas(3_000_000, 2).total, 6_000_000)
        self.assertEqual(Gas(2**64, 2).total, 2**65)
        with self.assertRaises(FrozenInstanceError):
            Gas().price = 2
        for value in (True, "1", 1.0):
            with self.assertRaises(TypeError):
                Gas(value, 1)

    def test_chain_id(self):
        for value in (0, 1, 42, 255):
            self.assertEqual(validate_chain_id(value), value)
        for value in (-1, 256, 2**100):
            with self.assertRaises(ValueError):
                validate_chain_id(value)
        for value in (None, False, "42", 1.0):
            with self.assertRaises(TypeError):
                validate_chain_id(value)

    def test_bookmark(self):
        raw = bytearray.fromhex("deadbeef00000000")
        bookmark = Bookmark(raw)
        raw[0] = 0
        self.assertEqual(str(bookmark), "deadbeef00000000")
        self.assertEqual(int(bookmark), 0xEFBEADDE)
        self.assertEqual(bytes(Bookmark.from_int(1)), b"\x01" + bytes(7))
        self.assertTrue(Bookmark.from_int(2**64 - 1).is_none)
        self.assertFalse(Bookmark.from_int(0).is_none)
        self.assertEqual(Bookmark.from_int(int(bookmark)), bookmark)

    def test_bad_bookmark(self):
        for value in (b"", bytes(7), bytes(9)):
            with self.assertRaises(ValueError):
                Bookmark(value)
        for value in (-1, 2**64):
            with self.assertRaises(ValueError):
                Bookmark.from_int(value)
        for value in (True, 1.0, "1"):
            with self.assertRaises(TypeError):
                Bookmark.from_int(value)
        with self.assertRaises(TypeError):
            Bookmark(8)


class EncodingTests(unittest.TestCase):
    def test_base16(self):
        for value in (b"", bytes(range(256)), b"abc123", b"\xab" * 32):
            self.assertEqual(base16_decode(base16_encode(value)), value)
        self.assertEqual(base16_decode("aAbB"), b"\xaa\xbb")
        self.assertEqual(base16_encode(b"\xab"), "ab")

    def test_bad_base16(self):
        for value in ("abc", "gg", "ab cd", "ab\n", "éé", "0x12"):
            with self.assertRaises(ValueError):
                base16_decode(value)
        with self.assertRaises(TypeError):
            base16_decode(b"ab")
        with self.assertRaises(TypeError):
            base16_encode(3)

    def test_base58_go_vectors(self):
        for value, text in [(b"", ""), (b"\0", "1"), (bytes(4), "1111"),
                            (b"Hello World!", "2NEpo7TZRRrLZSi2U")]:
            self.assertEqual(base58_encode(value), text)
            self.assertEqual(base58_decode(text), value)
        for value in (bytes(range(256)), b"\0\0\xff", b"\xff", b"hello"):
            self.assertEqual(base58_decode(base58_encode(value)), value)

    def test_bad_base58(self):
        for value in ("0", "O", "I", "l", "é", " abc", "123\n", "!"):
            with self.assertRaises(ValueError):
                base58_decode(value)
        with self.assertRaises(TypeError):
            base58_decode(1)
        with self.assertRaises(TypeError):
            base58_encode([0])

    def test_key_type(self):
        for size, kind in ((64, "address"), (96, "account"), (3, "undefined")):
            self.assertEqual(key_type(base58_encode(bytes(size))), kind)
        for value in ("", "!", None):
            self.assertEqual(key_type(value), "undefined")

    def test_public_profile_ownership(self):
        data = bytearray(b"\xaa" * 64 + b"\xbb" * 96)
        public = Profile(data)
        data[:] = bytes(160)
        self.assertEqual(public.address, b"\xaa" * 64)
        self.assertEqual(public.account, b"\xbb" * 96)
        self.assertEqual(base58_decode(public.account_id), public.account)
        self.assertEqual(base58_decode(public.address_id), public.address)
        self.assertEqual(bytes(public), public.address + public.account)
        self.assertFalse(hasattr(public, "_seed"))
        with self.assertRaises(FrozenInstanceError):
            public.data = bytes(160)

    def test_bad_profile(self):
        for size in (0, 64, 96, 159, 161):
            with self.assertRaises(ValueError):
                Profile(bytes(size))
        with self.assertRaises(TypeError):
            Profile(160)


if __name__ == "__main__":
    unittest.main()
