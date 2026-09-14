from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastdds_bench.rawio import (
    ALL_VALID_FLAGS,
    ReceiveRecord,
    ReceiveRecordWriter,
    SendRecord,
    SendRecordWriter,
    export_raw_csv,
    iter_receive_records,
    iter_send_records,
)


class RawIoTests(unittest.TestCase):
    def test_raw_record_round_trip_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            send_path = tmp_path / "send.bin"
            send = SendRecord(3, 7, 123456, 1024, 0xDEADBEEF)
            writer = SendRecordWriter(send_path)
            writer.append(send)
            self.assertEqual(writer.snapshot_count(), 1)
            self.assertEqual(writer.close(), 1)
            self.assertEqual(list(iter_send_records(send_path)), [send])

            receive_path = tmp_path / "receive.bin"
            receive = ReceiveRecord(
                3,
                7,
                123456,
                124456,
                1024,
                0xDEADBEEF,
                1024,
                0xDEADBEEF,
                ALL_VALID_FLAGS,
            )
            receive_writer = ReceiveRecordWriter(receive_path)
            receive_writer.append(receive)
            receive_writer.close()
            self.assertEqual(list(iter_receive_records(receive_path)), [receive])

            csv_path = tmp_path / "receive.csv"
            self.assertEqual(export_raw_csv(receive_path, csv_path), 1)
            text = csv_path.read_text(encoding="utf-8-sig")
            self.assertIn("publisher_id,sequence_number", text)
            self.assertIn("3735928559", text)

    def test_truncated_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "send.bin"
            writer = SendRecordWriter(path)
            writer.append(SendRecord(0, 0, 1, 1, 1))
            writer.close()
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "Truncated raw record"):
                list(iter_send_records(path))


if __name__ == "__main__":
    unittest.main()
