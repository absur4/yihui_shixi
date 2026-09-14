from __future__ import annotations

import unittest

from fastdds_bench.payload import read_payload_text


class _BoundedStringMessage:
    def payload_str(self) -> str:
        return "bounded"

    def payload(self) -> object:
        return object()


class _LegacyStringMessage:
    def payload(self) -> str:
        return "legacy"


class _BytesMessage:
    def payload(self) -> bytes:
        return b"bytes"


class PayloadCompatibilityTests(unittest.TestCase):
    def test_bounded_string_uses_generated_str_helper(self) -> None:
        self.assertEqual(read_payload_text(_BoundedStringMessage()), "bounded")

    def test_legacy_string_is_supported(self) -> None:
        self.assertEqual(read_payload_text(_LegacyStringMessage()), "legacy")

    def test_bytes_are_decoded(self) -> None:
        self.assertEqual(read_payload_text(_BytesMessage()), "bytes")


if __name__ == "__main__":
    unittest.main()
