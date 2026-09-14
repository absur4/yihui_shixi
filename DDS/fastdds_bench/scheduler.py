from __future__ import annotations


def aggregate_slot(
    local_sequence: int,
    publisher_id: int,
    publisher_count: int,
) -> int:
    if local_sequence < 0:
        raise ValueError("local_sequence must be non-negative")
    if publisher_count < 1:
        raise ValueError("publisher_count must be positive")
    if not 0 <= publisher_id < publisher_count:
        raise ValueError("publisher_id is outside publisher_count")
    return local_sequence * publisher_count + publisher_id


def aggregate_target_ns(start_ns: int, slot: int, aggregate_rate_hz: float) -> int:
    if slot < 0:
        raise ValueError("slot must be non-negative")
    if aggregate_rate_hz <= 0:
        raise ValueError("aggregate_rate_hz must be positive")
    return int(start_ns) + int(slot * 1_000_000_000 / aggregate_rate_hz)

