from __future__ import annotations

import hashlib
import zlib
from typing import Any


def make_payload_text(size_bytes: int, random_seed: int, condition_id: str) -> str:
    """Return deterministic ASCII text whose UTF-8 length is exactly size_bytes."""
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    seed = f"{random_seed}:{condition_id}".encode()
    block = hashlib.sha256(seed).hexdigest()
    if not size_bytes:
        return ""
    return (block * ((size_bytes + len(block) - 1) // len(block)))[:size_bytes]


def crc32_text(payload: str) -> int:
    return zlib.crc32(payload.encode("ascii")) & 0xFFFFFFFF


def read_payload_text(message: Any) -> str:
    """Read an IDL string as Python text across Fast DDS-Gen versions.

    Recent Fast DDS-Gen versions represent bounded IDL strings as
    ``fastcdr::fixed_string``. Their regular getter returns a SWIG proxy, while
    the generated ``<member>_str()`` helper returns an actual Python string.
    Older generators return ``str`` (or occasionally ``bytes``) directly.
    """
    text_getter = getattr(message, "payload_str", None)
    value = text_getter() if callable(text_getter) else message.payload()
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("ascii")
    raise TypeError(
        "Generated payload getter did not return str/bytes; "
        "a bounded IDL string requires payload_str()"
    )
