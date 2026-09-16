import unittest
import uuid

from mqtt_cd.wire import HEADER_SIZE, decode, encode
from mqtt_cd.worker import TIMING_FIELDS, due_slot, planned_count


class WireTests(unittest.TestCase):
    def setUp(self):
        self.identifier = str(uuid.uuid4())
        self.payload = bytes(range(256)) * 4

    def test_round_trip_preserves_identity_and_byte_conventions(self):
        raw = encode(self.identifier, 0, 123, 987654321, 1, self.payload)
        result = decode(raw, self.identifier, len(self.payload))
        self.assertEqual(HEADER_SIZE, 64)
        self.assertEqual(result, dict(publisher_id=0, sequence=123, send_ns=987654321,
            phase=1, validation="ok", payload_bytes=1024, wire_bytes=1088))

    def test_crc_detects_changed_body(self):
        raw = bytearray(encode(self.identifier, 0, 0, 1, 1, self.payload))
        raw[-1] ^= 1
        self.assertEqual(decode(raw, self.identifier, 1024)["validation"], "bad_crc")

    def test_foreign_run_and_publisher_do_not_count_as_valid(self):
        raw = encode(self.identifier, 1, 0, 1, 1, self.payload)
        self.assertEqual(decode(raw, str(uuid.uuid4()), 1024)["validation"], "wrong_run")
        self.assertEqual(decode(raw, self.identifier, 1024)["validation"], "wrong_publisher")

    def test_invalid_header_and_size(self):
        raw = encode(self.identifier, 0, 0, 1, 0, self.payload)
        self.assertEqual(decode(raw[:30], self.identifier, 1024)["validation"], "bad_header")
        self.assertEqual(decode(raw[:-1], self.identifier, 1024)["validation"], "bad_length")
        self.assertEqual(decode(raw + b"extra", self.identifier, 1024)["validation"], "bad_length")
        modified = bytearray(raw)
        modified[4] = 99
        self.assertEqual(decode(modified, self.identifier, 1024)["validation"], "bad_header")

    def test_phase_and_reserved_fields(self):
        with self.assertRaises(ValueError):
            encode(self.identifier, 0, 0, 1, 2, self.payload)
        raw = bytearray(encode(self.identifier, 0, 0, 1, 0, self.payload))
        raw[52] = 1
        self.assertEqual(decode(raw, self.identifier, 1024)["validation"], "bad_header")

    def test_fixed_warmup_payload_is_independent_of_formal_payload(self):
        raw = encode(self.identifier, 0, 0, 1, 0, self.payload)
        self.assertEqual(decode(raw, self.identifier, 65536)["validation"], "ok")

    def test_fixed_schedule_counts_and_skips_instead_of_catching_up(self):
        self.assertEqual(planned_count(20, 100), 2000)
        self.assertEqual(planned_count(.15, 10), 2)
        self.assertEqual(due_slot(3_035_000_000, 3_000_000_000, 100, 1), 3)
        self.assertEqual(due_slot(2_000_000_000, 3_000_000_000, 100, 0), 0)
        self.assertEqual(due_slot(3_000_000_000, 3_000_000_000, 100, 1), 1)

    def test_last_moment_timestamp_stamp_preserves_payload_crc_and_prior_packet(self):
        template = bytearray(encode(self.identifier, 0, 0, 0, 1, self.payload))
        first, second = template.copy(), template.copy()
        TIMING_FIELDS.pack_into(first, 28, 10, 123456789)
        TIMING_FIELDS.pack_into(second, 28, 11, 123459999)
        result = decode(first, self.identifier, 1024)
        self.assertEqual(result["validation"], "ok")
        self.assertEqual((result["sequence"], result["send_ns"]), (10, 123456789))
        self.assertEqual(decode(second, self.identifier, 1024)["sequence"], 11)


if __name__ == "__main__":
    unittest.main()
