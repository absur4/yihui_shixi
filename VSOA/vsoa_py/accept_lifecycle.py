"""Windows hidden-console cancellation and owned-process cleanup acceptance."""

import ctypes
import json
import os
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import psutil
import yaml

from standalone.io_utils import write_json


def main():
    package = Path("release/vsoa_win64_v1.2").resolve()
    executable = package / "vsoa.exe"
    root = Path("results").resolve() / ("lifecycle-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    config = yaml.safe_load((package / "config.yaml").read_text(encoding="utf-8"))
    config.update(duration_seconds=10, repeats=1)
    results = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]

    for mode in ("graceful", "forced"):
        current = {**config, "output_directory": str(root / mode)}
        config_path = root / f"{mode}.yaml"
        config_path.write_text(yaml.safe_dump(current), encoding="utf-8")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        with open(root / f"{mode}.log", "wb") as log:
            process = subprocess.Popen([str(executable), "--config", str(config_path), "--scenario", "small_message_latency"],
                                       stdout=log, stderr=log, startupinfo=startup,
                                       creationflags=subprocess.CREATE_NEW_CONSOLE)
            deadline = time.monotonic() + 30
            while not list((root / mode / "artifacts").glob("*/start.json")):
                assert process.poll() is None, "Controller exited before startup"
                if time.monotonic() > deadline:
                    process.kill()
                    raise TimeoutError("Lifecycle test startup")
                time.sleep(0.05)
            family = psutil.Process(process.pid).children(recursive=True)
            owned_workers = [child for child in family if "--internal-worker" in child.cmdline()]
            assert len(owned_workers) >= 3
            if mode == "graceful":
                kernel.FreeConsole()
                assert kernel.AttachConsole(process.pid), ctypes.get_last_error()
                kernel.SetConsoleCtrlHandler(None, True)
                try:
                    assert kernel.GenerateConsoleCtrlEvent(0, 0), ctypes.get_last_error()
                    process.wait(timeout=15)
                finally:
                    kernel.FreeConsole()
                report = json.loads((root / mode / "result.json").read_text(encoding="utf-8"))
                assert report["status"] == "cancelled", report["status"]
            else:
                controllers = [child for child in family if child.name().lower() == "vsoa.exe"
                               and "--internal-worker" not in child.cmdline()]
                target = controllers[-1] if controllers else psutil.Process(process.pid)
                target.kill()
                process.wait(timeout=15)
                report = json.loads((root / mode / "result.json").read_text(encoding="utf-8"))
                assert report["status"] == "running", report["status"]
            gone, alive = psutil.wait_procs(owned_workers, timeout=10)
            assert not alive, [child.pid for child in alive]
            results.append({"mode": mode, "passed": True, "controller_exit_code": process.returncode,
                            "result_status": report["status"], "owned_workers_cleaned": len(gone)})
    result = {"passed": True, "checks": results, "evidence_directory": str(root),
              "method": "hidden console Ctrl+C; separately kill actual controller to verify Windows Job cleanup"}
    write_json(root / "report.json", result)
    write_json(package / "lifecycle_acceptance.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
