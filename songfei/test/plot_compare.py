"""同场景、同参数下三种中间件的"主要性能指标随时间变化"对比图（一张图）。

数据来源（全部是各中间件自己落盘的**真实原始样本**，不做插值、不生成数据）：

- VSOA : ``<mw>/<S>/group<N>.json`` → ``artifacts_directory`` → ``subscriber-0.result.json``
         （逐样本 ``latencies_ms``）与 ``resource_samples.json``（逐采样资源）
- MQTT : ``MQTT/<S>/group<N>.json`` → ``samples_path`` 指向的逐样本文件（``latencies_ms``）
         与同目录 ``resources.jsonl``（逐采样资源，含 broker）
- Zenoh: ``zenoh/<S>/group<N>.json`` → ``samples``（``{sequence, latency, ...}``，毫秒）
- DDS  : ``DDS/<S>/artifacts/<run_id>/subscriber-0.result.json``（``latencies_ms``，毫秒）

时间轴口径：各家原始样本都不带统一的墙钟时间戳，因此**按测量窗口均匀铺开**
（第 i 个样本的 t = 窗口长度 × (i+0.5)/N），窗口取该组参数里的 ``duration_seconds``。
100 Hz 时 1 秒 ≈ 100 个样本，与参数吻合；跨中间件口径一致，便于横向比较。
若某家样本不是按到达顺序排列，曲线形状会有偏差 —— 这是本图唯一的近似点，标题里已注明。

用法::

    python songfei/test/plot_compare.py --scenario S01 --group 1
    python songfei/test/plot_compare.py --scenario S08 --group 1 --bin 5
    python songfei/test/plot_compare.py --scenario S07 --group 4 --middlewares vsoa,mqtt,zenoh
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

DATA_ROOT = Path(__file__).resolve().parent          # songfei/test
COLORS = {"vsoa": "#1f77b4", "mqtt": "#d62728", "zenoh": "#2ca02c", "dds": "#9467bd"}


# --------------------------------------------------------------------------- 读取
def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _group_file(middleware: str, scenario: str, group: int) -> Path:
    # Windows 上 MQTT / mqtt 是同一个目录，这里两者都试一遍
    for name in (middleware, middleware.upper()):
        path = DATA_ROOT / name / scenario / f"group{group}.json"
        if path.exists():
            return path
    raise SystemExit(f"找不到 {middleware} {scenario} 组{group} 的结果文件（是不是还没跑？）")


def _series_from_latencies(values) -> np.ndarray:
    return np.asarray([float(v) for v in values if isinstance(v, (int, float))], dtype=float)


def _resource_series(entries, time_key_candidates=("timestamp_ns", "t", "time_ns")) -> tuple:
    """把逐采样资源记录转成 (times_seconds_or_None, cpu, mem)。"""
    cpu, mem, stamps = [], [], []
    for index, row in enumerate(entries):
        if not isinstance(row, dict):
            continue
        cpu_value = row.get("cpu_percent", row.get("cpu"))
        mem_value = row.get("memory_mb", row.get("rss_mb"))
        if cpu_value is None and mem_value is None:
            continue
        cpu.append(float(cpu_value) if isinstance(cpu_value, (int, float)) else np.nan)
        mem.append(float(mem_value) if isinstance(mem_value, (int, float)) else np.nan)
        stamp = next((row.get(k) for k in time_key_candidates if isinstance(row.get(k), (int, float))), None)
        stamps.append(stamp if stamp is not None else index)
    if not cpu:
        return None, None, None
    stamps_array = np.asarray(stamps, dtype=float)
    times = (stamps_array - stamps_array[0]) / 1e9 if stamps_array.max() > 1e9 else None
    return times, np.asarray(cpu), np.asarray(mem)


def load_vsoa(scenario: str, group: int) -> dict:
    document = _load_json(_group_file("vsoa", scenario, group)) or {}
    artifact_dir = document.get("artifacts_directory")
    if not artifact_dir:
        return {"error": "该组没有 artifacts_directory（可能 status=error）"}
    base = DATA_ROOT / "vsoa" / scenario / artifact_dir
    subscriber = _load_json(base / "subscriber-0.result.json") or {}
    latencies = _series_from_latencies(subscriber.get("latencies_ms") or [])
    resources = _load_json(base / "resource_samples.json") or {}
    entries, scope_note = [], ""
    # 逐采样序列： samples[i] = {timestamp_ns, groups: {middleware: {...}, test_infrastructure: {...}}}
    # 只取 middleware 口径，否则会把测试基础设施的内存/CPU 混进来（total ≈ +42 MB）。
    for entry in resources.get("samples") or []:
        if not isinstance(entry, dict):
            continue
        groups = entry.get("groups")
        scope = groups.get("middleware") if isinstance(groups, dict) else None
        if not isinstance(scope, dict):                   # 兼容平铺的旧布局
            scope = entry if "cpu_percent" in entry else None
        if scope is None:
            continue
        entries.append({"timestamp_ns": entry.get("timestamp_ns"),
                        "cpu_percent": scope.get("cpu_percent"), "memory_mb": scope.get("memory_mb")})
    if entries:
        scope_note = "VSOA 只算中间件端点进程（文件里另有 test_infrastructure/total，未混入）"
    else:
        for value in resources.values():                  # 兜底：任意逐采样 list
            if isinstance(value, list) and value and isinstance(value[0], dict) and "cpu_percent" in value[0]:
                entries, scope_note = value, "VSOA 资源口径未细分"
                break
    times, cpu, mem = _resource_series(entries)
    return {"latencies": latencies, "res_times": times, "cpu": cpu, "mem": mem,
            "source": str(base / "subscriber-0.result.json"),
            "cpu_scope": scope_note or "VSOA 无逐采样资源"}


def load_mqtt(scenario: str, group: int) -> dict:
    document = _load_json(_group_file("mqtt", scenario, group)) or {}
    samples_path = document.get("samples_path")
    if not samples_path:
        return {"error": "该组没有 samples_path"}
    samples_file = DATA_ROOT / "MQTT" / scenario / samples_path
    samples = _load_json(samples_file) or {}
    values = samples.get("latencies_ms")
    if not values:
        values = [row.get("latency") for row in (samples.get("samples") or []) if isinstance(row, dict)]
    latencies = _series_from_latencies(values or [])
    entries = []
    resource_file = samples_file.parent / "resources.jsonl"
    if resource_file.exists():
        for line in resource_file.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    times, cpu, mem = _resource_series(entries)
    return {"latencies": latencies, "res_times": times, "cpu": cpu, "mem": mem,
            "source": str(samples_file), "cpu_scope": "MQTT 端点进程 + broker（口径比其它家宽）"}


def load_zenoh(scenario: str, group: int) -> dict:
    document = _load_json(_group_file("zenoh", scenario, group)) or {}
    samples = document.get("samples") or []
    latencies = _series_from_latencies(
        [row.get("latency") for row in samples if isinstance(row, dict)]
    )
    return {"latencies": latencies, "res_times": None, "cpu": None, "mem": None,
            "source": str(_group_file("zenoh", scenario, group)),
            "cpu_scope": "Zenoh 未落盘逐采样资源，本图不画"}


def load_dds(scenario: str, group: int) -> dict:
    """DDS 侧结果回传后即可画同一张图（目录结构与其它家一致）。"""
    pattern = str(DATA_ROOT / "DDS" / scenario / "artifacts" / "*" / "subscriber-0.result.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        return {"error": "还没有 DDS 的逐样本产物"}
    samples = _load_json(Path(files[0])) or {}
    latencies = _series_from_latencies(samples.get("latencies_ms") or [])
    return {"latencies": latencies, "res_times": None, "cpu": None, "mem": None,
            "source": files[0], "cpu_scope": "DDS 逐采样资源未纳入本图"}


LOADERS = {"vsoa": load_vsoa, "mqtt": load_mqtt, "zenoh": load_zenoh, "dds": load_dds}


# --------------------------------------------------------------------------- 统计
def binned(latencies: np.ndarray, window: float, bin_seconds: float) -> dict:
    """按时间窗（窗口长度均匀铺开）分箱，返回每箱的均值 / P95 / 最大值 / 样本数。"""
    count = len(latencies)
    edges = np.arange(0.0, window + bin_seconds, bin_seconds)
    if len(edges) < 2:
        edges = np.array([0.0, window])
    index = np.clip(((np.arange(count) + 0.5) / count) * window // bin_seconds, 0, len(edges) - 2)
    index = index.astype(int)
    centers, means, p95s, maxima, counts = [], [], [], [], []
    for slot in range(len(edges) - 1):
        mine = latencies[index == slot]
        centers.append((edges[slot] + edges[slot + 1]) / 2)
        counts.append(mine.size)
        means.append(float(np.mean(mine)) if mine.size else np.nan)
        p95s.append(float(np.percentile(mine, 95)) if mine.size else np.nan)
        maxima.append(float(np.max(mine)) if mine.size else np.nan)
    return {"centers": np.asarray(centers), "mean": np.asarray(means),
            "p95": np.asarray(p95s), "max": np.asarray(maxima), "count": np.asarray(counts)}


def main() -> int:
    parser = argparse.ArgumentParser(description="同参数下多中间件指标随时间对比图")
    parser.add_argument("--scenario", required=True, help="S01..S08")
    parser.add_argument("--group", type=int, required=True, help="1..4")
    parser.add_argument("--middlewares", default="vsoa,mqtt,zenoh")
    parser.add_argument("--bin", type=float, default=1.0, help="时间箱宽度（秒），默认 1")
    parser.add_argument("--out", default=None, help="输出 png 路径")
    args = parser.parse_args()

    names = [item.strip().lower() for item in args.middlewares.split(",") if item.strip()]
    loaded, reference = {}, None
    for name in names:
        if name not in LOADERS:
            raise SystemExit(f"未知中间件：{name}")
        info = LOADERS[name](args.scenario, args.group)
        loaded[name] = info
        if reference is None and info.get("latencies") is not None and len(info["latencies"]):
            document = _load_json(_group_file(name, args.scenario, args.group)) or {}
            reference = (document.get("_matrix") or {}).get("parameters") or {}

    if reference is None:
        raise SystemExit("三家都没有可用的逐样本数据，无法出图")

    window = float(reference.get("duration_seconds") or 10.0)
    payload = int(reference.get("payload_size_bytes") or 1024)
    rate = float(reference.get("publish_rate_hz") or 0)
    offered = rate * payload * 8 / 1e6 if rate else None

    figure, axes = plt.subplots(2, 2, figsize=(15, 10))
    title = (f"{args.scenario} 组{args.group} · 同一参数下三种中间件主要指标随时间变化\n"
             f"payload={payload} B · rate={rate or '不限速'} Hz · 窗口={window:g} s · "
             f"P×S={reference.get('publisher_count')}×{reference.get('subscriber_count')}")
    figure.suptitle(title, fontsize=14)

    for name in names:
        info = loaded[name]
        color = COLORS.get(name, None)
        latencies = info.get("latencies")
        if latencies is None or len(latencies) == 0:
            for axis in axes.ravel():
                axis.plot([], [], label=f"{name.upper()}（无样本：{info.get('error', '数据为空')}）",
                          color=color)
            continue
        stats = binned(latencies, window, args.bin)
        times = (np.arange(len(latencies)) + 0.5) / len(latencies) * window

        axes[0][0].plot(times, latencies, color=color, alpha=0.18, linewidth=0.6)
        axes[0][0].plot(stats["centers"], stats["mean"], color=color, linewidth=2,
                        label=f"{name.upper()} 均值（n={len(latencies)}）")
        axes[0][1].plot(stats["centers"], stats["p95"], color=color, linewidth=2,
                        label=f"{name.upper()} P95")
        axes[0][1].plot(stats["centers"], stats["max"], color=color, linewidth=1.2, linestyle="--",
                        alpha=0.8, label=f"{name.upper()} 最大")
        axes[1][0].plot(stats["centers"], stats["count"] * payload * 8 / args.bin / 1e6,
                        color=color, linewidth=2, label=f"{name.upper()} 实收吞吐")

        if info.get("cpu") is not None:
            res_times = info["res_times"]
            if res_times is None:
                res_times = np.linspace(0, window, len(info["cpu"]))
            axes[1][1].plot(res_times, info["cpu"], color=color, linewidth=1.8,
                            label=f"{name.upper()} CPU%（{info.get('cpu_scope')}）")
            axes[1][1].plot(res_times, info["mem"], color=color, linewidth=1.2, linestyle=":",
                            alpha=0.85, label=f"{name.upper()} 内存 MB")

    axes[0][0].set(title="延迟随时间（淡色=逐样本，粗线=时间箱均值）", xlabel="时间 (s)", ylabel="延迟 (ms)")
    axes[0][1].set(title=f"延迟尾部随时间（{args.bin:g} s 箱）", xlabel="时间 (s)", ylabel="延迟 (ms)")
    axes[1][0].set(title=f"实收吞吐随时间（{args.bin:g} s 箱）", xlabel="时间 (s)", ylabel="Mbps")
    axes[1][1].set(title="资源占用随时间（逐采样）", xlabel="时间 (s)", ylabel="CPU% / 内存 MB")
    if offered:
        axes[1][0].axhline(offered, color="grey", linestyle="--", linewidth=1.2,
                           label=f"目标（按参数）= {offered:.2f} Mbps")
    for axis in axes.ravel():
        axis.grid(alpha=0.3)
        axis.legend(fontsize=8, loc="best")
        if not axis.get_legend_handles_labels()[0]:
            axis.set_axis_off()

    figure.text(0.5, 0.005,
                "时间轴口径：逐样本按测量窗口均匀铺开（样本不带统一墙钟时间戳）；"
                "CPU 口径各家不同（见图例）。数据全部来自真实落盘样本，未插值、未生成。",
                ha="center", fontsize=8.5, color="#555555")
    figure.tight_layout(rect=(0, 0.02, 1, 0.94))

    out_path = Path(args.out) if args.out else (DATA_ROOT / f"compare_{args.scenario}_G{args.group}.png")
    figure.savefig(out_path, dpi=150)
    print(f"[图] {out_path}")
    for name in names:
        info = loaded[name]
        size = 0 if info.get("latencies") is None else len(info["latencies"])
        print(f"  {name:6s} 样本={size:6d}  来源={info.get('source') or info.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
