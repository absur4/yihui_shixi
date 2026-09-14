"""DDS 控制台执行器（子进程入口）。

用法::

    python DDS/console_runner.py <job_dir>/spec.json

spec.json 由控制台生成（根 README §8）::

    {middleware, config, cases, plan, output, logs}

产物（与控制台归档目录一致）::

    <output>/result.json                     套件级结果，控制台轮询
    <output>/runs/<run_id>.json              单轮统一字段结果
    <output>/artifacts/<run_id>/            每轮原始证据
    <output>/artifacts/<run_id>/subscriber-0.result.json   延迟曲线样本

stdout 每行 ``[HH:MM:SS] 消息``；每轮打印 ``START`` / ``END``；结束打印
``RESULT <output>/result.json status=…``。退出码：0 全部完成 / 130 取消 / 其它失败。

本模块属于「编排层」：顶层不导入任何原生依赖。引擎（`fastdds_bench.controller`）
只在环境探测通过、且真正要测量时才被惰性导入；环境未就绪时全部轮次返回
``status="not_tested"``、指标全为 ``null``，并写明原因，绝不伪造数据。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
if str(FOLDER) not in sys.path:
    sys.path.insert(0, str(FOLDER))

from fastdds_bench.runtime import configure_runtime  # noqa: E402  (stdlib-only)

# 与 launch.py / endpoint_entry.py 一致：把 runtime/Lib/site-packages 与 runtime/bin
# 注入 sys.path 和 DLL 搜索路径。引擎的 probe_fastdds_runtime() 是真实 import，
# 少了这一步会在 import BenchmarkMessage 时失败。
configure_runtime()

from fastdds_bench.results import (  # noqa: E402  (stdlib-only module)
    map_engine_run,
    not_tested_run,
    sample_document,
    suite_document,
)
from fastdds_bench.util import (  # noqa: E402  (stdlib-only module)
    atomic_write_json,
    read_json,
    safe_id,
    utc_now_iso,
)

MIDDLEWARE_ID = "dds"
CONFIG_PATH = FOLDER / "config.yaml"

SUITE_LIMITATIONS = [
    "缺失/未测量/不适用的指标一律写 null，不写 0，也不插值或生成数据",
    "DDS 的 RELIABLE 是协议级重传，不等价于其他中间件的应用层恢复",
    "transport_mode 是映射值（udp=显式 UDPv4，shm=显式 SHM），不同 transport_mode 不得同组平均",
    "DDS 无 Broker/Router 进程，cpu_percent/memory_mb 只统计端点进程",
]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


class _Tee:
    """把同一行日志同时写到 stdout 与 ``<output>/logs/suite.log``。"""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.stream = None

    def attach(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("w", encoding="utf-8", newline="\n")

    def write(self, message: str) -> None:
        log(message)
        if self.stream is not None:
            self.stream.write(
                f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
            )
            self.stream.flush()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def case_repeats(case: dict) -> int:
    """重复次数：优先 case_repeats，其次 repeats，默认 1（根 README §8.1）。"""
    raw = case.get("case_repeats")
    if raw in (None, ""):
        raw = case.get("repeats")
    try:
        repeats = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, repeats)


def _network_profile_for_case(case: dict) -> dict:
    loss = float(case.get("network_loss_rate") or 0)
    delay = float(case.get("network_delay_ms") or 0)
    jitter = float(case.get("network_jitter_ms") or 0)
    impaired = loss > 0 or delay > 0 or jitter > 0
    return {
        "latency_ms": delay,
        "jitter_ms": jitter,
        "packet_loss_percent": loss * 100.0,
        "bandwidth_mbps": 0,
        "impairment_required": impaired,
        # 没有真实注入器就不要声称注入成功。
        "external_impairment_confirmed": not impaired,
    }


class EngineBridge:
    """把 spec.json 里的控制台条件桥接成引擎可执行条件。

    只有环境探测通过时才会构造本对象，因此可以在这里安全地导入引擎层。
    """

    def __init__(self, spec: dict, log_fn) -> None:
        from fastdds_bench import config as engine_config
        from fastdds_bench import controller

        self.engine_config = engine_config
        self.controller = controller
        self.conditions = [
            engine_config.condition_from_console_case(case)
            for case in spec.get("cases") or []
        ]
        document = dict(spec.get("config") or {})
        if not document:
            document = engine_config.load_raw_config(CONFIG_PATH)
        document["conditions"] = self.conditions
        profiles = document.setdefault("network_profiles", {})
        for case, condition in zip(spec.get("cases") or [], self.conditions):
            name = condition["network_profile"]
            if name not in profiles:
                profiles[name] = _network_profile_for_case(case)
        self.config, self.warnings = engine_config.normalize_config(document)
        for warning in self.warnings:
            log_fn(f"CONFIG WARNING {warning}")
        self.environment = controller.collect_environment(self.config)
        self.probe = controller.probe_fastdds_runtime()
        self.environment["runtime_probe"] = self.probe

    def measure(self, condition_index, repeat_index, ordinal, run_dir, run_id, suite_id):
        condition = self.config["conditions"][condition_index]
        return self.controller.run_once(
            self.config,
            condition,
            suite_id,
            repeat_index,
            ordinal,
            Path(run_dir).parent,
            run_dir=Path(run_dir),
            run_id=run_id,
        )

    def write_samples(self, condition_index, run_dir, run_id) -> str | None:
        condition = self.config["conditions"][condition_index]
        run_dir = Path(run_dir)
        publisher_results = []
        subscriber_results = []
        for index in range(int(condition["publisher_count"])):
            found = self.controller._collect_endpoint_results(run_dir, "publisher", index)
            if found:
                publisher_results.append(found)
        for index in range(int(condition["subscriber_count"])):
            found = self.controller._collect_endpoint_results(run_dir, "subscriber", index)
            if found:
                subscriber_results.append(found)
        if not subscriber_results:
            return None
        return self.controller.write_run_sample_file(
            self.config["conditions"][condition_index],
            run_dir,
            publisher_results,
            subscriber_results,
            run_id,
        )


def _build_bridge(spec: dict, log_fn) -> tuple[EngineBridge | None, str | None]:
    """返回 (bridge, 不可用原因)。任何异常都转成"不可用原因"，不抛出。"""
    try:
        from adapter import probe_environment
    except Exception as exc:  # adapter.py 本身出问题时也要能降级
        return None, f"契约层导入失败：{type(exc).__name__}: {exc}"
    try:
        ok, detail = probe_environment()
    except Exception as exc:
        return None, f"环境探测异常：{type(exc).__name__}: {exc}"
    if not ok:
        return None, f"Fast DDS 环境未就绪：{detail}"
    try:
        return EngineBridge(spec, log_fn), None
    except Exception as exc:
        return None, f"DDS 引擎初始化失败：{type(exc).__name__}: {exc}"


def execute_spec(spec: dict, output_log: _Tee | None = None) -> dict:
    """按 spec 逐条件逐轮执行并落盘，返回套件级文档。"""
    logger = output_log or _Tee()
    output = Path(spec["output"]).resolve() if spec.get("output") else FOLDER / "results" / "unnamed"
    logs = Path(spec["logs"]).resolve() if spec.get("logs") else output / "logs"
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    logger.attach(logs / "suite.log")

    cases = list(spec.get("cases") or [])
    plan = list(spec.get("plan") or [])
    if not cases:
        raise ValueError("spec.json 里没有可执行条件（cases 为空）")

    suite_id = safe_id(
        f"{utc_now_iso().replace(':', '_').replace('.', '_')}_{spec.get('job_id') or 'job'}",
        max_length=96,
    )
    planned_runs = sum(case_repeats(case) for case in cases)
    planned_by_scenario = {
        str(item.get("scenario_name")): item.get("planned_repeats") for item in plan
    }
    started = utc_now_iso()
    bridge, unavailable = _build_bridge(spec, logger.write)
    environment = bridge.environment if bridge else {}
    if bridge is None:
        logger.write(f"降级运行：{unavailable}")
    else:
        logger.write(
            f"引擎就绪：{environment.get('vendor')} {environment.get('version')} · "
            f"Python {environment.get('python_version')}"
        )
    logger.write(f"SUITE 开始：{len(cases)} 个条件，共 {planned_runs} 轮")
    for item in plan:
        logger.write(
            f"PLAN {item.get('scenario_id') or item.get('scenario_name')} "
            f"{item.get('scenario_name') or ''} "
            f"planned_repeats={item.get('planned_repeats')}"
        )

    runs: list[dict] = []
    limitations = list(SUITE_LIMITATIONS)
    if bridge is None:
        limitations.insert(0, f"本次未进行真实测量：{unavailable}")

    bridge_suite = ((getattr(bridge, "config", None) or {}).get("suite") or {}) if bridge else {}

    def publish(status: str, end_time: str | None) -> dict:
        document = suite_document(
            run_id=suite_id,
            status=status,
            planned_runs=planned_runs,
            test_start_time=started,
            test_end_time=end_time,
            environment=environment,
            configuration={
                "source": "console_runner",
                "middleware": spec.get("middleware") or MIDDLEWARE_ID,
                "cases": len(cases),
                "planned_by_scenario": planned_by_scenario,
                "measured_runs": sum(1 for r in runs if r.get("status") == "completed"),
                "not_tested_runs": sum(
                    1 for r in runs if r.get("status") == "not_tested"
                ),
                "error_runs": sum(1 for r in runs if r.get("status") == "error"),
                "rate_scope": "per_publisher",
                "default_repeats": bridge_suite.get("default_repeats"),
                "formal_repeats": bridge_suite.get("formal_repeats"),
                "config_warnings": (bridge.warnings if bridge else []),
                "runtime_probe": (bridge.probe if bridge else None),
            },
            runs=runs,
            limitations=limitations,
            middleware_version=(
                environment.get("version") if bridge else None
            ),
        )
        atomic_write_json(output / "result.json", document)
        return document

    publish("running", None)
    ordinal = 0
    try:
        try:
            for case_index, case in enumerate(cases):
                condition_id = str(case.get("condition_id") or f"case{case_index}")
                repeats = case_repeats(case)
                capability = str(case.get("capability") or "supported")
                note = case.get("capability_note") or case.get("limitation")
                for repeat_index in range(1, repeats + 1):
                    run_id = safe_id(
                        f"{suite_id}_{condition_id}_r{repeat_index}", max_length=120
                    )
                    run_dir = output / "artifacts" / run_id
                    logger.write(f"START {condition_id} repeat={repeat_index}/{repeats}")
                    if capability != "supported":
                        reason = note or f"该条件的能力标记为 {capability}，当前不产生数据"
                        run = not_tested_run(
                            case,
                            repeat=repeat_index,
                            run_id=run_id,
                            suite_id=suite_id,
                            status=capability,
                            reason=reason,
                            environment=environment,
                        )
                    elif bridge is None:
                        run = not_tested_run(
                            case,
                            repeat=repeat_index,
                            run_id=run_id,
                            suite_id=suite_id,
                            status="not_tested",
                            reason=unavailable or "Fast DDS 环境未就绪",
                            environment=environment,
                        )
                    else:
                        engine_run = bridge.measure(
                            case_index, repeat_index, ordinal, run_dir, run_id, suite_id
                        )
                        run = map_engine_run(engine_run, environment=environment)
                        name = bridge.write_samples(case_index, run_dir, run_id)
                        if not name:
                            run["limitations"] = list(run.get("limitations") or []) + [
                                "本轮没有得到任何被校验通过的接收样本，样本文件为空列表"
                            ]
                    # 无论是否真实测量，都保证样本文件存在（前端延迟曲线直接读它）。
                    run_dir.mkdir(parents=True, exist_ok=True)
                    sample_file = run_dir / "subscriber-0.result.json"
                    if not sample_file.exists():
                        atomic_write_json(
                            sample_file,
                            sample_document([], run_id=run_id, condition_id=condition_id),
                        )
                    run["samples_path"] = f"artifacts/{run_id}/subscriber-0.result.json"
                    run["artifacts_directory"] = f"artifacts/{run_id}"
                    run["middleware_version"] = run.get("middleware_version") or (
                        environment.get("version") if bridge else None
                    )
                    atomic_write_json(output / "runs" / f"{run_id}.json", run)
                    runs.append(run)
                    publish("running", None)
                    logger.write(
                        f"END {condition_id} status={run.get('status')} "
                        f"sent={run.get('messages_sent')} "
                        f"received={run.get('messages_received')}"
                    )
                    ordinal += 1
        except KeyboardInterrupt:
            logger.write("Cancelled by user; completed runs retained")
            return publish("cancelled", utc_now_iso())

        failed = any(
            run.get("status") in {"error", "timeout", "cancelled"} for run in runs
        )
        return publish("error" if failed else "completed", utc_now_iso())
    finally:
        logger.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("用法: console_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(argv[0]).resolve()
    spec = read_json(spec_path)
    output = Path(spec["output"]).resolve() if spec.get("output") else spec_path.parent / "output"
    try:
        document = execute_spec(spec)
    except Exception as exc:  # 执行器自身失败也要给出可解析的结束行
        log(f"ERROR {type(exc).__name__}: {exc}")
        document = {"status": "error"}
    status = document.get("status", "error")
    log(f"RESULT {output / 'result.json'} status={status}")
    if status == "completed":
        return 0
    if status == "cancelled":
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
