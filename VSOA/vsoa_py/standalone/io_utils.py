"""Atomic UTF-8 artifacts and bounded control-file waits."""

import json
import os
import time
import uuid
from pathlib import Path


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for attempt in range(100):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.01)


def read_json(path):
    for attempt in range(100):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.01)


def wait_file(path, timeout=120):
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Control file not received: {path}")
        time.sleep(0.005)
    return read_json(path)


def wait_until_ns(target):
    while True:
        remaining = (target - time.perf_counter_ns()) / 1e9
        if remaining <= 0:
            return
        time.sleep(max(0, remaining - 0.0005) if remaining > 0.001 else 0)
