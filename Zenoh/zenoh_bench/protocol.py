from __future__ import annotations

import struct
import zlib

MAGIC = b"ZB10"
HEADER = struct.Struct("!4sIQQII")
HEADER_SIZE = HEADER.size


def build_message(publisher_id: int, sequence: int, send_ns: int, payload_size: int, seed: int) -> bytes:
    """Build a message with *payload_size* application bytes."""
    body_len = payload_size
    pattern = ((publisher_id * 31 + sequence * 17 + seed) & 0xFF)
    body = bytes([pattern]) * body_len
    checksum = zlib.crc32(body) & 0xFFFFFFFF
    wire_len = HEADER_SIZE + body_len
    return HEADER.pack(MAGIC, publisher_id, sequence, send_ns, wire_len, checksum) + body


def parse_message(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        return {"valid": False, "reason": "short"}
    magic, publisher_id, sequence, send_ns, declared_len, checksum = HEADER.unpack_from(data)
    body = data[HEADER_SIZE:]
    valid = magic == MAGIC and declared_len == len(data) and checksum == (zlib.crc32(body) & 0xFFFFFFFF)
    return {"valid": valid, "publisher_id": publisher_id, "sequence": sequence, "send_ns": send_ns,
            "payload_size": len(body), "wire_size": declared_len,
            "reason": None if valid else "metadata_or_checksum"}
