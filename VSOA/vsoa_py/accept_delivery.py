"""Exercise the shipped exe under a Python-free PATH and retain auditable acceptance artifacts."""

import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import yaml

from standalone.io_utils import write_json
from standalone.validate_delivery import validate_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path("release/vsoa_win64_v1.2"))
    args = parser.parse_args()
    package = args.package.resolve()
    root = Path("results").resolve() / ("standalone-acceptance-" + uuid.uuid4().hex[:8])
    isolated = root / "\u72ec\u7acb\u8fd0\u884c \u9a8c\u6536 with spaces" / package.name
    isolated.mkdir(parents=True)
    for source in package.iterdir():
        if source.is_file():
            shutil.copy2(source, isolated / source.name)
    executable = isolated / "vsoa.exe"
    binary = executable.read_bytes()
    pe_offset = struct.unpack_from("<I", binary, 0x3c)[0]
    machine = struct.unpack_from("<H", binary, pe_offset + 4)[0]
    assert binary[pe_offset:pe_offset + 4] == b"PE\0\0" and machine == 0x8664
    environment = dict(os.environ)
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
    environment["PYTHONHOME"] = str(root / "python-does-not-exist")
    environment["PYTHONPATH"] = str(root / "no-site-packages")
    environment.pop("VIRTUAL_ENV", None)
    environment["VSOA_NO_PAUSE"] = "1"
    checks = []
    observed_names = set()
    embedded_python_paths = set()

    def run(name, arguments, expected_exit=0, timeout=240, batch=False):
        command = [str(executable), *arguments]
        if batch:
            command_shell = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
            batch_arguments = subprocess.list2cmdline(arguments)
            command = f'"{command_shell}" /d /s /c ""{isolated / "run.bat"}" {batch_arguments}"'
        with open(root / f"{name}.log", "wb") as log:
            process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() > deadline:
                    family = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(family):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    process.kill()
                    raise TimeoutError(name)
                try:
                    family = psutil.Process(process.pid).children(recursive=True)
                    for child in family:
                        observed_names.add(child.name().lower())
                        if name == "default-five-scenarios" and not embedded_python_paths:
                            for mapping in child.memory_maps():
                                if mapping.path.lower().endswith("python313.dll"):
                                    embedded_python_paths.add(mapping.path)
                except psutil.Error:
                    pass
                time.sleep(0.1)
            assert process.returncode == expected_exit, (name, process.returncode, (root / f"{name}.log").read_text(encoding="utf-8", errors="replace")[-3000:])
        checks.append({"name": name, "passed": True, "exit_code": process.returncode})
        print(f"PASS {name}", flush=True)

    def configured(name, config, scenario):
        config_path = isolated / f"{name}.yaml"
        config["output_directory"] = f"outputs/{name}"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        run(name, ["--config", str(config_path), "--scenario", scenario])
        result = json.loads((isolated / config["output_directory"] / "result.json").read_text(encoding="utf-8"))
        validate_result(result)
        return result

    run("version", ["--version"])
    run("config-validation", ["--validate-config"])
    run("default-five-scenarios", [])
    default_result = json.loads((isolated / "outputs/result.json").read_text(encoding="utf-8"))
    validate_result(default_result, all_scenarios=True)
    assert len(default_result["runs"]) == 25
    assert "python.exe" not in observed_names and "pythonw.exe" not in observed_names
    assert embedded_python_paths and all("_mei" in path.lower() for path in embedded_python_paths)
    config = yaml.safe_load((isolated / "config.yaml").read_text(encoding="utf-8"))
    short = copy.deepcopy(config)
    short.update(repeats=1, duration_seconds=0.3)
    multi = copy.deepcopy(short)
    multi["scenarios"]["multi_subscriber_broadcast"].update(publisher_count=2, subscriber_count=3)
    result = configured("multi-publisher", multi, "multi_subscriber_broadcast")
    assert result["runs"][0]["expected_deliveries"] == result["runs"][0]["messages_sent"] * 3
    for label, loss, recovery in (("loss-zero", 0.0, False), ("loss-all-no-recovery", 1.0, False), ("loss-all-with-recovery", 1.0, True)):
        candidate = copy.deepcopy(short)
        candidate["scenarios"]["weak_network_recovery"].update(loss_rate=loss, recovery_enabled=recovery)
        result = configured(label, candidate, "weak_network_recovery")
        row = result["runs"][0]
        if loss == 1:
            assert row["packet_loss"] == 1 and row["latency_ms"] is None
            assert row["final_packet_loss"] == (0 if recovery else 1)
        else:
            assert row["packet_loss"] == 0
    invalid = copy.deepcopy(short)
    invalid["publisher_count"] = 0
    invalid_path = isolated / "invalid.yaml"
    invalid_path.write_text(yaml.safe_dump(invalid), encoding="utf-8")
    run("invalid-config", ["--config", str(invalid_path)], expected_exit=2)
    duplicate_path = isolated / "duplicate.yaml"
    duplicate_path.write_text((isolated / "config.yaml").read_text(encoding="utf-8") + "\nmodule_name: vsoa\n", encoding="utf-8")
    run("duplicate-config", ["--config", str(duplicate_path)], expected_exit=2)
    conflict = copy.deepcopy(short)
    conflict["output_directory"] = "outputs/occupied-port"
    conflict_path = isolated / "occupied.yaml"
    conflict_path.write_text(yaml.safe_dump(conflict), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", config["port"]))
        listener.listen(1)
        run("occupied-port", ["--config", str(conflict_path), "--scenario", "small_message_latency"], expected_exit=1)
    assert json.loads((isolated / "outputs/occupied-port/result.json").read_text(encoding="utf-8"))["status"] == "error"
    batch = copy.deepcopy(short)
    batch["output_directory"] = "outputs/batch-run"
    batch_path = isolated / "batch.yaml"
    batch_path.write_text(yaml.safe_dump(batch), encoding="utf-8")
    run("batch-launch", ["--config", str(batch_path), "--scenario", "small_message_latency"], batch=True)
    leftovers = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        if process.info["exe"] and Path(process.info["exe"]).resolve() == executable:
            leftovers.append(process.info["pid"])
    assert not leftovers, leftovers
    for name in ("result.json", "history", "runs", "artifacts"):
        source = isolated / "outputs" / name
        target = package / "outputs" / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    shutil.copytree(isolated / "logs", package / "logs", dirs_exist_ok=True)
    report = {"passed": True, "exe_sha256": hashlib.sha256(binary).hexdigest(), "pe_machine": "AMD64 0x8664",
              "default_scenario_count": 5, "default_repeat_count": 5, "default_run_count": 25,
              "test_environment": "PATH has only Windows system directories; nonexistent PYTHONHOME/PYTHONPATH; no virtualenv variable",
              "clean_windows_vm_tested": False, "observed_child_executables": sorted(observed_names),
              "embedded_python_dlls": sorted(embedded_python_paths), "checks": checks,
              "orphan_processes": leftovers, "evidence_directory": str(root)}
    write_json(root / "acceptance.json", report)
    write_json(package / "acceptance.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
