"""Standalone Windows entry point; internal services reuse this executable."""

import argparse
import contextlib
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path


@contextlib.contextmanager
def output_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".run.lock", "a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another run owns this output directory") from error
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-worker":
        from standalone.workers import run_worker
        run_worker(sys.argv[2])
        return 0
    if hasattr(signal, "SIGBREAK"):
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, interrupted)
    from standalone.configuration import SCENARIOS, load_config
    from standalone.engine import run_suite
    from standalone.version import VERSION
    from standalone.qualification import qualification_cases, PHASES

    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "delivery_template"
    parser = argparse.ArgumentParser(description=f"VSOA Windows standalone middleware benchmark v{VERSION}")
    parser.add_argument("--config", type=Path, default=base / "config.yaml")
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--suite", choices=("smoke", "qualification"), default="smoke")
    parser.add_argument("--phase", choices=("all", *PHASES), default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, help="Override output directory (relative to config directory)")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--version", action="version", version=f"vsoa module {VERSION} / result schema 2.1")
    options = parser.parse_args()
    try:
        config, cases = load_config(options.config)
        plan = []
        if options.suite == "qualification":
            cases, plan = qualification_cases(config, options.phase)
        if options.scenario != "all" and options.scenario not in {case["scenario_name"] for case in cases}:
            raise ValueError("Unknown or unsupported scenario")
    except Exception as error:
        print(f"CONFIG ERROR: {error}", file=sys.stderr)
        return 2
    if options.list_scenarios:
        print(json.dumps(plan or [case["scenario_name"] for case in cases], indent=2))
        return 0
    config_dir = options.config.resolve().parent
    output = options.output or Path(config["output_directory"])
    output = output if output.is_absolute() else config_dir / output
    output = output.resolve()
    if options.validate_config:
        print(json.dumps({"valid": True, "configuration": config, "effective_scenarios": cases,
                          "output_directory": str(output)}, ensure_ascii=False, indent=2))
        return 0
    logs = config_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        with output_lock(output), open(logs / config["log_file"], "a", encoding="utf-8") as handle:
            def log(message):
                line = f"{datetime.now(timezone.utc).isoformat()} {message}"
                print(line, flush=True)
                handle.write(line + "\n")
                handle.flush()

            report = run_suite(config, cases, output, logs, log, options.scenario, plan, options.resume)
            log(f"RESULT {output / 'result.json'} status={report['status']}")
            return 0 if report["status"] == "completed" else 130 if report["status"] == "cancelled" else 1
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) == 1 and sys.stdin.isatty():
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("Finished. Press Enter to close...")
    raise SystemExit(exit_code)
