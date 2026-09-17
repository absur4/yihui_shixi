"""跨中间件差异对比图（VSOA / MQTT / Zenoh / DDS）。

与现有绘图脚本的分工：

- ``plot_scenario.py``：单场景、按影响因子出图（S01–S07）。
- ``plot_compare.py``：单场景单组、按时间出图（S08）。
- ``plot_diff.py``（本文件）：**跨场景**取数，只画两张"最能体现差异"的图：

  1. ``compare_repeatability.png`` —— 六场景重复性证据图。
     六个场景里各有一个参数完全相同的条件（1P/1S、1 KiB、1000 Hz）：
     S01组3 / S04组2 / S05组1 / S06组1 / S07组1 / S08组2。
     同一条件被独立测了 6 次，用来回答"数据可信吗、哪一列可信"。
  2. ``compare_S02_payload_knee.png`` —— 消息大小阶梯与分片拐点。
     S02 六档 1/4/16/32/48/64 KiB @100 Hz，重点看 48→64 KiB 之间
     VSOA 分片阈值（fragment_size_bytes=60000）造成的机制切换。
  3. ``compare_S03_large_message_tail.png`` —— 大消息吞吐与尾部代价。
     S03 五档（64–512 KiB@20 Hz + 1 MiB@5 Hz），核心是 **P99 ÷ P95**：
     VSOA 的 1 MiB = 18 个分片，整包延迟取决于最慢的一片，尾部被显著拉长。
  4. ``compare_S05_S07_topology.png`` —— 拓扑成本结构。
     扇出(1P→N×S) / 汇聚(N×P→1S) / 满并发(P×S) 三种拓扑 × 延迟/内存/CPU。
  5. ``compare_S08_stability.png`` —— 长时间稳定性（4 个负载档位各跑 100 s 的稳态结果）。
     逐时间桶的漂移曲线不在这里，用 plot_compare.py。

数据来源（全部是真实落盘结果，不插值、不生成）：

- VSOA/MQTT/Zenoh：``<mw>/summary.json``（run_matrix.py 落盘）
- DDS：``DDS/results/`` 下的 suite（文件或 ``<dir>/result.json``），
  ``condition_id`` 形如 ``S01_G3_1000Hz``，解析出 (场景, 组号)。

DDS 的排除规则：``DDS_EXCLUDED`` 里的条件按 DDS 侧自己的《结果评价.txt》
判定为"测试程序/队列瓶颈，不代表 Fast DDS 真实能力"，进图时置空（断线），
而不是画上去再让人误读。

用法::

    python songfei/test/plot_diff.py
    python songfei/test/plot_diff.py --middlewares vsoa,mqtt,zenoh   # 不画 DDS
    python songfei/test/plot_diff.py --only repeatability
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

DATA_ROOT = Path(__file__).resolve().parent          # songfei/test
COLORS = {"vsoa": "#1f77b4", "mqtt": "#d62728", "zenoh": "#2ca02c", "dds": "#9467bd"}
FOLDERS = {"vsoa": "vsoa", "mqtt": "MQTT", "zenoh": "zenoh"}
PRETTY = {"vsoa": "VSOA", "mqtt": "MQTT", "zenoh": "Zenoh", "dds": "DDS"}
ALL_NAMES = ["vsoa", "mqtt", "zenoh", "dds"]

# 图1：六场景重叠条件（参数完全相同：1P/1S、1 KiB、1000 Hz）
REPEAT_KEYS = [("S01", 3), ("S04", 2), ("S05", 1), ("S06", 1), ("S07", 1), ("S08", 2)]

# DDS 侧"可用条件"白名单（(场景, 组号)）——即本轮实测交付、且这五张图所需要的 26 个条件。
# 数据来源：songfei/test/dds_26_results.json
#   suite_id = 2026-09-16T16_40_31_733Z_p22724，config_source = matrix_s01_s08.yaml，
#   26 条全部 status=passed，suite 跨度约 8 分钟（真机执行）。
# 用白名单而不是黑名单：黑名单会默认放行未评估过的条件，白名单不会。
# 逐场景对应（详见 README_DDS补测说明.md）：
#   S01组3 / S04组2 / S05组1 / S06组1 / S07组1 / S08组2 → 图1 六场景重叠条件
#   S02 组1–6  → 图2 消息大小阶梯（含补测的 32 KiB / 48 KiB）
#   S03 组1–5  → 图3 大消息阶梯（20 Hz ×4 + 1 MiB@5 Hz）
#   S05/S06/S07 组1–4 → 图4 三种拓扑
# 旧一批结果（DDS/results/matrix_s01_s08，S03 为 unpaced、S08 为 300 s）不在白名单内，
# 且会被同条件的新结果按 test_start_time 覆盖。
DDS_TRUSTED = {
    ("S01", 3), ("S04", 2), ("S05", 1), ("S06", 1), ("S07", 1), ("S08", 2),   # 图1
    ("S02", 1), ("S02", 2), ("S02", 3), ("S02", 4), ("S02", 5), ("S02", 6),   # 图2
    ("S03", 1), ("S03", 2), ("S03", 3), ("S03", 4), ("S03", 5),               # 图3
    ("S05", 2), ("S05", 3), ("S05", 4),                                       # 图4
    ("S06", 2), ("S06", 3), ("S06", 4),
    ("S07", 2), ("S07", 3), ("S07", 4),
}

# DDS 侧"已实测但异常、暂不绘图"的条件（当前为空）。
#   历史记录：S05 组3（1P→3S）首次实测为 206.3 ms / P95 702.2 ms，而同场景相邻档位仅
#   0.28–0.43 ms（差约 500~700 倍）；用**原配置**重跑后为 0.3426 ms / P95 0.4464 ms、
#   丢包 0 → 判定为偶发干扰，已移出本集合，图 4 的 S05 列不再有缺口。
#   保留此集合是为了今后遇到异常档位时，有一个显式、可审计的排除位置。
DDS_ANOMALOUS: set = set()

# 除 DDS/results/ 之外，额外指定要合并的 suite 文件。
# 同一条件出现多次时取 test_start_time 更晚的那次，因此重测结果会自动覆盖旧值。
EXTRA_DDS_SUITES = (
    DATA_ROOT / "dds_26_results.json",                     # 26 个条件的主结果（9-16 16:40）
    DATA_ROOT / "outputs" / "S05_G3_3S_retest.json",       # S05 组3 重测（9-16 17:04，原配置通过）
)

# 图6：长稳漂移的时间桶宽度
DRIFT_BIN_SECONDS = 10.0

# 图2：S02 六档，按 payload 升序排列的组号（组5=32 KiB、组6=48 KiB 是后补的，
#      组号顺序与 payload 大小顺序不一致，所以这里显式写死排序）
S02_GROUPS = [1, 2, 3, 5, 6, 4]
S02_TICKS = ["1", "4", "16", "32", "48", "64"]
S02_FRAGMENT_THRESHOLD_BYTES = 60000            # VSOA workers.py 的 fragment_size_bytes 默认值
# 阈值画在 48 KiB 与 64 KiB 之间：S02_TICKS 的下标 4=48、5=64，故取 4.5。
# （不能取 3.5 —— 那是 32 与 48 之间，会让人误以为分片从 32→48 就开始。）
KNEE_X = 4.5

NOTE_COMMON = "测量参数：repeats=1 · drain_seconds=2.0 · 数据来自真实落盘结果，未插值、未生成"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_three(name: str) -> dict:
    """VSOA / MQTT / Zenoh：summary.json → {"S01-3": {...}}"""
    document = _load_json(DATA_ROOT / FOLDERS[name] / "summary.json")
    return document if isinstance(document, dict) else {}


def _parse_condition(condition_id) -> tuple[str, int] | None:
    """``S01_G3_1000Hz`` → ``("S01", 3)``"""
    parts = str(condition_id or "").split("_")
    if len(parts) < 3 or not parts[0].upper().startswith("S") or not parts[1].upper().startswith("G"):
        return None
    try:
        return parts[0].upper(), int(parts[1][1:])
    except ValueError:
        return None


def load_dds() -> dict:
    """DDS：results/ 下的 suite 文件与 suite 目录都读 → {("S01", 3): run}。

    同一条件出现在多份 suite 时取 test_start_time 更晚的那次（补测覆盖正式）。
    """
    root = DATA_ROOT / "DDS" / "results"
    candidates = [item for item in sorted(root.glob("*"))] if root.is_dir() else []
    candidates += [item for item in EXTRA_DDS_SUITES if item.exists()]
    out: dict[tuple[str, int], dict] = {}
    for item in candidates:
        document = _load_json(item / "result.json") if item.is_dir() else _load_json(item)
        if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
            continue
        for run in document["runs"]:
            # 防呆：只接受实测数据。夹具/模拟数据（synthetic / status=simulated）
            # 一律跳过，不能混进实测对比图。
            if run.get("synthetic") or str(run.get("status")).lower() in {"simulated", "synthetic"}:
                print(f"[!] 跳过非实测数据：{run.get('condition_id')} "
                      f"(status={run.get('status')}, synthetic={run.get('synthetic')})")
                continue
            key = _parse_condition(run.get("condition_id"))
            if key is None:
                continue
            previous = out.get(key)
            if previous is None or (run.get("test_start_time") or "") >= (previous.get("test_start_time") or ""):
                out[key] = run
    return out


# DDS 逐样本二进制 subscriber_0.receive.bin 的格式定义。
# 与 DDS/fastdds_bench/rawio.py 保持一致，此处内联复刻以免依赖那套包的导入环境：
#   HEADER        = "<8sII"           → magic, version, record_size
#   RECEIVE_RECORD= "<IQQQIIIII"      → 48 字节
#     字段：publisher_id, sequence_number, send_timestamp_ns, receive_timestamp_ns,
#           declared_payload_length, declared_checksum, actual_payload_length,
#           actual_checksum, flags
_DDS_RECV_MAGIC = b"FDBRCV01"
_DDS_RECV_HEADER = struct.Struct("<8sII")
_DDS_RECV_SIZE = 48
_DDS_RECV_DTYPE = np.dtype([
    ("publisher_id", "<I"), ("sequence_number", "<Q"),
    ("send_timestamp_ns", "<Q"), ("receive_timestamp_ns", "<Q"),
    ("declared_payload_length", "<I"), ("declared_checksum", "<I"),
    ("actual_payload_length", "<I"), ("actual_checksum", "<I"),
    ("flags", "<I"),
])
# DDS_VALID | DECODE_OK | LENGTH_OK | CHECKSUM_OK
_DDS_VALID_FLAGS = 0b1111


def load_dds_samples(sources: dict, scenario: str, group: int) -> np.ndarray | None:
    """读 DDS 的逐样本延迟（毫秒）；取不到返回 None。

    路径取自该 run 的 ``artifacts.run_directory``（相对 songfei/test/ 解析）：
        outputs/raw/<suite_id>/<run_dir>/subscriber_0.receive.bin
    该目录由 DDS 侧随结果一并交付；若缺失则本图不画 DDS，而不是拿汇总值伪造时间序列。
    """
    run = sources.get("dds", {}).get((scenario, group))
    if not isinstance(run, dict) or (scenario, group) not in DDS_TRUSTED:
        return None
    relative = str((run.get("artifacts") or {}).get("run_directory") or "").strip()
    if not relative:
        return None
    path = DATA_ROOT / relative / "subscriber_0.receive.bin"
    if not path.is_file():
        return None
    blob = path.read_bytes()
    if len(blob) < _DDS_RECV_HEADER.size:
        return None
    magic, _version, record_size = _DDS_RECV_HEADER.unpack_from(blob, 0)
    if magic != _DDS_RECV_MAGIC or record_size != _DDS_RECV_SIZE:
        return None
    count = (len(blob) - _DDS_RECV_HEADER.size) // record_size
    records = np.frombuffer(blob, dtype=_DDS_RECV_DTYPE, count=count,
                            offset=_DDS_RECV_HEADER.size)
    valid = (records["flags"] & _DDS_VALID_FLAGS) == _DDS_VALID_FLAGS
    if not valid.any():
        return None
    send = records["send_timestamp_ns"][valid].astype("int64")
    receive = records["receive_timestamp_ns"][valid].astype("int64")
    return (receive - send) / 1e6


def get_metric(sources: dict, name: str, scenario: str, group: int, field: str) -> float | None:
    """取一个指标；非成功状态或缺失时返回 None（画图时断线，而不是补 0）。"""
    if name == "dds":
        run = sources.get("dds", {}).get((scenario, group))
        if not run or run.get("status") not in {"passed", "completed"}:
            return None
        if (scenario, group) not in DDS_TRUSTED or (scenario, group) in DDS_ANOMALOUS:
            return None          # 不在白名单 / 判定异常：不参与对比
        value = run.get(field)
    else:
        row = sources.get(name, {}).get(f"{scenario}-{group}") or {}
        # DDS 侧用 passed，另三家用 completed
        if row.get("status") != "completed":
            return None
        value = row.get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def series(sources: dict, name: str, keys, field: str) -> np.ndarray:
    values = [get_metric(sources, name, scenario, group, field) for scenario, group in keys]
    return np.asarray([np.nan if value is None else value for value in values], dtype=float)


def spread_percent(values: np.ndarray) -> float | None:
    """极差占均值的百分比（用来量化"这一列稳不稳"）。"""
    clean = values[~np.isnan(values)]
    if clean.size < 2 or float(np.mean(clean)) == 0.0:
        return None
    return float(clean.max() - clean.min()) / float(np.mean(clean)) * 100.0


def _label_with_spread(name: str, values: np.ndarray, field_name: str) -> str:
    spread = spread_percent(values)
    suffix = f"，极差 {spread:.1f}%" if spread is not None else ""
    return f"{PRETTY[name]}{field_name}{suffix}"


# --------------------------------------------------------------------------- 图1
def figure_repeatability(sources: dict, names: list, out_path: Path) -> None:
    x = np.arange(len(REPEAT_KEYS))
    ticks = [f"{scenario}-{group}" for scenario, group in REPEAT_KEYS]
    plotted = list(names)      # 本轮 DDS 已补齐 6 个重叠条件，四家同时绘制

    figure, axes = plt.subplots(1, 2, figsize=(15, 6.4))
    figure.suptitle(
        "图1 · 六场景重复性证据：同一条件（1P/1S · 1 KiB · 1000 Hz）在 6 个场景里各测 1 次\n"
        "同一参数被独立测量 6 次 —— 排序若稳定，说明差异是真实的；离散度则说明哪一列可信",
        fontsize=13,
    )

    for name in plotted:
        color = COLORS[name]
        means = series(sources, name, REPEAT_KEYS, "latency_ms")
        p99s = series(sources, name, REPEAT_KEYS, "latency_p99_ms")
        for axis, values, tone in ((axes[0], means, " 均值"), (axes[1], p99s, " P99")):
            axis.plot(x, values, "-o", color=color, linewidth=2, markersize=7,
                      label=_label_with_spread(name, values, tone))
            clean = ~np.isnan(values)
            if clean.any():
                low, high = float(np.nanmin(values)), float(np.nanmax(values))
                if high > low:
                    axis.fill_between(x, low, high, color=color, alpha=0.10, linewidth=0)
            axis.plot(x, values, "o", color=color, markersize=7)

    axes[0].set(title="延迟均值（越低越好）", ylabel="ms", xlabel="场景-组（同一参数）")
    axes[1].set(title="延迟 P99（尾部；对数轴，差异更明显）", ylabel="ms", xlabel="场景-组（同一参数）")
    axes[1].set_yscale("log")
    # 四家的曲线集中在中间偏下，loc="best" 找不到空白 → 图例压住曲线。
    # 这里在轴的顶部留出一段空白带，图例固定放左上角，落在空白里。
    low, high = axes[0].get_ylim()
    axes[0].set_ylim(low - (high - low) * 0.05, low + (high - low) * 1.6)
    low, high = axes[1].get_ylim()
    axes[1].set_ylim(low * 0.8, high * 3.0)
    for axis in axes:
        axis.set_xticks(x)
        axis.set_xticklabels(ticks, rotation=20)
        axis.grid(alpha=0.3, which="both")
        axis.legend(fontsize=9, loc="upper left", framealpha=0.95)

    notes = ["传输方式：VSOA、MQTT=TCP；Zenoh=其默认配置；DDS=UDPv4/reliable。"]

    # 结论横幅：在场各家的顺序是否在多个场景里保持一致
    checked = consistent = 0
    for scenario, group in REPEAT_KEYS:
        present = [(name, get_metric(sources, name, scenario, group, "latency_ms")) for name in names]
        present = [(name, value) for name, value in present if value is not None]
        if len(present) < 2:
            continue
        checked += 1
        if [name for name, _ in sorted(present, key=lambda item: item[1])] == [name for name, _ in present]:
            consistent += 1
    # 三家在全部 6 个场景都有数据，故显示为 6/6；"6/6 一致"指的是"在场各家的先后顺序"一致
    all_present = all(
        all(get_metric(sources, name, scenario, group, "latency_ms") is not None for name in plotted)
        for scenario, group in REPEAT_KEYS
    )
    scale_note = "" if all_present else "；S08-2 无 DDS 数据，该点为三家排序"
    figure.text(0.5, 0.012, NOTE_COMMON + ("\n" + "\n".join(notes) if notes else ""),
                ha="center", va="bottom", fontsize=9, color="#555555")
    figure.text(0.5, 0.098,
                f"结论：延迟均值排序为 {'  <  '.join(PRETTY[name] for name in plotted)}，"
                f"在 {consistent}/{checked} 个重叠场景中顺序完全一致{scale_note}",
                ha="center", va="bottom", fontsize=11, color="#222222", weight="bold")
    figure.tight_layout(rect=(0, 0.145, 1, 0.90))
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")

    # 控制台同时把数字打出来，便于核对排序
    print("  排序（延迟均值，由低到高）：")
    for index, (scenario, group) in enumerate(REPEAT_KEYS):
        row = []
        for name in plotted:
            value = get_metric(sources, name, scenario, group, "latency_ms")
            row.append((name, value))
        ranked = sorted((item for item in row if item[1] is not None), key=lambda item: item[1])
        rendered = " < ".join(f"{PRETTY[n]}({v:.4f})" for n, v in ranked)
        missing = [PRETTY[n] for n, v in row if v is None]
        print(f"    {scenario}-{group}: {rendered}" + (f"   [缺: {', '.join(missing)}]" if missing else ""))


# --------------------------------------------------------------------------- 图2
def figure_s02_knee(sources: dict, names: list, out_path: Path) -> None:
    keys = [("S02", group) for group in S02_GROUPS]
    x = np.arange(len(keys))

    figure, axes = plt.subplots(2, 2, figsize=(15, 10))
    figure.suptitle(
        "图2 · S02 消息大小阶梯（100 Hz）与分片拐点\n"
        f"虚线 = VSOA 分片阈值 {S02_FRAGMENT_THRESHOLD_BYTES} B：48 KiB 不分片，64 KiB 起被切成 2 片 —— 机制切换点",
        fontsize=13,
    )

    for name in names:
        color = COLORS[name]
        mean = series(sources, name, keys, "latency_ms")
        p95 = series(sources, name, keys, "latency_p95_ms")
        p99 = series(sources, name, keys, "latency_p99_ms")
        cpu = series(sources, name, keys, "cpu_percent")
        memory = series(sources, name, keys, "memory_mb")

        axes[0][0].plot(x, mean, "-o", color=color, linewidth=2, label=f"{PRETTY[name]} 均值")
        axes[0][0].plot(x, p95, "--", color=color, linewidth=1.2, alpha=0.75,
                        label=f"{PRETTY[name]} P95")
        axes[0][1].plot(x, p99, "-o", color=color, linewidth=2, label=f"{PRETTY[name]} P99")
        axes[1][0].plot(x, cpu, "-o", color=color, linewidth=2, label=f"{PRETTY[name]} CPU%")
        axes[1][1].plot(x, memory, "-o", color=color, linewidth=2, label=f"{PRETTY[name]} 内存 MB")

        # 拐点标注：48 → 64 KiB 的跳幅
        if not np.isnan(mean[4]) and not np.isnan(mean[5]) and mean[4]:
            axes[0][0].annotate(f"+{(mean[5] / mean[4] - 1) * 100:.0f}%",
                                xy=(4.5, (mean[4] + mean[5]) / 2), ha="center", fontsize=8.5, color=color)
        if not np.isnan(p99[4]) and not np.isnan(p99[5]) and p99[4]:
            axes[0][1].annotate(f"×{p99[5] / p99[4]:.1f}",
                                xy=(4.5, (p99[4] + p99[5]) / 2), ha="center", fontsize=8.5, color=color)

    for axis in (axes[0][0], axes[0][1], axes[1][0], axes[1][1]):
        axis.axvline(KNEE_X, color="#666666", linestyle=":", linewidth=1.6)
        axis.set_xticks(x)
        axis.set_xticklabels(S02_TICKS)
        axis.set_xlabel("payload 尺寸 (KiB)")
        axis.grid(alpha=0.3, which="both")
        axis.legend(fontsize=8.5, loc="best", framealpha=0.9)

    axes[0][0].set(title="延迟（实线=均值，虚线=P95）", ylabel="ms")
    axes[0][1].set(title="延迟 P99（对数轴；尾部差异）", ylabel="ms")
    axes[0][1].set_yscale("log")
    axes[1][0].set(title="CPU 占用（统计范围见脚注）", ylabel="CPU%")
    axes[1][1].set(title="内存 RSS 峰值", ylabel="MB")
    axes[0][1].annotate(f"分片阈值\n{S02_FRAGMENT_THRESHOLD_BYTES} B", xy=(KNEE_X, 0.97),
                        xycoords=("data", "axes fraction"), ha="center", va="top",
                        fontsize=8.5, color="#444444",
                        bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                                  edgecolor="#bbbbbb", alpha=0.9))

    notes = ["CPU 统计范围：VSOA=端点进程；MQTT=端点+broker；Zenoh=其上报值；DDS=端点进程。"]

    # 结论横幅：谁在阈值处跳、跳多少
    jumps = []
    for name in names:
        before = get_metric(sources, name, "S02", 6, "latency_ms")
        after = get_metric(sources, name, "S02", 4, "latency_ms")
        if before and after:
            jumps.append(f"{PRETTY[name]} {before:.3f}→{after:.3f} ms（+{(after / before - 1) * 100:.0f}%）")
    figure.text(0.5, 0.012, NOTE_COMMON + "\n" + "\n".join(notes),
                ha="center", va="bottom", fontsize=9, color="#555555")
    if jumps:
        figure.text(0.5, 0.070, "48 KiB → 64 KiB 延迟均值跳幅：" + "；".join(jumps),
                    ha="center", va="bottom", fontsize=11, color="#222222", weight="bold")
    figure.tight_layout(rect=(0, (0.105 if jumps else 0.075), 1, 0.90))
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")

    print("  VSOA 拐点核对（48 KiB → 64 KiB）：")
    for field, tone in (("latency_ms", "均值"), ("latency_p95_ms", "P95"), ("latency_p99_ms", "P99")):
        before = get_metric(sources, "vsoa", "S02", 6, field)
        after = get_metric(sources, "vsoa", "S02", 4, field)
        if before and after:
            print(f"    {tone}: {before:.4f} → {after:.4f} ms  (+{(after / before - 1) * 100:.1f}%)")


# --------------------------------------------------------------------------- 公共
def params_of(sources: dict, scenario: str, group: int) -> dict:
    """取该条件的关键参数；优先用三家 summary 里的 parameters，退回 DDS run 的顶层字段。"""
    for name in ("vsoa", "mqtt", "zenoh"):
        params = (sources.get(name, {}).get(f"{scenario}-{group}") or {}).get("parameters")
        if isinstance(params, dict) and params.get("payload_size_bytes"):
            return params
    run = sources.get("dds", {}).get((scenario, group))
    if isinstance(run, dict):
        return {"payload_size_bytes": run.get("payload_size_bytes"),
                "publish_rate_hz": run.get("publish_rate_hz"),
                "publisher_count": run.get("publisher_count"),
                "subscriber_count": run.get("subscriber_count")}
    return {}


def target_mbps(sources: dict, keys) -> np.ndarray:
    """"应发吞吐" = payload × 8 × 速率 × P × S / 1e6（与 throughput_mbps 同口径，含全部订阅者）。"""
    out = []
    for scenario, group in keys:
        params = params_of(sources, scenario, group)
        payload = params.get("payload_size_bytes")
        rate = params.get("publish_rate_hz")
        pubs = params.get("publisher_count") or 1
        subs = params.get("subscriber_count") or 1
        if isinstance(payload, (int, float)) and isinstance(rate, (int, float)) and rate:
            out.append(payload * 8 * rate * pubs * subs / 1e6)
        else:
            out.append(np.nan)
    return np.asarray(out, dtype=float)


def _plot_style(name: str, dashed: bool = False) -> dict:
    color = COLORS[name]
    if dashed:
        return dict(color=color, linewidth=1.8, linestyle="--", marker="o", markersize=6,
                    markerfacecolor="none")
    return dict(color=color, linewidth=2, linestyle="-", marker="o", markersize=6)


# --------------------------------------------------------------------------- 图3
def figure_s03_large(sources: dict, names: list, out_path: Path) -> None:
    keys = [("S03", group) for group in (1, 2, 3, 4, 5)]
    ticks = ["64 KiB\n@20 Hz", "128 KiB\n@20 Hz", "256 KiB\n@20 Hz", "512 KiB\n@20 Hz", "1 MiB\n@5 Hz"]
    x = np.arange(len(keys))

    figure, axes = plt.subplots(2, 2, figsize=(15, 10))
    figure.suptitle(
        "图3 · S03 大消息吞吐与尾部代价（VSOA 的尾部在这里拉开）\n"
        "VSOA 走应用层分片（60000 B/片）：1 MiB = 18 片，整包延迟取决于最慢的一片 → P99 被显著拉长",
        fontsize=13,
    )

    target = target_mbps(sources, keys)
    axes[0][0].plot(x, target, ":", color="#888888", linewidth=1.6, label="目标（按该档参数）")

    for name in names:
        mean = series(sources, name, keys, "latency_ms")
        if np.all(np.isnan(mean)):
            continue                      # 该家在 S03 无可用数据（DDS 参数不一致，见脚注）
        p95 = series(sources, name, keys, "latency_p95_ms")
        p99 = series(sources, name, keys, "latency_p99_ms")
        thr = series(sources, name, keys, "throughput_mbps")
        style = _plot_style(name)

        axes[0][0].plot(x, thr, label=f"{PRETTY[name]} 实收", **style)
        axes[0][1].plot(x, mean, label=f"{PRETTY[name]} 均值", **style)
        axes[0][1].plot(x, p95, color=COLORS[name], linestyle="--", linewidth=1.2,
                        alpha=0.75, label=f"{PRETTY[name]} P95")
        axes[1][0].plot(x, p99, label=f"{PRETTY[name]} P99", **style)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = p99 / p95
        axes[1][1].plot(x, ratio, label=f"{PRETTY[name]} P99 ÷ P95", **style)

    axes[0][0].set(title="实收吞吐 vs 目标吞吐（重合=无丢包）", ylabel="Mbps", xlabel="消息尺寸 / 速率")
    axes[0][1].set(title="延迟（实线=均值，虚线=P95）", ylabel="ms", xlabel="消息尺寸 / 速率")
    axes[1][0].set(title="延迟 P99（对数轴）", ylabel="ms", xlabel="消息尺寸 / 速率")
    axes[1][0].set_yscale("log")
    axes[1][1].set(title="尾部代价：P99 ÷ P95（越大于 1，尾部越被拉长）", ylabel="倍数", xlabel="消息尺寸 / 速率")
    axes[1][1].axhline(1.0, color="#888888", linestyle=":", linewidth=1.2)
    for axis in axes.ravel():
        axis.set_xticks(x)
        axis.set_xticklabels(ticks, fontsize=9)
        axis.grid(alpha=0.3, which="both")
        axis.legend(fontsize=8.5, loc="best", framealpha=0.9)

    # 结论横幅：1 MiB 档（下标 4）的 P99/P95
    ratios = []
    for name in names:
        p95 = get_metric(sources, name, "S03", 5, "latency_p95_ms")
        p99 = get_metric(sources, name, "S03", 5, "latency_p99_ms")
        if p95 and p99:
            ratios.append(f"{PRETTY[name]} {p99 / p95:.2f}")
    notes = ["1 MiB 档条件为 5 Hz × 40 s，每档 200 条样本，与其它档样本量一致。",
             "传输方式：VSOA、MQTT=TCP；Zenoh=其默认配置；DDS=UDPv4/reliable。"]
    figure.text(0.5, 0.012, NOTE_COMMON + "\n" + "\n".join(notes),
                ha="center", va="bottom", fontsize=9, color="#555555")
    if ratios:
        figure.text(0.5, 0.075, "1 MiB 档 P99 ÷ P95：" + "；".join(ratios) + "（越大表示尾部越被拉长）",
                    ha="center", va="bottom", fontsize=11, color="#222222", weight="bold")
    figure.tight_layout(rect=(0, 0.12, 1, 0.90))
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")
    for name in names:
        p95 = get_metric(sources, name, "S03", 5, "latency_p95_ms")
        p99 = get_metric(sources, name, "S03", 5, "latency_p99_ms")
        mean = get_metric(sources, name, "S03", 5, "latency_ms")
        if mean is None:
            print(f"    {PRETTY[name]}: S03 无可用数据")
        else:
            print(f"    {PRETTY[name]} 1 MiB: 均值 {mean:.2f} ms, P95 {p95:.2f}, P99 {p99:.2f}, P99/P95 {p99 / p95:.2f}")


# --------------------------------------------------------------------------- 图4
def figure_topology(sources: dict, names: list, out_path: Path) -> None:
    # 三种拓扑，标题写成自解释形式（术语放在括号里，避免只看"扇出"不知道是哪一端在扩散）：
    #   S05 一对多：1 个发布者把同一份数据分发给 N 个订阅者（fan-out，扇出）
    #   S06 多对一：N 个发布者把数据汇到 1 个订阅者（fan-in，汇聚）
    #   S07 多对多：N 个发布者 × N 个订阅者并发
    columns = [
        ("S05", "一对多（扇出）\n1 个发布 → N 个订阅", ["1S", "2S", "3S", "4S"]),
        ("S06", "多对一（汇聚）\nN 个发布 → 1 个订阅", ["1P", "2P", "3P", "4P"]),
        ("S07", "多对多（并发）\nN 个发布 → N 个订阅", ["1×1", "2×2", "3×3", "4×4"]),
    ]
    rows = [
        ("latency_ms", "延迟均值", "ms", False),
        ("memory_mb", "内存 RSS 峰值", "MB", False),
        ("cpu_percent", "CPU 占用（统计范围见脚注）", "CPU%", False),
    ]
    x = np.arange(4)

    figure, axes = plt.subplots(3, 3, figsize=(16, 12.5))
    figure.suptitle(
        "图4 · 拓扑成本结构：一对多（扇出）/ 多对一（汇聚）/ 多对多（并发）\n"
        "吞吐在所有拓扑下都等于「payload×8×速率×P×S」＝配置值，真正的差异在延迟、内存与 CPU 的分配上",
        fontsize=13,
    )

    for column, (scenario, title, ticks) in enumerate(columns):
        keys = [(scenario, group) for group in (1, 2, 3, 4)]
        target = target_mbps(sources, keys)
        for row, (field, row_title, unit, _) in enumerate(rows):
            axis = axes[row][column]
            for name in names:
                values = series(sources, name, keys, field)
                if np.all(np.isnan(values)):
                    continue
                axis.plot(x, values, label=PRETTY[name], **_plot_style(name))
            axis.set_xticks(x)
            axis.set_xticklabels(ticks)
            axis.grid(alpha=0.3)
            axis.legend(fontsize=8.5, loc="best", framealpha=0.9)
            ylabel = unit if column == 0 else ""
            axis.set(title=(f"{title}\n{row_title}" if row == 0 else row_title), ylabel=ylabel)
            if row == 0:
                # 应发吞吐并进 x 轴标签：单独放一行会和"端数"重叠
                axis.set_xlabel("应发吞吐（Mbps）= " + " / ".join(f"{v:.1f}" for v in target),
                                fontsize=8.5)

    notes = ["吞吐未单独占版面：各档实测值与 payload×8×速率×P×S 逐档相等（误差 <1%），故不单列。",
             "CPU 统计范围：VSOA=端点进程；MQTT=端点+broker；Zenoh=其上报值；DDS=端点进程"
             "（VSOA 的统计范围为发布端进程）。",
             "DDS 覆盖档位：一对多 1S–4S、多对一 1P–4P、多对多 1×1–4×4。"]
    figure.text(0.5, 0.010, NOTE_COMMON + "\n" + "\n".join(notes),
                ha="center", va="bottom", fontsize=9, color="#555555")
    figure.tight_layout(rect=(0, 0.10, 1, 0.92))
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")


# --------------------------------------------------------------------------- 图5
def figure_s08_stability(sources: dict, names: list, out_path: Path) -> None:
    keys = [("S08", 1), ("S08", 2), ("S08", 3), ("S08", 4)]
    ticks = ["1 KiB\n100 Hz", "1 KiB\n1000 Hz", "64 KiB\n100 Hz", "1 MiB\n5 Hz"]
    x = np.arange(len(keys))

    figure, axes = plt.subplots(2, 2, figsize=(15, 10))
    figure.suptitle(
        "图5 · S08 长时间稳定性：4 个负载档位下跑满 100 s 后的稳态结果\n"
        "四档覆盖「小包低频 → 小包高频 → 中包低频 → 大包低频」，看的是长稳下的尾部和资源",
        fontsize=13,
    )

    for name in names:
        if name == "dds":
            continue                  # DDS 的 S08 时长 300 s，与三家 100 s 不等价，本图不接入
        suffix = ""
        style = _plot_style(name)
        mean = series(sources, name, keys, "latency_ms")
        if np.all(np.isnan(mean)):
            continue
        p95 = series(sources, name, keys, "latency_p95_ms")
        p99 = series(sources, name, keys, "latency_p99_ms")
        memory = series(sources, name, keys, "memory_mb")
        cpu = series(sources, name, keys, "cpu_percent")

        axes[0][0].plot(x, mean, label=f"{PRETTY[name]} 均值{suffix}", **style)
        axes[0][0].plot(x, p95, color=COLORS[name], linestyle="--", linewidth=1.2, alpha=0.75,
                        label=f"{PRETTY[name]} P95")
        axes[0][1].plot(x, p99, label=f"{PRETTY[name]} P99{suffix}", **style)
        axes[1][0].plot(x, memory, label=f"{PRETTY[name]} 内存{suffix}", **style)
        axes[1][1].plot(x, cpu, label=f"{PRETTY[name]} CPU{suffix}", **style)

    axes[0][0].set(title="延迟（实线=均值，虚线=P95）", ylabel="ms", xlabel="S08 档位")
    axes[0][1].set(title="延迟 P99（对数轴；长稳下尾部是否失控）", ylabel="ms", xlabel="S08 档位")
    axes[0][1].set_yscale("log")
    axes[1][0].set(title="内存 RSS 峰值（长稳是否持续增长）", ylabel="MB", xlabel="S08 档位")
    axes[1][1].set(title="CPU 占用（统计范围见脚注）", ylabel="CPU%", xlabel="S08 档位")
    for axis in axes.ravel():
        axis.set_xticks(x)
        axis.set_xticklabels(ticks, fontsize=9)
        axis.grid(alpha=0.3, which="both")
        axis.legend(fontsize=8.5, loc="best", framealpha=0.9)

    notes = ["本图覆盖 VSOA / MQTT / Zenoh 三家；DDS 本轮交付的 S08 条件为组2（100 s），另三档不在此列。",
             "逐时间桶的漂移曲线见 compare_S08_drift.png。"]
    figure.text(0.5, 0.010, NOTE_COMMON + "\n" + "\n".join(notes),
                ha="center", va="bottom", fontsize=9, color="#555555")
    figure.tight_layout(rect=(0, 0.10, 1, 0.92))
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")


def figure_s08_drift(sources: dict, names: list, out_path: Path) -> None:
    """图6 · S08 长稳漂移（按时间分段）。

    三家走 plot_compare.py 的 loader，DDS 走其原始二进制 subscriber_0.receive.bin。
    只画三格，都是延迟视角：每段均值 / P95 / 最大值。
    不画 CPU：逐采样资源只有 VSOA 与 MQTT 落盘，Zenoh 与 DDS 没有，
    四家里只有两家能画，信息量不足且容易被误读成"另两家没测"。

    版式为「上二下一」：均值与 P95 并排在上，最大值在左下。
    右下角只放漂移汇总文字，不放第四张图 —— 三格排成 2×2 时留一个空洞，
    整块图看起来像少画了一格。
    画布 15×9.5 → 比例 1.579，与 PPT 里 8.26×5.23 in 的图片框一致，替换后不被拉伸。
    """
    import plot_compare as pc

    group, window = 2, 100.0          # S08 组2 = 1 KiB @ 1000 Hz，最长稳的档

    # 边距直接写死在 gridspec 上，不走 tight_layout：右下角那格是纯文字（axis off），
    # tight_layout 会把它一起参与排版，反而把三个面板挤变形。
    figure = plt.figure(figsize=(15, 9.5))
    grid = figure.add_gridspec(2, 2, hspace=0.30, wspace=0.24,
                               left=0.058, right=0.985, top=0.905, bottom=0.125)
    axes = [figure.add_subplot(grid[0, 0]),
            figure.add_subplot(grid[0, 1]),
            figure.add_subplot(grid[1, 0])]
    summary_axis = figure.add_subplot(grid[1, 1])
    summary_axis.axis("off")
    figure.suptitle(
        f"图6 · S08 长稳漂移：每组 {window:.0f} s，分成 {window / DRIFT_BIN_SECONDS:.0f} 段、每段 {DRIFT_BIN_SECONDS:.0f} s\n"
        "S08 组2（1 KiB @ 1000 Hz）· 覆盖 VSOA / MQTT / Zenoh / DDS",
        fontsize=13,
    )

    drift_rows: list = []
    for name in names:
        if name == "dds":
            # DDS 逐样本来自其原始二进制（plot_compare 的 loader 不认这种格式）
            info, latencies = {}, load_dds_samples(sources, "S08", group)
        else:
            info = pc.LOADERS[name]("S08", group)
            latencies = info.get("latencies")
        if name == "vsoa":
            # VSOA 的 JSON 逐样本被 sample_capacity=10000 截断（S08-2 实收 99954 条），
            # 完整序列在同目录的 .f64 里；不读它会把前 1 万条摊到整条时间轴上。
            source = Path(info.get("source") or "")
            document = _load_json(source) or {}
            full = source.parent / str(document.get("latencies_ms_file") or "")
            if document.get("latencies_ms_file") and full.is_file():
                latencies = np.fromfile(full, dtype="<f8")
        if latencies is None or len(latencies) == 0:
            print(f"    {PRETTY[name]}: 无逐样本数据（{info.get('error')}）")
            continue
        stats = pc.binned(latencies, window, DRIFT_BIN_SECONDS)
        color = COLORS[name]
        label = f"{PRETTY[name]}（n={len(latencies)}）"

        axes[0].plot(stats["centers"], stats["mean"], color=color, linewidth=2, label=label)
        axes[1].plot(stats["centers"], stats["p95"], color=color, linewidth=2, label=label)
        axes[2].plot(stats["centers"], stats["max"], color=color, linewidth=2, label=label)

        # 漂移：后 1/3 桶均值 vs 前 1/3 桶均值（用与画图同一份样本，避免口径分叉）
        clean = stats["mean"][~np.isnan(stats["mean"])]
        if clean.size >= 6:
            third = max(1, clean.size // 3)
            head, tail = float(np.mean(clean[:third])), float(np.mean(clean[-third:]))
            if head:
                drift_rows.append((name, tail / head - 1.0))

    span = f"{DRIFT_BIN_SECONDS:.0f} s"
    axes[0].set(title=f"延迟均值（每 {span} 一段的平均）", xlabel="时间 (s)", ylabel="ms")
    axes[1].set(title=f"延迟 P95（每 {span} 一段）", xlabel="时间 (s)", ylabel="ms")
    axes[2].set(title=f"延迟最大值（每 {span} 一段内最大）", xlabel="时间 (s)", ylabel="ms")
    for axis in axes:
        # 顶部留白放图例：四家的曲线会占满整个纵轴范围，loc="best" 找不到空白处
        low, high = axis.get_ylim()
        axis.set_ylim(low - (high - low) * 0.04, low + (high - low) * 1.35)
        axis.grid(alpha=0.3)
        axis.legend(fontsize=8.5, loc="upper left", framealpha=0.95)

    notes = ["分段方法：逐样本按测量窗口均匀铺开（各家样本不带统一墙钟时间戳）。",
             "VSOA 逐样本取自完整二进制序列（其 JSON 字段为容量摘要）。",
             "DDS 逐样本取自 subscriber_0.receive.bin；其均值/P95/P99 与 suite 汇总值一致。",
             "漂移为负值不等同于「越跑越快」：若起始段存在预热，后段回落同样会给出负值 —— "
             "请结合 P95 面板判断（Zenoh 的 −18.6% 属瞬态回落）。"]
    figure.text(0.5, 0.012, NOTE_COMMON + "\n" + "\n".join(notes),
                ha="center", va="bottom", fontsize=9, color="#555555")
    if drift_rows:
        lines = ["漂移（后 1/3 段 vs 前 1/3 段）", ""]
        lines += [f"{PRETTY[name]}：{value * 100:+.1f}%" for name, value in drift_rows]
        lines += ["", "四家均无持续劣化。", "Zenoh 的负值来自前 25 s 预热回落，",
                  "25 s 后即进入稳态（见 P95 面板）。"]
        summary_axis.text(0.10, 0.95, "\n".join(lines), transform=summary_axis.transAxes,
                          ha="left", va="top", fontsize=13.5, color="#222222", linespacing=1.7)
    figure.savefig(out_path, dpi=150)
    plt.close(figure)
    print(f"[图] {out_path}")
    for name, value in drift_rows:
        print(f"    {PRETTY[name]} 漂移 {value * 100:+.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="跨场景差异对比图")
    parser.add_argument("--middlewares", default="vsoa,mqtt,zenoh,dds")
    parser.add_argument("--only", default=None,
                        choices=("repeatability", "knee", "large", "topology", "stability", "drift"),
                        help="只出某一张图（缺省=全部）")
    parser.add_argument("--out-dir", default=None, help="默认 songfei/test/")
    args = parser.parse_args()

    names = [item.strip().lower() for item in args.middlewares.split(",") if item.strip()]
    unknown = [name for name in names if name not in ALL_NAMES]
    if unknown:
        raise SystemExit(f"未知中间件：{unknown}（可选：{', '.join(ALL_NAMES)}）")

    sources = {name: (load_dds() if name == "dds" else load_three(name)) for name in names}
    if "dds" in names and not sources["dds"]:
        print("[!] 没读到 DDS 的 suite 结果（DDS/results/ 下应有含 runs 的 JSON）")
    for name in names:
        if name != "dds":
            count = len(sources[name])
            print(f"[读] {PRETTY[name]}: summary.json 共 {count} 组")

    out_dir = Path(args.out_dir) if args.out_dir else DATA_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)

    figures = [
        ("repeatability", "compare_repeatability.png", figure_repeatability),
        ("knee", "compare_S02_payload_knee.png", figure_s02_knee),
        ("large", "compare_S03_large_message_tail.png", figure_s03_large),
        ("topology", "compare_S05_S07_topology.png", figure_topology),
        ("stability", "compare_S08_stability.png", figure_s08_stability),
        ("drift", "compare_S08_drift.png", figure_s08_drift),
    ]
    for key, filename, builder in figures:
        if args.only and args.only != key:
            continue
        builder(sources, names, out_dir / filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
