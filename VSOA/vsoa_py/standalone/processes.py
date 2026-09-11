"""Process ownership, bounded waits and Windows close-window cleanup."""

import contextlib
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import psutil

from standalone.io_utils import read_json, write_json


class WindowsJob:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                        ("maximum_working_set", ctypes.c_size_t), ("active_process_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IOCounters), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                        ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessGroup:
    def __init__(self, folder, logs):
        self.folder = Path(folder)
        self.logs = Path(logs)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.children = []
        self.job = WindowsJob()
        self.closed = False
        self.lock = threading.Lock()

    def start(self, spec):
        with self.lock:
            if self.closed:
                raise RuntimeError("Process group is closed")
            name = f"{spec['kind']}-{spec.get('identifier', 0)}"
            spec = {**spec, "folder": str(self.folder)}
            path = self.folder / f"{name}.spec.json"
            write_json(path, spec)
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(Path(__file__).resolve().parents[1] / "vsoa_module.py"))
            command += ["--internal-worker", str(path)]
            log = open(self.logs / f"{name}.log", "w", encoding="utf-8")
            environment = dict(os.environ, PYTHONUTF8="1")
            process = subprocess.Popen(command, stdout=log, stderr=log, env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.children.append((process, log, name))
            try:
                self.job.assign(process)
            except Exception:
                process.kill()
                process.wait(timeout=3)
                raise
            return process

    def check(self):
        if self.closed:
            raise TimeoutError("Run watchdog closed the worker group")
        errors = list(self.folder.glob("*.error.json"))
        if errors:
            raise RuntimeError(f"Worker error: {read_json(errors[0])}")
        for process, log, name in self.children:
            if process.poll() is not None and not (process.returncode == 0 and (self.folder / "finish.json").exists()):
                raise RuntimeError(f"Worker exited unexpectedly: {name}, code={process.returncode}; see {self.logs}")

    def stop_owned(self, process):
        entry = next(item for item in self.children if item[0] is process)
        family = []
        with contextlib.suppress(psutil.Error):
            parent = psutil.Process(process.pid)
            family = parent.children(recursive=True) + [parent]
        for child in reversed(family):
            with contextlib.suppress(psutil.Error):
                child.kill()
        psutil.wait_procs(family, timeout=3)
        process.wait(timeout=3)
        self.children.remove(entry)
        entry[1].close()

    def wait(self, path, timeout):
        deadline = time.monotonic() + timeout
        while not Path(path).exists():
            self.check()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Worker timeout: {Path(path).name}")
            time.sleep(0.01)
        return read_json(path)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            family = []
            for process, log, name in self.children:
                with contextlib.suppress(psutil.Error):
                    parent = psutil.Process(process.pid)
                    family.extend(parent.children(recursive=True))
                    family.append(parent)
            self.job.close()
            for child in reversed(family):
                with contextlib.suppress(psutil.Error):
                    child.terminate()
            gone, alive = psutil.wait_procs(family, timeout=3)
            for child in alive:
                with contextlib.suppress(psutil.Error):
                    child.kill()
            for process, log, name in self.children:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3)
                log.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
