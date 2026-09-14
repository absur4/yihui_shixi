"""DDS 统一适配器（契约层）。

本文件是「即插即用」的硬门槛：**顶层不得出现 `import fastdds`**，也不得导入任何
会触发原生依赖的模块，因此主解释器（可能没有 Fast DDS 绑定、没有 PyYAML）也能
成功 `exec_module` 并调用：

* :func:`metadata` —— 中间件元数据与**真实**可用性；
* :func:`catalog` —— S01–S12 全部标准条件（与是否编译无关）；
* :func:`build_cases` —— 把表单参数或完整矩阵展开成引擎可执行条件；
* :func:`base_config` —— 引擎全局配置（写入 spec.json 的 config）；
* :func:`runner_command` —— 启动子进程执行器的命令行。

兼容根 README §4 的适配器形式：:class:`Adapter` / :func:`create_adapter`。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

FOLDER = Path(__file__).resolve().parent
REPO_ROOT = FOLDER.parent
CONFIG_PATH = FOLDER / "config.yaml"

if str(FOLDER) not in sys.path:
    sys.path.insert(0, str(FOLDER))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yamlmini  # noqa: E402  (零依赖的 YAML 子集读取器)

try:
    from interfaces import MiddlewareAdapter  # type: ignore
except Exception:  # pragma: no cover - standalone adapter import
    class MiddlewareAdapter:  # type: ignore[no-redef]
        pass

MIDDLEWARE_ID = "dds"
MIDDLEWARE_LABEL = "DDS"
VENDOR = "eProsima Fast DDS"
TRANSPORT_OPTIONS = ["udp", "shm"]
QOS_OPTIONS = [
    {"value": "reliable", "label": "RELIABLE 可靠"},
    {"value": "best_effort", "label": "BEST_EFFORT 尽力而为"},
]

_MATRIX_CACHE: tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None = None
_ROWS_CACHE: list[dict[str, Any]] | None = None


# --------------------------------------------------------------------------- 环境探测


def _venv_python() -> Path | None:
    for candidate in (
        FOLDER / ".venv" / "Scripts" / "python.exe",
        FOLDER / ".venv" / "bin" / "python",
        FOLDER / ".venv" / "bin" / "python3",
    ):
        if candidate.exists():
            return candidate
    return None


def _find_fastdds_binding() -> str | None:
    """在私有 runtime 或当前解释器里寻找 fastdds 绑定（只做查找，不导入）。"""
    try:
        if importlib.util.find_spec("fastdds") is not None:
            return "current-interpreter"
    except (ImportError, ValueError):
        pass
    runtime = FOLDER / "runtime"
    if runtime.is_dir():
        for candidate in sorted(runtime.glob("**/site-packages/fastdds/__init__.py")):
            return str(candidate)
        for candidate in sorted(runtime.glob("**/fastdds/__init__.py")):
            return str(candidate)
    return None


def _find_type_support() -> str | None:
    runtime = FOLDER / "runtime"
    if runtime.is_dir():
        for pattern in ("**/BenchmarkMessage.py", "**/BenchmarkMessage/__init__.py"):
            for candidate in sorted(runtime.glob(pattern)):
                return str(candidate)
    return None


def _tool_advisories() -> list[str]:
    """只在重新编译绑定时才需要的工具，缺失不影响已有 runtime 的运行。"""
    advisories: list[str] = []
    missing = [
        name
        for name in ("cmake", "swig", "java")
        if shutil.which(name) is None
    ]
    if missing:
        advisories.append(
            "缺少重新编译绑定所需的工具："
            + ", ".join(missing)
            + "（仅在需要重建 runtime 时才有影响）"
        )
    if not os.environ.get("FASTDDSHOME"):
        advisories.append("未设置 FASTDDSHOME（运行期加载 Fast DDS DLL 需要）")
    return advisories


def probe_environment() -> tuple[bool, str]:
    """返回 ``(ok, detail)``；任何异常都不会抛出。"""
    try:
        reasons: list[str] = []
        runtime = FOLDER / "runtime"
        binding = _find_fastdds_binding()
        if binding is None:
            reasons.append(
                "未找到 fastdds Python 绑定（需先执行 DDS/setup_windows.bat 构建 runtime）"
            )
        if not runtime.is_dir():
            reasons.append("缺少编译后的 runtime 目录")
        elif _find_type_support() is None:
            reasons.append("缺少 IDL 生成的类型支持模块 BenchmarkMessage")
        if _venv_python() is None:
            reasons.append("缺少私有虚拟环境 DDS/.venv（Python 3.11）")
        if not os.environ.get("FASTDDSHOME"):
            reasons.append("未设置 FASTDDSHOME")
        if shutil.which("cmake") is None and binding is None:
            reasons.append("缺少 CMake")
        return (not reasons), "；".join(reasons)
    except Exception as exc:  # 探测本身失败也必须降级为 available=False
        return False, f"环境探测异常：{type(exc).__name__}: {exc}"


def _runtime_info() -> dict[str, Any]:
    path = FOLDER / "runtime" / "build_info.json"
    if not path.is_file():
        return {}
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _detect_version(suite: dict[str, Any]) -> tuple[str | None, str]:
    """探测 Fast DDS 版本；探测不到就回落到配置里声明的目标版本并注明来源。"""
    info = _runtime_info()
    for key in ("fastdds_version", "version"):
        value = info.get(key)
        if value:
            return str(value), f"runtime/build_info.json:{key}"
    declared = suite.get("module_version")
    if declared:
        return str(declared), "config.yaml suite.module_version（runtime 未记录独立版本字段）"
    return None, "未探测到"


# --------------------------------------------------------------------------- 条件矩阵


def _load_matrix():
    global _MATRIX_CACHE
    if _MATRIX_CACHE is None:
        document = yamlmini.load(CONFIG_PATH)
        if not isinstance(document, dict):
            raise RuntimeError("DDS/config.yaml 的根必须是映射")
        suite = document.get("suite") or {}
        profiles = document.get("network_profiles") or {}
        conditions = list(document.get("conditions") or [])
        _MATRIX_CACHE = (document, suite, profiles, conditions)
    return _MATRIX_CACHE


def _scenario(scenario_id: str):
    """场景 ID 与标题的唯一来源：interfaces/scenarios.py。"""
    from interfaces.scenarios import get_scenario

    return get_scenario(scenario_id)


def _row(condition: dict[str, Any], suite: dict[str, Any], profiles: dict[str, Any]) -> dict[str, Any]:
    scenario_id = str(condition.get("scenario_id"))
    scenario = _scenario(scenario_id)
    profile_name = str(condition.get("network_profile") or "normal")
    profile = profiles.get(profile_name) or {}
    timeout = float(suite.get("startup_timeout_seconds", 30)) + float(
        suite.get("discovery_timeout_seconds", 30)
    )
    return {
        # 根 README §5 必需字段
        "scenario_name": scenario_id,
        "case": str(condition.get("scenario_name")),
        "condition_id": str(condition.get("condition_id")),
        "title": str(condition.get("scenario_title") or scenario.title),
        "payload_size_bytes": int(condition.get("payload_size_bytes") or 0),
        "publish_rate_hz": float(condition.get("publish_rate_hz") or 0),
        "publisher_count": int(condition.get("publisher_count") or 1),
        "subscriber_count": int(condition.get("subscriber_count") or 1),
        "message_count": int(condition.get("message_count") or 0),
        "duration_seconds": float(condition.get("duration_seconds") or 0),
        "repeats": int(condition.get("repeats") or suite.get("default_repeats") or 1),
        "random_seed": int(condition.get("random_seed") or 0),
        "warmup_seconds": float(condition.get("warmup_seconds") or 0),
        "drain_seconds": float(condition.get("drain_seconds") or 0),
        "timeout_seconds": timeout,
        "network_delay_ms": float(profile.get("latency_ms") or 0),
        "network_jitter_ms": float(profile.get("jitter_ms") or 0),
        "network_loss_rate": float(profile.get("packet_loss_percent") or 0) / 100.0,
        "network_profile": profile_name,
        "transport_mode": str(condition.get("transport_mode") or "udp").lower(),
        "qos_profile": str(condition.get("qos_profile") or "reliable"),
        # DDS 额外字段
        "scenario_id": scenario_id,
        "scenario_title": str(condition.get("scenario_title") or scenario.title),
        "scenario_description": scenario.description,
        "status": str(condition.get("capability") or "supported"),
        "capability": str(condition.get("capability") or "supported"),
        "capability_note": condition.get("capability_note"),
        "limitation": condition.get("capability_note"),
        "enabled": bool(condition.get("enabled", True)),
        "fault_plan": condition.get("fault_plan") or {"kind": "none"},
        "transport_detail": _transport_detail(str(condition.get("transport_mode") or "udp")),
        "rate_scope": "per_publisher",
    }


_TRANSPORT_DETAILS = {
    "udp": "UDPv4, builtin transports disabled, data sharing off",
    "shm": "SHM descriptor, builtin transports disabled, data sharing off",
    "default": "builtin transports (UDPv4 + SHM), data sharing off",
}


def _transport_detail(mode: str) -> str:
    return _TRANSPORT_DETAILS.get(mode, mode)


# --------------------------------------------------------------------------- 控制台钩子


def metadata() -> dict[str, Any]:
    """中间件元数据与真实可用性（根 README §4 / DDS_readme_1.md §3.3）。"""
    try:
        _document, suite, profiles, conditions = _load_matrix()
        matrix_error = None
    except Exception as exc:
        suite, profiles, conditions = {}, {}, []
        matrix_error = f"{type(exc).__name__}: {exc}"
    ok, detail = probe_environment()
    version, version_source = _detect_version(suite)
    notes: list[str] = []
    if matrix_error:
        notes.append(f"条件矩阵读取失败：{matrix_error}")
    if ok:
        notes.append(
            f"{VENDOR} {version or '未知版本'} + Python 绑定 "
            f"{suite.get('fastdds_python_version') or '未知'}；"
            f"transport 使用显式 UDPv4 或 SHM descriptor"
        )
        notes.append(f"S01–S12 共 {len(conditions)} 个标准条件，指标全部来自真实测量，不生成模拟数据。")
        notes.append(
            "S09 弱网三档与 S11 网络中断需要四种中间件共用的外部注入器，"
            "条件保留在目录中并标注 not_tested。"
        )
    else:
        notes.append(f"环境未就绪：{detail}")
        notes.append("请先执行 DDS/setup_windows.bat，详见 DDS/SETUP.md")
    notes.extend(_tool_advisories())
    return {
        "id": MIDDLEWARE_ID,
        "name": MIDDLEWARE_ID,
        "label": MIDDLEWARE_LABEL,
        "available": bool(ok),
        "version": version if ok else None,
        "version_source": version_source if ok else None,
        "vendor": VENDOR,
        "python_binding": suite.get("fastdds_python_version"),
        "transport_options": list(TRANSPORT_OPTIONS),
        "qos_options": [dict(item) for item in QOS_OPTIONS],
        "condition_count": len(conditions),
        "supported_condition_count": sum(
            1 for c in conditions if str(c.get("capability") or "supported") == "supported"
        ),
        "notes": notes,
        "environment_detail": detail,
    }


def catalog() -> list[dict[str, Any]]:
    """返回 S01–S12 全部标准条件；与是否编译/是否可用无关。"""
    global _ROWS_CACHE
    if _ROWS_CACHE is None:
        _document, suite, profiles, conditions = _load_matrix()
        _ROWS_CACHE = [_row(condition, suite, profiles) for condition in conditions]
    return [dict(row) for row in _ROWS_CACHE]


def base_config() -> dict[str, Any]:
    """返回引擎全局配置（写入 spec.json 的 config）。"""
    document, _suite, _profiles, _conditions = _load_matrix()
    return dict(document)


def runner_command() -> list[str]:
    """返回启动执行器的命令行（控制台会追加 spec.json 路径）。"""
    interpreter = _venv_python()
    return [
        str(interpreter) if interpreter is not None else sys.executable,
        str(FOLDER / "console_runner.py"),
    ]


# --------------------------------------------------------------------------- 参数校验


def _number(values: dict[str, Any], key: str, default: float, minimum: float, maximum: float, integer: bool = False):
    raw = values.get(key, default)
    if raw is None or raw == "":
        raw = default
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 必须是数字") from None
    if value < minimum:
        raise ValueError(f"参数 {key} 不能小于 {minimum}")
    if value > maximum:
        raise ValueError(f"参数 {key} 不能大于 {maximum}")
    return value


def _build_case(
    condition: dict[str, Any],
    suite: dict[str, Any],
    profiles: dict[str, Any],
    configuration: dict[str, Any] | None,
) -> dict[str, Any]:
    values = dict(configuration or {})
    row = _row(condition, suite, profiles)
    profile_name = row["network_profile"]
    profile = profiles.get(profile_name) or {}

    payload = _number(values, "payload_size_bytes", row["payload_size_bytes"], 1, 1_048_576, True)
    rate = _number(values, "publish_rate_hz", row["publish_rate_hz"], 0, 1_000_000)
    publishers = _number(values, "publisher_count", row["publisher_count"], 1, 8, True)
    subscribers = _number(values, "subscriber_count", row["subscriber_count"], 1, 16, True)
    message_count = _number(values, "message_count", row["message_count"], 0, 100_000_000, True)
    duration = _number(values, "duration_seconds", row["duration_seconds"], 0, 3600)
    warmup = _number(values, "warmup_seconds", row["warmup_seconds"], 0, 60)
    drain = _number(values, "drain_seconds", row["drain_seconds"], 0, 300)
    repeats = _number(values, "repeats", row["repeats"], 1, 30, True)
    seed = _number(values, "random_seed", row["random_seed"], 0, 2 ** 31 - 1, True)
    timeout = _number(values, "timeout_seconds", row["timeout_seconds"], 2, 600)
    loss = _number(values, "network_loss_rate", row["network_loss_rate"], 0, 1)
    delay = _number(values, "network_delay_ms", row["network_delay_ms"], 0, 60000)
    jitter = _number(values, "network_jitter_ms", row["network_jitter_ms"], 0, 60000)

    if payload < 1:
        raise ValueError("payload_size_bytes 至少为 1 字节")
    if payload > 1_048_576:
        raise ValueError("payload_size_bytes 不能超过 1 MiB（IDL 上限）")
    if message_count == 0 and duration == 0:
        raise ValueError("message_count 与 duration_seconds 至少有一个大于 0")

    transport = str(values.get("transport_mode") or row["transport_mode"]).lower()
    if transport not in {"udp", "shm"}:
        raise ValueError("transport_mode 仅支持 udp 或 shm（不使用 default，避免 UDP/SHM 混测）")
    qos = str(values.get("qos_profile") or row["qos_profile"])
    if qos not in {"reliable", "best_effort"}:
        raise ValueError("qos_profile 仅支持 reliable 或 best_effort")

    matched_profile = (
        abs(loss * 100.0 - float(profile.get("packet_loss_percent") or 0)) < 1e-9
        and abs(delay - float(profile.get("latency_ms") or 0)) < 1e-9
        and abs(jitter - float(profile.get("jitter_ms") or 0)) < 1e-9
    )
    effective_profile = profile_name if matched_profile else "console_custom"

    capability = str(condition.get("capability") or "supported")
    capability_note = condition.get("capability_note")
    if capability == "supported" and (loss > 0 or delay > 0 or jitter > 0):
        capability = "not_tested"
        capability_note = (
            "网络损伤（丢包/延迟/抖动）需要四种中间件共用的外部网络仿真器；"
            "未确认注入前不产生数据"
        )

    case = dict(row)
    case.update(
        {
            "payload_size_bytes": int(payload),
            "publish_rate_hz": float(rate),
            "publisher_count": int(publishers),
            "subscriber_count": int(subscribers),
            "message_count": int(message_count),
            "duration_seconds": float(duration),
            "warmup_seconds": float(warmup),
            "drain_seconds": float(drain),
            "random_seed": int(seed),
            "timeout_seconds": float(timeout),
            "network_loss_rate": float(loss),
            "network_delay_ms": float(delay),
            "network_jitter_ms": float(jitter),
            "network_profile": effective_profile,
            "transport_mode": transport,
            "transport_detail": _transport_detail(transport),
            "qos_profile": qos,
            "repeats": int(repeats),
            "case_repeats": int(repeats),
            "capability": capability,
            "capability_note": capability_note,
            "status": capability,
            "limitation": capability_note,
        }
    )
    return case


def build_cases(template_index, configuration, matrix) -> list[dict[str, Any]]:
    """把"当前表单参数"或"完整矩阵"展开成引擎可执行条件列表。

    ``matrix=True`` 忽略 configuration，返回该场景的全部标准条件；
    ``matrix=False`` 用 configuration 覆盖所选标准条件，返回 1 个条件。
    参数非法时抛 ``ValueError``（中文消息，控制台原样显示）。
    """
    _document, suite, profiles, conditions = _load_matrix()
    try:
        index = int(template_index)
    except (TypeError, ValueError):
        raise ValueError("无效的测试条件") from None
    if not 0 <= index < len(conditions):
        raise ValueError("无效的测试条件")
    condition = conditions[index]
    if matrix:
        scenario_id = condition["scenario_id"]
        selected = [c for c in conditions if c["scenario_id"] == scenario_id]
        return [_build_case(item, suite, profiles, None) for item in selected]
    return [_build_case(condition, suite, profiles, configuration)]


# --------------------------------------------------------------------------- 适配器形式


class Adapter(MiddlewareAdapter):
    """根 README §4 的 MiddlewareAdapter 形式（供控制台或程序化调用）。"""

    name = MIDDLEWARE_ID

    def metadata(self) -> dict[str, Any]:
        return metadata()

    def catalog(self) -> list[dict[str, Any]]:
        return catalog()

    def build_cases(self, template_index, configuration, matrix) -> list[dict[str, Any]]:
        return build_cases(template_index, configuration, matrix)

    def base_config(self) -> dict[str, Any]:
        return base_config()

    def runner_command(self) -> list[str]:
        return runner_command()

    def run(self, scenario, parameters):
        """单轮直调：跑 1 轮真实测量并返回统一格式的单轮结果。

        环境未就绪时返回 ``status="not_tested"``、指标全为 null，不伪造数据。
        """
        from interfaces import ScenarioResult

        parameters = dict(parameters or {})
        scenario_id = str(
            getattr(scenario, "scenario_id", None) or getattr(scenario, "name", "") or ""
        )
        rows = catalog()
        index = next(
            (i for i, row in enumerate(rows) if row["scenario_id"] == scenario_id), None
        )
        if index is None:
            raise ValueError(f"未知场景：{scenario_id}")
        parameters.setdefault("repeats", 1)
        case = build_cases(index, parameters, False)[0]
        case["case_repeats"] = 1
        case["repeats"] = 1

        from console_runner import execute_spec

        output = Path(parameters.get("output_dir") or (FOLDER / "results" / "single_run"))
        spec = {
            "middleware": MIDDLEWARE_ID,
            "job_id": f"single_{case['condition_id']}",
            "config": base_config(),
            "cases": [case],
            "plan": [
                {
                    "scenario_id": case["scenario_id"],
                    "scenario_name": case["scenario_id"],
                    "planned_repeats": 1,
                }
            ],
            "output": str(output),
            "logs": str(output / "logs"),
        }
        document = execute_spec(spec)
        runs = document.get("runs") or []
        run = runs[-1] if runs else {"status": document.get("status")}
        metrics = {
            key: run.get(key)
            for key in (
                "latency_ms",
                "latency_p95_ms",
                "latency_p99_ms",
                "latency_std_ms",
                "throughput_mbps",
                "jitter_ms",
                "cpu_percent",
                "memory_mb",
                "packet_loss",
                "final_packet_loss",
                "startup_time_ms",
                "discovery_time_ms",
                "recovery_time_ms",
            )
        }
        return ScenarioResult(
            middleware=MIDDLEWARE_ID,
            middleware_version=run.get("middleware_version"),
            scenario_id=scenario_id,
            status=str(run.get("status")),
            metrics=metrics,
            samples={},
            configuration=case,
            errors=list(run.get("errors") or []),
            started_at=run.get("test_start_time"),
            finished_at=run.get("test_end_time"),
        )


def create_adapter() -> Adapter:
    return Adapter()
