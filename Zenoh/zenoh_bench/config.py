from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PAYLOAD_SIZES = [1024, 4096, 16384, 32768, 49152, 65536, 262144, 1048576]


@dataclass(slots=True)
class NetworkProfile:
    name: str = "normal"
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    bandwidth_mbps: float | None = None


@dataclass(slots=True)
class BenchConfig:
    module_name: str = "Zenoh"
    module_version: str = "auto"
    scenario_name: str = "S01"
    payload_size_bytes: int = 1024
    message_count: int = 1000
    publish_rate_hz: float = 100.0
    publisher_count: int = 1
    subscriber_count: int = 1
    transport_mode: str = "TCP"
    qos_profile: str = "reliable-block"
    warmup_seconds: float = 1.0
    drain_seconds: float = 1.0
    duration_seconds: float = 10.0
    repeats: int = 5
    network_profile: NetworkProfile = field(default_factory=NetworkProfile)
    random_seed: int = 20260909
    run_id: str = ""
    key_prefix: str = "bench/zenoh"
    mode: str = "peer"
    connect: list[str] = field(default_factory=list)
    listen: list[str] = field(default_factory=list)
    expected_publishers: int | None = None
    expected_subscribers: int | None = None
    discovery_timeout_seconds: float = 10.0
    recovery_timeout_seconds: float = 30.0
    clock_offset_ns: int = 0
    scheduled_start_epoch_ns: int | None = None

    def validate(self) -> None:
        if self.payload_size_bytes < 1:
            raise ValueError("payload_size_bytes must be positive")
        if self.message_count < 0 or self.publish_rate_hz < 0:
            raise ValueError("message_count and publish_rate_hz cannot be negative")
        if self.publisher_count < 1 or self.subscriber_count < 1:
            raise ValueError("publisher_count and subscriber_count must be positive")
        if self.repeats < 5:
            raise ValueError("repeats must be at least 5 per the unified requirements")
        if self.transport_mode.upper() not in {"TCP", "UDP", "QUIC", "OTHER"}:
            raise ValueError("transport_mode must be TCP, UDP, QUIC or OTHER")
        if not 0 <= self.network_profile.loss_percent <= 100:
            raise ValueError("network loss must be between 0 and 100")
        if self.network_profile.delay_ms < 0 or self.network_profile.jitter_ms < 0:
            raise ValueError("network delay and jitter cannot be negative")
        if self.network_profile.bandwidth_mbps is not None and self.network_profile.bandwidth_mbps <= 0:
            raise ValueError("bandwidth_mbps must be positive when configured")
        if self.discovery_timeout_seconds <= 0 or self.recovery_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.scheduled_start_epoch_ns is not None and self.scheduled_start_epoch_ns <= 0:
            raise ValueError("scheduled_start_epoch_ns must be positive when configured")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchConfig":
        data = dict(raw)
        network = data.get("network_profile", {})
        if isinstance(network, str):
            network = {"name": network}
        data["network_profile"] = NetworkProfile(**network)
        allowed = set(cls.__dataclass_fields__)
        obj = cls(**{k: v for k, v in data.items() if k in allowed})
        obj.validate()
        return obj


