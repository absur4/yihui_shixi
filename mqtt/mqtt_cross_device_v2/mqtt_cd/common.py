from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid


DEFAULT_CASE = dict(scenario="CD1", level="100Hz", direction="A_to_B", rate_hz=100,
                    payload_bytes=1024, publishers=1, subscribers=1, duration_s=20,
                    warmup_s=5, settle_s=2, drain_s=2, repeat=1, qos=0,
                    queue_messages=1024, queue_bytes=64*1024*1024,
                    connect_timeout_s=30, sample_interval_s=.2,
                    memory_limit_fraction=.5, random_seed=20260916,
                    profile="sender_service_tcp_v2")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fd, temp = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_id(value):
    text = str(value)
    if str(uuid.UUID(text)) != text:
        raise ValueError("run_id must be a canonical UUID")
    return text


def token():
    value = os.environ.get("MQTT_CD_TOKEN", "")
    if len(value) < 24 or any(c.isspace() for c in value):
        raise ValueError("Set MQTT_CD_TOKEN to a shared token of at least 24 non-space characters")
    return value


def validate_case(case):
    cfg = dict(DEFAULT_CASE, **case)
    for key in ("rate_hz", "payload_bytes", "publishers", "subscribers", "repeat",
                "queue_messages", "queue_bytes", "random_seed", "qos"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    if not 1 <= cfg["rate_hz"] <= 1_000_000 or not 1 <= cfg["payload_bytes"] <= 1024*1024:
        raise ValueError("rate_hz outside 1..1000000 or payload outside 1..1048576")
    if cfg["publishers"] != 1 or not 1 <= cfg["subscribers"] <= 4:
        raise ValueError("CD1-CD4 support one publisher and 1..4 subscribers")
    if cfg["qos"] not in (0, 1, 2):
        raise ValueError("qos must be 0, 1 or 2")
    if cfg["qos"] != 0 and cfg.get("profile") == "sender_service_tcp_v2":
        raise ValueError("QoS1/2 require a separate profile, e.g. mqtt_qos1_tcp_v2")
    if not 1 <= cfg["repeat"] <= 100 or not 1 <= cfg["queue_messages"] <= 100000:
        raise ValueError("invalid repeat or queue_messages")
    if not cfg["payload_bytes"] <= cfg["queue_bytes"] <= 1024**3:
        raise ValueError("queue_bytes must accommodate payload and be <=1GiB")
    for key in ("duration_s", "warmup_s", "settle_s", "drain_s", "connect_timeout_s",
                "sample_interval_s", "memory_limit_fraction"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number")
    if not 0 < cfg["duration_s"] <= 3600 or not 0 <= cfg["warmup_s"] <= 60:
        raise ValueError("invalid measurement or warmup duration")
    if not 0 <= cfg["settle_s"] <= 30 or not 0 <= cfg["drain_s"] <= 60:
        raise ValueError("invalid settle/drain duration")
    if not .05 <= cfg["sample_interval_s"] <= 5 or not 0 < cfg["memory_limit_fraction"] <= .8:
        raise ValueError("invalid sampling or memory limit")
    if not 1 <= cfg["connect_timeout_s"] <= 120:
        raise ValueError("connect timeout outside 1..120 seconds")
    if cfg["direction"] not in ("A_to_B", "B_to_A"):
        raise ValueError("direction must be A_to_B or B_to_A")
    if cfg["scenario"] not in ("CD1", "CD2", "CD3", "CD4-C", "CD4-L", "CD4-H", "SMOKE"):
        raise ValueError("invalid cross-device scenario")
    for key in ("level", "profile"):
        if not isinstance(cfg[key], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", cfg[key]):
            raise ValueError(f"{key} must be an ASCII identifier")
    if "run_id" in cfg:
        run_id(cfg["run_id"])
    return cfg


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def source_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def safe_member(name):
    parts = name.replace("\\", "/").split("/")
    if not name or name.startswith(("/", "\\")) or any(p in ("", ".", "..") or ":" in p for p in parts):
        raise ValueError("unsafe archive member")
    return parts
