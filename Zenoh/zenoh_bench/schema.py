from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "metrics_definition_version", "module_name", "scenario_name", "config", "environment", "counts", "metrics", "discovery_supported", "test_start_time", "test_end_time", "errors"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "metrics_definition_version": {"const": "unified-middleware-1.0"},
        "module_name": {"const": "Zenoh"},
        "module_version": {"type": "string"},
        "scenario_name": {"type": "string"},
        "config": {"type": "object"},
        "comparison_key": {"type": "object"},
        "environment": {"type": "object"},
        "counts": {"type": "object", "required": ["sent", "received", "expected", "valid_samples"]},
        "metrics": {"type": "object", "required": ["latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms", "jitter_ms", "throughput_mbps", "cpu_percent", "memory_mb", "packet_loss", "final_packet_loss", "startup_time_ms", "discovery_time_ms"]},
        "discovery_supported": {"type": "boolean"},
        "test_start_time": {"type": "string", "format": "date-time"},
        "test_end_time": {"type": "string", "format": "date-time"},
        "errors": {"type": "array"},
        "nodes": {"type": "array"},
        "delivery_matrix": {"type": "array"},
        "actual_payload_size_bytes": {"type": "integer", "minimum": 1},
        "wire_message_size_bytes": {"type": ["integer", "null"]},
        "samples": {"type": "array"}
    }
}


def validate_result(result: dict) -> list[str]:
    errors = sorted(
        Draft202012Validator(
            RESULT_SCHEMA, format_checker=FormatChecker()
        ).iter_errors(result),
        key=lambda e: list(e.path),
    )
    return [f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}" for error in errors]
