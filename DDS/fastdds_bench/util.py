from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def safe_id(value: str, max_length: int = 96) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")
    return (cleaned or "unnamed")[:max_length]


def atomic_write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def wait_until_ns(target_ns: int, spin_threshold_us: int = 100) -> None:
    spin_ns = max(0, int(spin_threshold_us)) * 1_000
    while True:
        remaining = target_ns - time.perf_counter_ns()
        if remaining <= 0:
            return
        if remaining > spin_ns + 1_000_000:
            time.sleep((remaining - spin_ns) / 1_000_000_000)
        elif spin_ns == 0:
            time.sleep(remaining / 1_000_000_000)
        else:
            # A short spin reduces scheduling error. Its CPU cost is intentionally
            # included in the endpoint resource measurement.
            pass


def ns_to_ms(value_ns: int | float | None) -> float | None:
    if value_ns is None:
        return None
    return float(value_ns) / 1_000_000.0


def relative_posix(path: Path, base: Path = PROJECT_ROOT) -> str:
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return path.as_posix()

