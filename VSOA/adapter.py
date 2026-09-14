"""VSOA 统一适配器：把 VSOA/vsoa_py/standalone 接入 songfei 控制台契约。

控制台需要的五个钩子（契约见仓库根 README.md 与本目录 console_runner.py）：
    metadata()                                          → 中间件元数据与可用性
    catalog()                                           → S01–S12 全部标准条件（UI 输入字段）
    build_cases(template_index, configuration, matrix)  → 引擎可执行条件列表
    base_config()                                       → 引擎全局配置（写入 spec.config）
    runner_command()                                    → 启动执行器的命令行
另外按根 README §4 提供 create_adapter() / Adapter 类（含 run() 单轮直调）。
"""

import sys
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
VSOA_PY = FOLDER / "vsoa_py"
if str(VSOA_PY) not in sys.path:
    sys.path.insert(0, str(VSOA_PY))

MIDDLEWARE_ID = "vsoa"
BASE_CONFIG = VSOA_PY / "delivery_template" / "config.yaml"

try:
    import vsoa as vsoa_package  # noqa: E402
    from standalone.configuration import load_config, validate_case  # noqa: E402
    from standalone.qualification import qualification_cases  # noqa: E402
    IMPORT_ERROR = None
except Exception as error:  # 环境异常时不抛异常，改为 available=False
    IMPORT_ERROR = f"{type(error).__name__}: {error}"

_CATALOG = None  # (base_config, templates, cases)


def _load_catalog():
    """惰性加载并缓存 (base_config, templates, cases)。"""
    global _CATALOG
    if _CATALOG is None:
        if IMPORT_ERROR:
            raise RuntimeError(f"VSOA 引擎不可用：{IMPORT_ERROR}")
        config, _expanded = load_config(BASE_CONFIG)
        cases, _plan = qualification_cases(config, "all")
        templates = [{
            "scenario_name": case["scenario_id"],
            "case": case["scenario_name"],
            "condition_id": case["scenario_name"],
            "title": case.get("scenario_title"),
            "payload_size_bytes": case["message_size_bytes"],
            "publish_rate_hz": case["publish_rate_hz"],
            "publisher_count": case["publisher_count"],
            "subscriber_count": case["subscriber_count"],
            "message_count": case["message_count"],
            "duration_seconds": case["duration_seconds"],
            "repeats": case["case_repeats"],
            "random_seed": case["seed"],
            "warmup_seconds": case["warmup_seconds"],
            "drain_seconds": case["drain_seconds"],
            "timeout_seconds": case["startup_timeout_seconds"],
            "network_delay_ms": case["network_delay_ms"],
            "network_jitter_ms": case["network_jitter_ms"],
            "network_loss_rate": case["loss_rate"],
            "network_profile": case.get("network_profile"),
            "transport_mode": case["transport_mode"],
            "qos_profile": case.get("qos_profile", "default"),
        } for case in cases]
        _CATALOG = (config, templates, cases)
    return _CATALOG


def metadata():
    if IMPORT_ERROR:
        return {"id": MIDDLEWARE_ID, "name": MIDDLEWARE_ID, "label": "VSOA", "available": False,
                "version": None, "transport_options": [], "qos_options": [],
                "notes": [f"VSOA 引擎导入失败：{IMPORT_ERROR}"]}
    _config, templates, _cases = _load_catalog()
    return {"id": MIDDLEWARE_ID, "name": MIDDLEWARE_ID, "label": "VSOA", "available": True,
            "version": getattr(vsoa_package, "__version__", None),
            "transport_options": ["tcp", "udp"],
            "qos_options": [{"value": "default", "label": "原生默认"}],
            "notes": [f"VSOA 已全量接入：S01–S12 共 {len(templates)} 个标准条件，全部指标由真实引擎测量，不生成模拟数据。",
                      "网络条件通过真实 UDP 代理进程注入（丢包/延迟/抖动/断网）；TCP 模式不支持网络损伤。",
                      "UDP 单消息上限 60 KiB，TCP 大消息自动应用层分片；发布者 ≤ 8、订阅者 ≤ 16。"]}


def catalog():
    """返回 UI 条件下拉数据（字段见根 README §5）。"""
    return _load_catalog()[1]


def base_config():
    """返回引擎全局配置（写入 spec.config）。"""
    return _load_catalog()[0]


def _number(values, key, default, minimum, maximum, integer=False):
    raw = values.get(key, default)
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 必须是数字") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"参数 {key} 低于允许的最小值 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"参数 {key} 超过允许的最大值 {maximum}")
    return value


def _translate_validation(message):
    rules = (
        ("message_size_bytes", "消息大小超出限制：UDP 单消息最大 60000 字节，TCP 最大 16 MiB"),
        ("publish_rate_hz", "发送速率需在 1–50000 Hz 之间"),
        ("publisher_count", "VSOA 发布者数量需在 1–8 之间"),
        ("subscriber_count", "VSOA 订阅者数量需在 1–16 之间"),
        ("Network impairment", "网络损伤（丢包/延迟/抖动）仅支持 UDP 传输模式"),
        ("planned deliveries", "单轮计划交付总数超过 200 万，请降低时长、速率或拓扑规模"),
        ("duration_seconds", "正式时长需在 0.1–3600 秒之间"),
        ("loss_rate", "丢包率需在 0–100% 之间"),
        ("network_delay_ms", "基础延迟需在 0–1000 ms 之间"),
        ("network_jitter_ms", "网络抖动需在 0–1000 ms 之间"),
        ("transport_mode", "传输模式仅支持 TCP 或 UDP"),
    )
    for key, text in rules:
        if key in message:
            return text
    return f"配置校验失败：{message}"


def _build_single_case(cases, index, configuration):
    """用控制台表单参数覆盖标准条件，返回可直接交给引擎的 case。"""
    case = dict(cases[index])
    values = configuration or {}
    case.update(
        message_size_bytes=_number(values, "payload_size_bytes", case["message_size_bytes"], 1, 16 * 1024 ** 2, True),
        publish_rate_hz=_number(values, "publish_rate_hz", case["publish_rate_hz"], 1, 50000),
        publisher_count=_number(values, "publisher_count", case["publisher_count"], 1, 8, True),
        subscriber_count=_number(values, "subscriber_count", case["subscriber_count"], 1, 16, True),
        message_count=_number(values, "message_count", case["message_count"], 0, 100_000_000, True),
        duration_seconds=_number(values, "duration_seconds", case["duration_seconds"], 0.1, 3600),
        warmup_seconds=_number(values, "warmup_seconds", case["warmup_seconds"], 0, 60),
        drain_seconds=_number(values, "drain_seconds", case["drain_seconds"], 0.05, 30),
        network_delay_ms=_number(values, "network_delay_ms", case["network_delay_ms"], 0, 1000),
        network_jitter_ms=_number(values, "network_jitter_ms", case["network_jitter_ms"], 0, 1000),
    )
    case["case_repeats"] = _number(values, "repeats", case["case_repeats"], 1, 30, True)
    case["seed"] = _number(values, "random_seed", case["seed"], 0, 2 ** 31 - 1, True)
    case["startup_timeout_seconds"] = _number(values, "timeout_seconds", case["startup_timeout_seconds"], 2, 120)
    case["loss_rate"] = _number(values, "network_loss_rate", case["loss_rate"], 0, 1)
    transport = str(values.get("transport_mode") or case["transport_mode"]).lower()
    if transport not in {"tcp", "udp"}:
        raise ValueError("传输模式仅支持 TCP 或 UDP")
    case["transport_mode"] = transport
    case["qos_profile"] = str(values.get("qos_profile") or case.get("qos_profile") or "default")
    case["payload_size_bytes"] = case["message_size_bytes"]
    case["network_loss_rate"] = case["loss_rate"]
    case["random_seed"] = case["seed"]
    try:
        validate_case(case)
    except ValueError as error:
        raise ValueError(_translate_validation(str(error))) from None
    return case


def build_cases(template_index, configuration, matrix):
    """构建本轮要执行的引擎条件列表；校验失败抛 ValueError（中文消息）。

    matrix=True：忽略 configuration，返回该场景全部标准条件。
    matrix=False：把 configuration 覆盖到所选标准条件上，返回 1 个条件。
    """
    _config, templates, cases = _load_catalog()
    try:
        index = int(template_index)
    except (TypeError, ValueError):
        raise ValueError("无效的测试条件") from None
    if not 0 <= index < len(cases):
        raise ValueError("无效的测试条件")
    scenario_id = templates[index]["scenario_name"]
    if matrix:
        return [dict(case) for case in cases if case["scenario_id"] == scenario_id]
    return [_build_single_case(cases, index, configuration)]


def runner_command():
    """返回启动执行器的命令行（控制台会追加 spec.json 路径）。"""
    return [sys.executable, str(FOLDER / "console_runner.py")]


class Adapter:
    """根 README §4 的 MiddlewareAdapter 形式（供控制台或程序化调用）。"""

    name = MIDDLEWARE_ID

    def metadata(self):
        return metadata()

    def catalog(self):
        return catalog()

    def build_cases(self, template_index, configuration, matrix):
        return build_cases(template_index, configuration, matrix)

    def base_config(self):
        return base_config()

    def runner_command(self):
        return runner_command()

    def run(self, scenario, parameters):
        """单轮直调：按 scenario_id 匹配标准条件，覆盖参数后跑 1 轮，返回该轮结果。"""
        parameters = dict(parameters or {})
        scenario_id = getattr(scenario, "scenario_id", None) or getattr(scenario, "scenario_name", None)
        config, _templates, cases = _load_catalog()
        index = next((i for i, case in enumerate(cases) if case["scenario_id"] == scenario_id), None)
        if index is None:
            raise ValueError(f"未知场景：{scenario_id}")
        case = _build_single_case(cases, index, parameters)
        case["case_repeats"] = 1
        from standalone.engine import run_suite
        output = Path(parameters.get("output_dir") or (FOLDER / "results" / f"single_{case['scenario_name']}"))
        report = run_suite(config, [case], output, output / "logs", lambda message: None, "all", [], False)
        runs = report.get("runs") or []
        return runs[-1] if runs else {"status": report.get("status"), "run_id": report.get("run_id")}


def create_adapter():
    return Adapter()
