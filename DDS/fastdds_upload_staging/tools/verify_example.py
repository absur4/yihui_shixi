from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastdds_bench.schema_validation import validate_result  # noqa: E402
from fastdds_bench.util import read_json, sha256_file  # noqa: E402


class VerificationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def verify(path: Path) -> dict[str, object]:
    document = read_json(path)
    validate_result(document)
    runs = document.get("runs", [])
    _require(len(runs) == 1, f"Expected exactly one run, found {len(runs)}")
    run = runs[0]
    _require(run["condition_id"] == "EXAMPLE_S01_1k_100", "Wrong example condition")
    _require(run["status"] == "passed", f"Run failed: {run.get('errors')}")
    _require(run["sent_success_count"] == 200, "Expected 200 successful sends")
    _require(run["expected_delivery_count"] == 200, "Expected 200 deliveries")
    _require(
        run["valid_unique_delivery_count"] == 200,
        "Not all 200 messages arrived after drain",
    )
    _require(run["final_packet_loss"] == 0, "Final packet loss is not zero")
    for field in (
        "duplicate_count",
        "out_of_order_count",
        "corrupted_count",
        "unexpected_count",
        "clock_anomaly_count",
        "metadata_mismatch_count",
    ):
        _require(run.get(field) == 0, f"{field} is not zero: {run.get(field)}")
    _require(run["latency_sample_count"] > 0, "No valid latency sample")
    _require(run["latency_ms"] is not None, "latency_ms is null")
    _require(run["throughput_mbps"] is not None, "throughput_mbps is null")
    _require(run["cpu_percent"] is not None, "cpu_percent is null")
    _require(run["memory_mb"] is not None, "memory_mb is null")

    checked_artifacts = 0
    for key in ("publisher_raw", "subscriber_raw"):
        for artifact in run["artifacts"].get(key, []):
            artifact_path = Path(artifact["path"])
            if not artifact_path.is_absolute():
                artifact_path = PROJECT_ROOT / artifact_path
            _require(artifact_path.is_file(), f"Missing raw artifact: {artifact_path}")
            _require(
                sha256_file(artifact_path) == artifact["sha256"],
                f"SHA-256 mismatch: {artifact_path}",
            )
            checked_artifacts += 1
    _require(checked_artifacts == 2, "Expected one publisher and one subscriber raw file")

    return {
        "result": "PASS",
        "schema": "PASS",
        "raw_artifacts_sha256": "PASS",
        "sent": run["sent_success_count"],
        "received_final": run["valid_unique_delivery_count"],
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
        default=PROJECT_ROOT / "outputs" / "one_example.json",
    )
    args = parser.parse_args(argv)
    try:
        result = verify(args.input)
    except (VerificationError, OSError, ValueError) as exc:
        print(f"EXAMPLE VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 2
    print("EXAMPLE VERIFICATION PASS")
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

