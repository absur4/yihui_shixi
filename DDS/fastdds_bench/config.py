from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from . import METRICS_DEFINITION_VERSION, MIDDLEWARE_ID, SCHEMA_VERSION, VENDOR


class ConfigError(ValueError):
    pass


SUITE_DEFAULTS: dict[str, Any] = {
    "middleware_id": MIDDLEWARE_ID,
    "module_name": MIDDLEWARE_ID,
    "vendor": VENDOR,
    "label": "DDS",
    "module_version": "3.6.2",
    "fastdds_python_version": "2.6.1",
    "domain_id_base": 80,
    "same_host": True,
    "rate_scope": "per_publisher",
    "message_count_scope": "per_publisher",
    "default_repeats": 5,
    "formal_repeats": 10,
    "startup_timeout_seconds": 30.0,
    "discovery_timeout_seconds": 30.0,
    "barrier_delay_ms": 250,
    "resource_sample_interval_ms": 50,
    "spin_threshold_us": 100,
    "warmup_unlimited_rate_hz": 1000,
    "default_run_timeout_seconds": 180.0,
}

CONDITION_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "capability": "supported",
    "capability_note": None,
    "scenario_title": None,
    "message_count": 0,
    "publish_rate_hz": 0,
    "publisher_count": 1,
    "subscriber_count": 1,
    "transport_mode": "udp",
    "qos_profile": "reliable",
    "warmup_seconds": 1.0,
    "drain_seconds": 2.0,
    "duration_seconds": 0.0,
    "repeats": 5,
    "network_profile": "normal",
    "random_seed": 7401,
    "timeout_seconds": None,
    "fault_plan": {"kind": "none"},
}

SUPPORTED_TRANSPORTS = {"udp", "shm", "default"}
SUPPORTED_RELIABILITY = {"reliable", "best_effort"}
SUPPORTED_DURABILITY = {"volatile", "transient_local"}
SUPPORTED_HISTORY = {"keep_last", "keep_all"}
SUPPORTED_PUBLISH_MODE = {"synchronous", "asynchronous"}
# capability 说明见 DDS_readme_1.md §6.4：
#   supported  —— 本适配器可以真实执行并测量
#   not_tested —— 条件保留在目录里，但当前不产生数据（需要外部注入器 / 后续阶段能力）
#   unsupported—— 中间件语义上不支持
SUPPORTED_CAPABILITIES = {"supported", "not_tested", "unsupported"}

SCENARIO_ID_PATTERN = re.compile(r"^S(0[1-9]|1[0-2])$")

# 引擎真正会执行的故障动作；其余故障必须交给四种中间件共用的编排器。
EXECUTABLE_FAULTS = {"none", "restart_publisher", "restart_subscriber"}


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{context} missing required field: {key}")
    return mapping[key]


def _number(value: Any, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return float(value)


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def load_raw_config(path: Path) -> dict[str, Any]:
    """读取未规范化的配置字典（供不依赖 PyYAML 的调用方复用同一份矩阵）。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream)
    if not isinstance(loaded, dict):
        raise ConfigError("The YAML root must be an object")
    return loaded


def load_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    return normalize_config(load_raw_config(path))


def condition_from_console_case(case: dict[str, Any]) -> dict[str, Any]:
    """把控制台 spec.json 的 case 转成引擎条件。

    控制台 case 里 `scenario_name` 是场景编号（S01…S12）、`case` 是
    `interfaces/scenarios.py` 的规范名；引擎侧反过来：`scenario_name` 存规范名，
    `scenario_id` 存编号。
    """
    repeats = case.get("case_repeats") or case.get("repeats") or 1
    return {
        "condition_id": str(case.get("condition_id")),
        "scenario_id": str(case.get("scenario_id") or case.get("scenario_name")),
        "scenario_name": str(case.get("case") or case.get("scenario_name")),
        "scenario_title": case.get("scenario_title") or case.get("title"),
        "payload_size_bytes": int(case.get("payload_size_bytes") or 0),
        "message_count": int(case.get("message_count") or 0),
        "publish_rate_hz": float(case.get("publish_rate_hz") or 0),
        "publisher_count": int(case.get("publisher_count") or 1),
        "subscriber_count": int(case.get("subscriber_count") or 1),
        "transport_mode": str(case.get("transport_mode") or "udp").lower(),
        "qos_profile": str(case.get("qos_profile") or "reliable"),
        "warmup_seconds": float(case.get("warmup_seconds") or 0),
        "drain_seconds": float(case.get("drain_seconds") or 0),
        "duration_seconds": float(case.get("duration_seconds") or 0),
        "repeats": int(repeats),
        "random_seed": int(case.get("random_seed") or 0),
        "network_profile": str(case.get("network_profile") or "normal"),
        "timeout_seconds": (
            None
            if case.get("timeout_seconds") in (None, "")
            else float(case["timeout_seconds"])
        ),
        "capability": str(case.get("capability") or "supported"),
        "capability_note": case.get("capability_note"),
        "enabled": bool(case.get("enabled", True)),
        "fault_plan": case.get("fault_plan") or {"kind": "none"},
    }


def normalize_config(source: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    config = copy.deepcopy(source)
    warnings: list[str] = []

    if str(config.get("schema_version")) != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION!r}")
    if str(config.get("metrics_definition_version")) != METRICS_DEFINITION_VERSION:
        raise ConfigError(
            f"metrics_definition_version must be {METRICS_DEFINITION_VERSION!r}"
        )

    suite = config.setdefault("suite", {})
    if not isinstance(suite, dict):
        raise ConfigError("suite must be an object")
    for key, value in SUITE_DEFAULTS.items():
        suite.setdefault(key, value)
    if suite["middleware_id"] != MIDDLEWARE_ID or suite["module_name"] != MIDDLEWARE_ID:
        raise ConfigError(f"suite.module_name must be {MIDDLEWARE_ID!r}")
    if not str(suite["vendor"]):
        raise ConfigError("suite.vendor must not be empty")
    if suite["same_host"] is not True:
        raise ConfigError(
            "This adapter uses perf_counter_ns across local processes; same_host must be true"
        )
    if suite["rate_scope"] != "per_publisher":
        raise ConfigError("suite.rate_scope must be 'per_publisher'")
    if suite["message_count_scope"] != "per_publisher":
        raise ConfigError("suite.message_count_scope must be 'per_publisher'")
    _integer(suite["default_repeats"], "suite.default_repeats", 1)
    _integer(suite["formal_repeats"], "suite.formal_repeats", 1)
    domain_base = _integer(suite["domain_id_base"], "suite.domain_id_base")
    if domain_base > 232:
        raise ConfigError("suite.domain_id_base must be <= 232")
    for name in (
        "startup_timeout_seconds",
        "discovery_timeout_seconds",
        "default_run_timeout_seconds",
    ):
        _number(suite[name], f"suite.{name}", 0.001)
    _integer(suite["barrier_delay_ms"], "suite.barrier_delay_ms", 0)
    _integer(
        suite["resource_sample_interval_ms"],
        "suite.resource_sample_interval_ms",
        10,
    )
    _integer(suite["spin_threshold_us"], "suite.spin_threshold_us", 0)
    _number(
        suite["warmup_unlimited_rate_hz"],
        "suite.warmup_unlimited_rate_hz",
        0.001,
    )

    qos_profiles = _require(config, "qos_profiles", "config")
    if not isinstance(qos_profiles, dict) or not qos_profiles:
        raise ConfigError("qos_profiles must be a non-empty object")
    for name, qos in qos_profiles.items():
        if not isinstance(qos, dict):
            raise ConfigError(f"qos_profiles.{name} must be an object")
        qos.setdefault("reliability", "reliable")
        qos.setdefault("durability", "volatile")
        qos.setdefault("history_kind", "keep_last")
        qos.setdefault("history_depth", 1024)
        qos.setdefault("publish_mode", "synchronous")
        qos.setdefault("max_blocking_time_ms", 1000)
        qos.setdefault("disable_data_sharing", True)
        qos.setdefault("wait_for_acknowledgments", True)
        if qos["reliability"] not in SUPPORTED_RELIABILITY:
            raise ConfigError(f"qos_profiles.{name}.reliability is unsupported")
        if qos["durability"] not in SUPPORTED_DURABILITY:
            raise ConfigError(f"qos_profiles.{name}.durability is unsupported")
        if qos["history_kind"] not in SUPPORTED_HISTORY:
            raise ConfigError(f"qos_profiles.{name}.history_kind is unsupported")
        if qos["publish_mode"] not in SUPPORTED_PUBLISH_MODE:
            raise ConfigError(f"qos_profiles.{name}.publish_mode is unsupported")
        _integer(qos["history_depth"], f"qos_profiles.{name}.history_depth", 1)
        _integer(
            qos["max_blocking_time_ms"],
            f"qos_profiles.{name}.max_blocking_time_ms",
            0,
        )
        _boolean(
            qos["disable_data_sharing"],
            f"qos_profiles.{name}.disable_data_sharing",
        )
        _boolean(
            qos["wait_for_acknowledgments"],
            f"qos_profiles.{name}.wait_for_acknowledgments",
        )

    network_profiles = config.setdefault(
        "network_profiles",
        {
            "normal": {
                "latency_ms": 0,
                "jitter_ms": 0,
                "packet_loss_percent": 0,
                "bandwidth_mbps": 0,
                "impairment_required": False,
                "external_impairment_confirmed": True,
            }
        },
    )
    if not isinstance(network_profiles, dict) or not network_profiles:
        raise ConfigError("network_profiles must be a non-empty object")
    for name, profile in network_profiles.items():
        if not isinstance(profile, dict):
            raise ConfigError(f"network_profiles.{name} must be an object")
        for key in ("latency_ms", "jitter_ms", "packet_loss_percent", "bandwidth_mbps"):
            profile.setdefault(key, 0)
            _number(profile[key], f"network_profiles.{name}.{key}")
        profile.setdefault("impairment_required", False)
        profile.setdefault("external_impairment_confirmed", False)
        _boolean(
            profile["impairment_required"],
            f"network_profiles.{name}.impairment_required",
        )
        _boolean(
            profile["external_impairment_confirmed"],
            f"network_profiles.{name}.external_impairment_confirmed",
        )
        if profile["packet_loss_percent"] > 100:
            raise ConfigError(f"network_profiles.{name}.packet_loss_percent must be <= 100")
        declared_impairment = any(
            float(profile[key]) > 0
            for key in (
                "latency_ms",
                "jitter_ms",
                "packet_loss_percent",
                "bandwidth_mbps",
            )
        )
        if declared_impairment and not profile["impairment_required"]:
            raise ConfigError(
                f"network_profiles.{name} declares an impairment but "
                "impairment_required is false"
            )

    conditions = _require(config, "conditions", "config")
    if not isinstance(conditions, list) or not conditions:
        raise ConfigError("conditions must be a non-empty array")
    seen: set[str] = set()
    normalized_conditions: list[dict[str, Any]] = []
    for index, item in enumerate(conditions):
        context = f"conditions[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be an object")
        condition = copy.deepcopy(CONDITION_DEFAULTS)
        condition.update(item)
        condition_id = str(_require(condition, "condition_id", context))
        scenario_id = str(_require(condition, "scenario_id", context))
        if not SCENARIO_ID_PATTERN.match(scenario_id):
            raise ConfigError(
                f"{context}.scenario_id must look like 'S01'..'S12' "
                "(interfaces/scenarios.py is the single source of scenario ids)"
            )
        scenario_name = str(_require(condition, "scenario_name", context))
        if not condition_id or condition_id in seen:
            raise ConfigError(f"{context}.condition_id must be non-empty and unique")
        seen.add(condition_id)
        condition["condition_id"] = condition_id
        condition["scenario_id"] = scenario_id
        condition["scenario_name"] = scenario_name
        condition["capability"] = str(condition["capability"])
        if condition["capability"] not in SUPPORTED_CAPABILITIES:
            raise ConfigError(
                f"{context}.capability must be one of {sorted(SUPPORTED_CAPABILITIES)}"
            )
        if condition["capability"] != "supported" and not condition["capability_note"]:
            raise ConfigError(
                f"{context}.capability_note is required when capability is not 'supported'"
            )
        condition["payload_size_bytes"] = _integer(
            _require(condition, "payload_size_bytes", context),
            f"{context}.payload_size_bytes",
            1,
        )
        if condition["payload_size_bytes"] > 1_048_576:
            raise ConfigError(f"{context}.payload_size_bytes exceeds the 1 MiB IDL bound")
        condition["message_count"] = _integer(
            condition["message_count"], f"{context}.message_count", 0
        )
        condition["publish_rate_hz"] = _number(
            condition["publish_rate_hz"], f"{context}.publish_rate_hz", 0
        )
        condition["publisher_count"] = _integer(
            condition["publisher_count"], f"{context}.publisher_count", 1
        )
        condition["subscriber_count"] = _integer(
            condition["subscriber_count"], f"{context}.subscriber_count", 1
        )
        for key in ("warmup_seconds", "drain_seconds", "duration_seconds"):
            condition[key] = _number(condition[key], f"{context}.{key}", 0)
        condition["repeats"] = _integer(condition["repeats"], f"{context}.repeats", 1)
        condition["random_seed"] = _integer(
            condition["random_seed"], f"{context}.random_seed", 0
        )
        if condition["timeout_seconds"] is not None:
            condition["timeout_seconds"] = _number(
                condition["timeout_seconds"], f"{context}.timeout_seconds", 0.001
            )
        _boolean(condition["enabled"], f"{context}.enabled")
        if condition["message_count"] == 0 and condition["duration_seconds"] == 0:
            raise ConfigError(
                f"{context} needs message_count > 0 or duration_seconds > 0"
            )
        if condition["transport_mode"] not in SUPPORTED_TRANSPORTS:
            raise ConfigError(
                f"{context}.transport_mode must be one of {sorted(SUPPORTED_TRANSPORTS)}"
            )
        if condition["qos_profile"] not in qos_profiles:
            raise ConfigError(f"{context}.qos_profile does not exist")
        if condition["network_profile"] not in network_profiles:
            raise ConfigError(f"{context}.network_profile does not exist")
        if condition["repeats"] < 5 and not condition_id.startswith("EXAMPLE"):
            warnings.append(
                f"{condition_id}: repeats={condition['repeats']} is below the formal minimum of 5"
            )
        rate = condition["publish_rate_hz"]
        count = condition["message_count"]
        duration = condition["duration_seconds"]
        if rate > 0 and count > 0 and duration > 0:
            planned = count / rate
            tolerance = max(0.05, planned * 0.01)
            if abs(planned - duration) > tolerance:
                warnings.append(
                    f"{condition_id}: message_count / publish_rate_hz = {planned:g}s "
                    f"(per publisher), but duration_seconds = {duration:g}s; "
                    "message_count is the stop condition"
                )
        fault_plan = condition.get("fault_plan", {"kind": "none"})
        if not isinstance(fault_plan, dict) or "kind" not in fault_plan:
            raise ConfigError(f"{context}.fault_plan must be an object with kind")
        fault_kind = str(fault_plan["kind"])
        condition["fault_plan"] = fault_plan
        if fault_kind not in EXECUTABLE_FAULTS and condition["capability"] == "supported":
            raise ConfigError(
                f"{context}.fault_plan.kind={fault_kind!r} must be supplied by the shared "
                "cross-middleware fault orchestrator; mark the condition capability='not_tested'"
            )
        normalized_conditions.append(condition)

    config["conditions"] = normalized_conditions
    comparison_keys: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for condition in normalized_conditions:
        group_key = (
            condition["scenario_name"],
            condition["payload_size_bytes"],
            condition["publish_rate_hz"],
            condition["publisher_count"],
            condition["subscriber_count"],
            condition["transport_mode"],
            condition["qos_profile"],
            condition["network_profile"],
        )
        comparison_controls = (
            condition["message_count"],
            condition["duration_seconds"],
            condition["warmup_seconds"],
            condition["drain_seconds"],
            condition["repeats"],
        )
        previous = comparison_keys.setdefault(group_key, comparison_controls)
        if previous != comparison_controls:
            raise ConfigError(
                f"{condition['condition_id']}: conditions in the same result group "
                "must use identical message_count, duration, warm-up, drain, and repeats"
            )
    return config, warnings


def select_conditions(
    config: dict[str, Any], condition_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """返回正式可执行的条件：enabled 且 capability=supported。"""
    selected = [
        c
        for c in config["conditions"]
        if c.get("enabled", True) and c.get("capability") == "supported"
    ]
    if condition_ids:
        wanted = set(condition_ids)
        selected = [c for c in selected if c["condition_id"] in wanted]
        missing = wanted - {c["condition_id"] for c in selected}
        if missing:
            raise ConfigError(f"Executable condition(s) not found: {sorted(missing)}")
    if not selected:
        raise ConfigError("No enabled conditions selected")
    return selected


def effective_condition(config: dict[str, Any], condition: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(condition)
    result["qos"] = copy.deepcopy(config["qos_profiles"][condition["qos_profile"]])
    result["network"] = copy.deepcopy(
        config["network_profiles"][condition["network_profile"]]
    )
    result["rate_scope"] = config["suite"]["rate_scope"]
    result["message_count_scope"] = config["suite"]["message_count_scope"]
    result["spin_threshold_us"] = config["suite"]["spin_threshold_us"]
    result["warmup_unlimited_rate_hz"] = config["suite"][
        "warmup_unlimited_rate_hz"
    ]
    return result
