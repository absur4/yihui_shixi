"""Versioned, fixed-size benchmark envelope; byte counts exclude this envelope."""
from __future__ import annotations

import struct
import uuid
import zlib

MAGIC = b"MCD2"
VERSION = 1
HEADER = struct.Struct("!4sBBH16sIQQII12s")
HEADER_SIZE = HEADER.size


def encode(run_id, publisher_id, sequence, send_ns, phase, payload, crc=None):
    if phase not in (0, 1):
        raise ValueError("phase must be warmup (0) or formal (1)")
    run_bytes = run_id if isinstance(run_id, bytes) else uuid.UUID(str(run_id)).bytes
    checksum = zlib.crc32(payload) if crc is None else crc
    return HEADER.pack(MAGIC, VERSION, phase, 0, run_bytes, publisher_id,
                       sequence, send_ns, len(payload), checksum, b"\0" * 12) + payload


def decode(message, expected_run, expected_payload_bytes, publisher_count=1):
    result = dict(publisher_id=-1, sequence=-1, send_ns=0, phase=-1,
                  validation="bad_header", payload_bytes=0, wire_bytes=len(message))
    if len(message) < HEADER_SIZE:
        return result
    magic, version, phase, flags, run_bytes, pub, seq, sent, size, crc, reserved = HEADER.unpack_from(message)
    result.update(publisher_id=pub, sequence=seq, send_ns=sent, phase=phase, payload_bytes=size)
    if magic != MAGIC or version != VERSION or flags or reserved != b"\0" * 12 or phase not in (0, 1):
        return result
    expected = expected_run if isinstance(expected_run, bytes) else uuid.UUID(str(expected_run)).bytes
    if run_bytes != expected:
        result["validation"] = "wrong_run"
    elif pub >= publisher_count:
        result["validation"] = "wrong_publisher"
    elif size != (1024 if phase == 0 else expected_payload_bytes) or len(message) != HEADER_SIZE + size:
        result["validation"] = "bad_length"
    elif zlib.crc32(memoryview(message)[HEADER_SIZE:]) != crc:
        result["validation"] = "bad_crc"
    else:
        result["validation"] = "ok"
    return result
