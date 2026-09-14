from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import load_config, select_conditions
from .rawio import export_raw_csv
from .results import summarize_runs
from .util import PROJECT_ROOT, atomic_write_json, read_json


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def command_check(args: argparse.Namespace) -> int:
    config, warnings = load_config(args.config)
    selected = select_conditions(config, args.condition)
    if args.config_only:
        probe = None
    else:
        from .controller import probe_fastdds_runtime

        probe = probe_fastdds_runtime()
    result = {
        "config_ok": True,
        "enabled_condition_count": len(selected),
        "planned_run_count": sum(int(c["repeats"]) for c in selected),
        "warnings": warnings,
        "runtime_probe": probe,
    }
    _print_json(result)
    return 0 if probe is None or probe["ok"] else 2


def command_run(args: argparse.Namespace) -> int:
    from .controller import run_suite
    from .schema_validation import validate_result

    suite = run_suite(
        args.config,
        args.output,
        condition_ids=args.condition,
        repeats_override=args.repeats,
        overwrite=args.overwrite,
    )
    validate_result(suite)
    passed = sum(run["status"] == "passed" for run in suite["runs"])
    failed = len(suite["runs"]) - passed
    print(f"Result: {args.output}")
    print(f"Runs: {len(suite['runs'])}, passed: {passed}, failed: {failed}")
    if len(suite["runs"]) == 1:
        run = suite["runs"][0]
        for key in (
            "latency_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "jitter_ms",
            "throughput_mbps",
            "cpu_percent",
            "memory_mb",
            "final_packet_loss",
            "startup_time_ms",
            "discovery_time_ms",
        ):
            print(f"{key}: {run.get(key)}")
    return 0 if failed == 0 else 3


def command_validate(args: argparse.Namespace) -> int:
    from .schema_validation import validate_result

    document = read_json(args.input)
    validate_result(document, args.schema)
    print(f"Schema validation passed: {args.input}")
    return 0


def command_export_raw(args: argparse.Namespace) -> int:
    count = export_raw_csv(args.input, args.output)
    print(f"Exported {count} records to {args.output}")
    return 0


def command_resummarize(args: argparse.Namespace) -> int:
    from .schema_validation import validate_result

    document = read_json(args.input)
    document["summary"] = summarize_runs(document["runs"])
    output = args.output or args.input
    atomic_write_json(output, document)
    validate_result(document)
    print(f"Summary rebuilt from run results: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified Fast DDS benchmark implemented in Python"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate config and runtime")
    check.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.example.yaml")
    check.add_argument("--condition", action="append")
    check.add_argument("--config-only", action="store_true")
    check.set_defaults(func=command_check)

    run = subparsers.add_parser("run", help="run one or more benchmark conditions")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--condition", action="append")
    run.add_argument("--repeats", type=int)
    run.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace an existing suite JSON (raw run folders are retained)",
    )
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="validate a suite JSON")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument(
        "--schema",
        type=Path,
        default=PROJECT_ROOT / "schemas" / "unified_result.schema.json",
    )
    validate.set_defaults(func=command_validate)

    export = subparsers.add_parser("export-raw", help="export binary observations to CSV")
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.set_defaults(func=command_export_raw)

    summarize = subparsers.add_parser(
        "resummarize", help="rebuild summary only from existing run results"
    )
    summarize.add_argument("--input", type=Path, required=True)
    summarize.add_argument("--output", type=Path)
    summarize.set_defaults(func=command_resummarize)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
