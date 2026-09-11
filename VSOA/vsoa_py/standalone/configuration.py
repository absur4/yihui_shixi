"""Strict shared configuration and repeatable scenario expansion."""

import copy
import math
from pathlib import Path

import yaml

SCENARIOS = ("small_message_latency", "large_message_throughput", "multi_subscriber_broadcast",
             "high_frequency_sustained", "weak_network_recovery")
FIELDS = {"module_name", "module_version", "scenario_name", "duration_seconds", "message_count",
          "payload_size_bytes", "message_size_bytes", "publish_rate_hz", "publisher_count",
          "subscriber_count", "transport_mode", "port", "output_directory", "log_file", "repeats",
          "qos_profile", "warmup_seconds", "drain_seconds", "timeout_seconds", "startup_timeout_seconds",
          "recovery_timeout_seconds", "network_loss_rate", "loss_rate", "network_delay_ms",
          "network_jitter_ms", "random_seed", "seed", "resource_sample_interval_seconds",
          "scenarios", "recovery_enabled"}
OVERRIDES = {"duration_seconds", "message_size_bytes", "publish_rate_hz", "publisher_count", "subscriber_count",
             "transport_mode", "loss_rate", "network_delay_ms", "network_jitter_ms", "recovery_enabled"}


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def numeric(config, key, minimum, maximum, integer=False):
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")


def validate_case(config):
    numeric(config, "duration_seconds", 0.1, 3600)
    numeric(config, "message_size_bytes", 1, 60000 if config["transport_mode"] == "udp" else 16 * 1024**2, True)
    numeric(config, "publish_rate_hz", 1, 50000)
    numeric(config, "publisher_count", 1, 8, True)
    numeric(config, "subscriber_count", 1, 16, True)
    numeric(config, "loss_rate", 0, 1)
    numeric(config, "network_delay_ms", 0, 1000)
    numeric(config, "network_jitter_ms", 0, 1000)
    if config["transport_mode"] not in {"tcp", "udp"}:
        raise ValueError("transport_mode supports only tcp or udp; TLS is not benchmarked")
    if not isinstance(config["recovery_enabled"], bool):
        raise ValueError("recovery_enabled must be true or false")
    if config["transport_mode"] == "tcp" and any(config[key] for key in ("loss_rate", "network_delay_ms", "network_jitter_ms")):
        raise ValueError("Network impairment is supported for UDP only; TCP packet loss is not simulated")
    deliveries = math.ceil(config["duration_seconds"] * config["publish_rate_hz"]) * config["publisher_count"] * config["subscriber_count"]
    if deliveries > 2_000_000:
        raise ValueError("At most 2000000 planned deliveries per run; reduce duration/rate/topology")


def load_config(path):
    path = Path(path).resolve()
    config = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueLoader)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a YAML mapping")
    aliases = {"payload_size_bytes": "message_size_bytes", "network_loss_rate": "loss_rate",
               "random_seed": "seed", "timeout_seconds": "startup_timeout_seconds"}
    for public, internal in aliases.items():
        if public in config:
            if internal in config and config[internal] != config[public]:
                raise ValueError(f"Conflicting configuration fields: {public} and {internal}")
            config[internal] = config[public]
    config.setdefault("module_version", "1.3")
    config.setdefault("scenario_name", "all")
    config.setdefault("message_count", 0)
    config.setdefault("qos_profile", "default")
    config.setdefault("warmup_seconds", 1.0)
    config.setdefault("recovery_timeout_seconds", config["timeout_seconds"])
    unknown = set(config) - FIELDS - {"qualification"}
    missing = FIELDS - set(config)
    if unknown or missing:
        raise ValueError(f"Configuration fields: unknown={sorted(unknown)}, missing={sorted(missing)}")
    if config["module_name"] != "vsoa":
        raise ValueError("This executable implements module_name: vsoa only")
    numeric(config, "port", 1024, 65000, True)
    numeric(config, "repeats", 1, 30, True)
    numeric(config, "message_count", 0, 100_000_000, True)
    numeric(config, "warmup_seconds", 0, 60)
    numeric(config, "drain_seconds", 0.05, 30)
    numeric(config, "startup_timeout_seconds", 2, 120)
    numeric(config, "recovery_timeout_seconds", 0.1, 120)
    numeric(config, "resource_sample_interval_seconds", 0.01, 10)
    numeric(config, "seed", 0, 2**31 - 1, True)
    if not isinstance(config["qos_profile"], str) or not config["qos_profile"]:
        raise ValueError("qos_profile must be a nonempty string")
    for key in ("output_directory", "log_file"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} must be a nonempty path string")
    if Path(config["log_file"]).name != config["log_file"] or config["log_file"] in {".", ".."}:
        raise ValueError("log_file must be a filename, not a path")
    if not isinstance(config["scenarios"], dict) or set(config["scenarios"]) != set(SCENARIOS):
        raise ValueError(f"scenarios must contain exactly {SCENARIOS}")
    validate_case(config)
    expanded = []
    for name in SCENARIOS:
        override = config["scenarios"][name]
        if not isinstance(override, dict) or set(override) - OVERRIDES:
            raise ValueError(f"Invalid override fields in {name}")
        effective = copy.deepcopy(config)
        effective.pop("scenarios")
        effective.pop("qualification", None)
        effective.update(override)
        effective["payload_size_bytes"] = effective["message_size_bytes"]
        effective["network_loss_rate"] = effective["loss_rate"]
        effective["random_seed"] = effective["seed"]
        effective["scenario_name"] = name
        validate_case(effective)
        if name == "multi_subscriber_broadcast" and effective["subscriber_count"] < 2:
            raise ValueError("multi_subscriber_broadcast requires at least two subscribers")
        if name == "weak_network_recovery" and effective["transport_mode"] != "udp":
            raise ValueError("weak_network_recovery requires transport_mode: udp")
        port_end = effective["port"] + effective["publisher_count"] + effective["publisher_count"] * effective["subscriber_count"]
        if port_end > 65535:
            raise ValueError("Configured port block exceeds 65535")
        expanded.append(effective)
    return config, expanded
