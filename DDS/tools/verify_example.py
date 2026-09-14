"""一次性示例的严格验收：真实 Fast DDS 通信是否完整、无损、可审计。

用法::

    .venv\\Scripts\\python.exe tools\\verify_example.py --input results\\one_example

`--input` 指向 launch.py 的套件目录（含 result.json / runs/ / artifacts/）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastdds_bench.schema_validation import validate_result  # noqa: E402
from fastdds_bench.util import read_json, sha256_file  # noqa: E402

EXPECTED_CONDITION = "EXAMPLE_S01_1KiB_100"
EXPECTED_MESSAGES = 200


class VerificationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _suite_path(target: Path) -> Path:
    return target / "result.json" if target.is_dir() else target


def verify(target: Path) -> dict[str, object]:
    target = Path(target)
    suite_dir = target if target.is_dir() else target.parent
    document = read_json(_suite_path(target))
    validate_result(document)
    runs = document.get("runs", [])
    _require(len(runs) == 1, f"Expected exactly one run, found {len(runs)}")
    run = runs[0]
    _require(run["condition_id"] == EXPECTED_CONDITION, "Wrong example condition")
    _require(
        run["status"] == "completed",
        f"Run status is {run['status']!r}: {run.get('errors')}",
    )
    _require(run["messages_sent"] == EXPECTED_MESSAGES, "Expected 200 successful sends")
    _require(
        run["expected_deliveries"] == EXPECTED_MESSAGES, "Expected 200 deliveries"
    )
    _require(
        run["unique_deliveries"] == EXPECTED_MESSAGES,
        "Not all 200 messages arrived after drain",
    )
    _require(run["final_packet_loss"] == 0, "Final packet loss is not zero")
    for field in ("duplicate_count", "out_of_order_count", "corrupted_count"):
        _require(run.get(field) == 0, f"{field} is not zero: {run.get(field)}")
    statistics = run.get("statistics") or {}
    for field in (
        "unexpected_count",
        "clock_anomaly_count",
        "metadata_mismatch_count",
        "malformed_send_count",
    ):
        _require(statistics.get(field) == 0, f"{field} is not zero: {statistics.get(field)}")
    _require((run.get("latency_sample_count") or 0) > 0, "No valid latency sample")
    for field in ("latency_ms", "latency_p95_ms", "latency_p99_ms"):
        _require(run.get(field) is not None, f"{field} is null")
    for field in ("throughput_mbps", "cpu_percent", "memory_mb"):
        _require(run.get(field) is not None, f"{field} is null")

    artifacts_dir = suite_dir / (run.get("artifacts_directory") or f"artifacts/{run['run_id']}")
    checked_artifacts = 0
    for key in ("publisher_raw", "subscriber_raw"):
        for artifact in (run.get("artifacts") or {}).get(key, []):
            artifact_path = artifacts_dir / artifact["path"]
            _require(artifact_path.is_file(), f"Missing raw artifact: {artifact_path}")
            _require(
                sha256_file(artifact_path) == artifact["sha256"],
                f"SHA-256 mismatch: {artifact_path}",
            )
            checked_artifacts += 1
    _require(checked_artifacts == 2, "Expected one publisher and one subscriber raw file")

    samples_path = artifacts_dir / "subscriber-0.result.json"
    _require(samples_path.is_file(), f"Missing chart sample file: {samples_path}")
    samples = json.loads(samples_path.read_text(encoding="utf-8")).get("latencies_ms") or []
    _require(len(samples) > 0, "Chart sample list is empty")

    return {
        "result": "PASS",
        "schema": "PASS",
        "raw_artifacts_sha256": "PASS",
        "chart_samples": len(samples),
        "sent": run["messages_sent"],
        "received_final": run["unique_deliveries"],
        "latency_ms": run["latency_ms"],
        "latency_p95_ms": run["latency_p95_ms"],
        "latency_p99_ms": run["latency_p99_ms"],
        "throughput_mbps": run["throughput_mbps"],
        "final_packet_loss": run["final_packet_loss"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the one-run Fast DDS example")
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "results" / "one_example",
        help="suite directory containing result.json (or the result.json path itself)",
    )
    args = parser.parse_args(argv)
    try:
        result = verify(args.input)
    except (VerificationError, OSError, ValueError, KeyError) as exc:
        print(f"EXAMPLE VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 2
    print("EXAMPLE VERIFICATION PASS")
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
