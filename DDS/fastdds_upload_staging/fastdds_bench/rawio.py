from __future__ import annotations

import csv
import struct
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

HEADER = struct.Struct("<8sII")
SEND_MAGIC = b"FDBSND01"
RECEIVE_MAGIC = b"FDBRCV01"
FORMAT_VERSION = 1

SEND_RECORD = struct.Struct("<IQQII")
RECEIVE_RECORD = struct.Struct("<IQQQIIIII")

FLAG_DDS_VALID = 1 << 0
FLAG_DECODE_OK = 1 << 1
FLAG_LENGTH_OK = 1 << 2
FLAG_CHECKSUM_OK = 1 << 3
ALL_VALID_FLAGS = (
    FLAG_DDS_VALID | FLAG_DECODE_OK | FLAG_LENGTH_OK | FLAG_CHECKSUM_OK
)


@dataclass(frozen=True)
class SendRecord:
    publisher_id: int
    sequence_number: int
    send_timestamp_ns: int
    payload_length: int
    checksum: int


@dataclass(frozen=True)
class ReceiveRecord:
    publisher_id: int
    sequence_number: int
    send_timestamp_ns: int
    receive_timestamp_ns: int
    declared_payload_length: int
    declared_checksum: int
    actual_payload_length: int
    actual_checksum: int
    flags: int


class _RecordWriter:
    def __init__(self, path: Path, magic: bytes, record: struct.Struct) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._record = record
        self._stream = self.path.open("wb", buffering=1024 * 1024)
        self._stream.write(HEADER.pack(magic, FORMAT_VERSION, record.size))
        self._lock = threading.Lock()
        self._closed = False
        self.count = 0

    def _append(self, values: tuple[int, ...]) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.write(self._record.pack(*values))
            self.count += 1

    def snapshot_count(self, flush: bool = True) -> int:
        with self._lock:
            if flush and not self._closed:
                self._stream.flush()
            return self.count

    def close(self) -> int:
        with self._lock:
            if not self._closed:
                self._stream.flush()
                self._stream.close()
                self._closed = True
            return self.count


class SendRecordWriter(_RecordWriter):
    def __init__(self, path: Path) -> None:
        super().__init__(path, SEND_MAGIC, SEND_RECORD)

    def append(self, value: SendRecord) -> None:
        self._append(
            (
                value.publisher_id,
                value.sequence_number,
                value.send_timestamp_ns,
                value.payload_length,
                value.checksum,
            )
        )


class ReceiveRecordWriter(_RecordWriter):
    def __init__(self, path: Path) -> None:
        super().__init__(path, RECEIVE_MAGIC, RECEIVE_RECORD)

    def append(self, value: ReceiveRecord) -> None:
        self._append(
            (
                value.publisher_id,
                value.sequence_number,
                value.send_timestamp_ns,
                value.receive_timestamp_ns,
                value.declared_payload_length,
                value.declared_checksum,
                value.actual_payload_length,
                value.actual_checksum,
                value.flags,
            )
        )


def _iter_raw(path: Path, magic: bytes, record: struct.Struct) -> Iterator[tuple[int, ...]]:
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(HEADER.size)
        if len(header) != HEADER.size:
            raise ValueError(f"Raw record file has no complete header: {path}")
        actual_magic, version, record_size = HEADER.unpack(header)
        if actual_magic != magic or version != FORMAT_VERSION or record_size != record.size:
            raise ValueError(f"Unsupported raw record format: {path}")
        while True:
            block = stream.read(record.size)
            if not block:
                return
            if len(block) != record.size:
                raise ValueError(f"Truncated raw record at end of {path}")
            yield record.unpack(block)


def iter_send_records(path: Path) -> Iterator[SendRecord]:
    for values in _iter_raw(path, SEND_MAGIC, SEND_RECORD):
        yield SendRecord(*values)


def iter_receive_records(path: Path) -> Iterator[ReceiveRecord]:
    for values in _iter_raw(path, RECEIVE_MAGIC, RECEIVE_RECORD):
        yield ReceiveRecord(*values)


def export_raw_csv(input_path: Path, output_path: Path) -> int:
    input_path = Path(input_path)
    with input_path.open("rb") as stream:
        magic = stream.read(8)
    if magic == SEND_MAGIC:
        rows = iter_send_records(input_path)
        fields = list(SendRecord.__dataclass_fields__)
    elif magic == RECEIVE_MAGIC:
        rows = iter_receive_records(input_path)
        fields = list(ReceiveRecord.__dataclass_fields__)
    else:
        raise ValueError(f"Unknown raw record file: {input_path}")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
            count += 1
    return count

