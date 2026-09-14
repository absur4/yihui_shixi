# DDS 模块化接入改造要求

> 读者：负责 `DDS/` 的实现者与其 AI。
> **场景与指标要求以《四种通信中间件统一测试需求文档》1.0（仓库根 PDF）为准；文件与接口要求以仓库根 `README.md` 为准。**
> 最终目标：让 DDS 成为一个**可以被 `songfei/` 控制台"即插即用"的模块**——控制台不改一行代码，仅凭你目录内的契约文件就能列出 DDS、展开 S01–S12 条件、启动真实测量、收集结果并在同一套页面展示。
>
> 参考实现：`VSOA/`（引擎 `vsoa_py/standalone` + 控制台接入方式 `songfei/app.py`、`songfei/vsoa_runner.py`）。

---

## 0. 完成标准（先看这一节，全部勾上才算交付）

- [ ] `DDS/adapter.py` 存在，且在**没有安装 Fast DDS 的解释器里也能被 import 成功**（这是"即插即用"的硬门槛）；实现控制台的五个钩子：`metadata()`、`catalog()`、`build_cases(index, configuration, matrix)`、`runner_command()`（+ 可选 `base_config()`）。
- [ ] `DDS/console_runner.py` 存在，`python DDS/console_runner.py <job>/spec.json` 可独立运行。
- [ ] 控制台启动后，中间件下拉框出现 `DDS`：环境就绪显示"可用"，未就绪显示"入口缺失"并在 `notes` 写明缺什么，**任何情况下不得抛异常导致控制台启动失败**。
- [ ] `catalog()` 返回 **S01–S12 全部场景**的条件（能力不足的条件也必须出现，用 `not_tested`/`unsupported` 标注）。
- [ ] **冒烟跑通即可，不需要完整跑一遍测试**：任选 1 个条件、跑 1 轮（`case_repeats=1`），能产出 `output/result.json` + `output/runs/<run_id>.json` + 样本文件（`artifacts/<run_id>/subscriber-0.result.json`）即算通过。**不要求跑 S01–S12 全量矩阵，也不要求跑满 5 次重复**；全量正式测试由控制台统一发起。环境未就绪时返回 `status="not_tested"` + `limitations`、指标全 `null`，同样算跑通（证明"链路通"，只是没数据）。
- [ ] **更新 `DDS/README.md`：写一份接口清单，每个接口只写"名称 + 一句话功能"**（不需要签名、字段表、示例），允许全部接口集中写在 `README.md` 一个文件里（见第 8 节）。
- [ ] `git status` 只显示 `DDS/` 下的改动（不得改 `songfei/`、`interfaces/`、其他中间件目录）。
- [ ] 提交前清理 `__pycache__/`、编译产物与测试输出。

---

## 1. 控制台会这样调用你（这是"模块化"的全部含义）

```
控制台启动
  └─ import <DDS>/adapter.py  →  create_adapter()
        ├─ metadata()                          → /api/init 的 middleware 项（含真实 available）
        ├─ catalog()                           → /api/init 的 catalogs["dds"]（S01–S12 全部条件）
        ├─ build_cases(index, config, matrix)   → 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件
        └─ runner_command()                     → 启动执行器的命令行（含解释器选择）

用户点"开始测试"
  └─ 控制台写入 spec.json
       └─ 子进程：python DDS/console_runner.py <job_dir>/spec.json
             ├─ 写 output/runs/<run_id>.json（每轮）
             ├─ 原子更新 output/result.json（每次轮次结束都刷新）
             └─ stdout: [HH:MM:SS] … ；结束打印 RESULT … status=…

控制台轮询 output/result.json → /api/results → 页面指标卡 / 曲线 / 报告
```

要点：**控制台只认字段名，不认你的库**。你内部用 `fastdds`、`launch.py`、`controller.py` 都可以，但对外必须是本节这套形状。

---

## 2. 必须交付的文件与目录（根 README §3）

```
DDS/
├─ adapter.py            ← 必须：契约层（零原生依赖）
├─ console_runner.py     ← 必须：子进程执行器
├─ README.md             ← 必须：更新为"依赖 + 启动命令 + S01–S12 能力矩阵 + 限制 + 一次真实测试结果"
├─ requirements.txt      ← 必须：PyYAML / psutil / jsonschema / pywin32 等
├─ SETUP.md              ← 必须（DDS 特有）：原生环境准备说明（见第 6 节）
├─ fastdds_bench/        ← 你的引擎（保持现状，不动测量逻辑）
├─ config.yaml           ← 条件矩阵（按第 4、5 节调整）
├─ results/<job_id>/     ← 你的作业产物（与控制台归档目录不冲突即可）
└─ （控制台归档）songfei/results/dds/<job_id>/{job.json, spec.json, console.log, output/...}
```

每个作业目录至少包含：`job.json`、`spec.json`、`console.log`、`output/result.json`、`output/runs/<run_id>.json`、`output/artifacts/<run_id>/`。

---

## 3. 接口要求（根 README §4 / §5 / §6 / §8）

### 3.1 `adapter.py` 必须实现（根 README §4）

```python
# —— 控制台实际调用的钩子（模块级函数，必需）——
MIDDLEWARE_ID = "dds"

def metadata() -> dict: ...        # 见 3.3
def catalog() -> list[dict]: ...   # 见 3.4
def build_cases(template_index, configuration, matrix) -> list[dict]: ...   # 必需，语义见下方说明
def base_config() -> dict: ...     # 可选：写入 spec.json 的 config
def runner_command() -> list[str]: ...   # 必需：如 [sys.executable, str(HERE / "console_runner.py")]

# —— 兼容根 README §4 的适配器形式（控制台两种写法都支持）——
from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec

class Adapter(MiddlewareAdapter):
    name = "dds"                       # 必须等于 middleware_id 与目录归档名
    def metadata(self) -> dict: return metadata()
    def catalog(self) -> list[dict]: return catalog()
    def build_cases(self, template_index, configuration, matrix): return build_cases(template_index, configuration, matrix)
    def run(self, scenario: ScenarioSpec, parameters: dict) -> ScenarioResult: ...   # 单轮直调（可选）

def create_adapter() -> Adapter:
    return Adapter()
```

**`build_cases()` 语义**（控制台点"开始测试"时调用，"当前条件"与"完整矩阵"都靠它）：

| 参数 | 说明 |
|---|---|
| `template_index` | int，指向 `catalog()` 返回列表的下标 |
| `configuration` | dict，前端表单的输入字段（`repeats` = 重复轮数、`network_loss_rate` 为 0–1 小数、`transport_mode` 小写） |
| `matrix` | bool。`True` = **忽略 configuration**，返回该场景全部标准条件；`False` = 用 configuration 覆盖所选标准条件，返回 1 个条件 |

- 返回的每个 case 必须带 `case_repeats`（本轮重复次数），并保留引擎所需的全部字段。
- 参数非法时 `raise ValueError("中文消息")`，控制台会把这条消息原样显示给用户。
- `runner_command()` 返回的命令行会被控制台追加 `spec.json` 路径后用 `subprocess` 启动，工作目录是你的模块目录；需要专用解释器（如 venv）就在这里指定。

- `Interfaces` 从仓库根 `interfaces/` 导入（`interfaces/scenarios.py` 是场景 ID 与标题的**唯一来源**，不要另建一份）。
- `run()` 用于"单轮直调"。批量/矩阵由 `console_runner.py` 负责（第 3.5 节），两者字段必须一致。

### 3.2 命名规则（根 README §2）

| 项 | 规定值 |
|---|---|
| `middleware_id` / `Adapter.name` / 归档目录名 | `dds`（**小写，禁止 `fastdds`、`FastDDS`、`DDS-3.6.2`**） |
| 页面显示名 `label` | `DDS` |
| 厂商与版本 | 放 `metadata().version` 与结果 `environment`（如 `vendor: eProsima Fast DDS`、`version: 3.6.2`、`python_binding: 2.6.1`） |

### 3.3 `metadata()` 字段（全部必填，必须真实探测）

| 字段 | 说明 |
|---|---|
| `id` | `"dds"` |
| `name` | `"dds"` |
| `label` | `"DDS"` |
| `version` | 探测到的 Fast DDS 版本；未就绪为 `null` |
| `available` | **必须真实探测**（见 6.2），不得硬编码 |
| `transport_options` | 你支持的映射值，如 `["udp", "shm"]`（与 PDF 的 `transport_mode` 映射见 5.5） |
| `qos_options` | `[{"value": "reliable", "label": "RELIABLE 可靠"}, {"value": "best_effort", "label": "BEST_EFFORT 尽力而为"}]` |
| `notes` | 能力、限制、以及环境未就绪时的**中文修复提示**（例：`"Fast DDS 绑定未构建：请先执行 DDS/setup_windows.bat"`） |

### 3.4 `catalog()` 返回字段（根 README §5，全部必填）

```text
scenario_name, case, condition_id, title
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
message_count, duration_seconds, repeats, random_seed
warmup_seconds, drain_seconds, timeout_seconds
network_delay_ms, network_jitter_ms, network_loss_rate, network_profile
transport_mode, qos_profile
```

- `scenario_name` 必须是 `S01`…`S12`（**不是** `point_to_point_low_latency` 这类名字，见 5.4）。
- 二选一语义：`message_count` 与 `duration_seconds`（PDF §2）。
- `network_loss_rate` 是 0–1 小数；`publish_rate_hz = 0` 表示不限速（PDF §2）。
- 全部数值必须是 JSON number，`transport_mode` 用小写。

### 3.5 `console_runner.py` 协议（根 README §8）

调用：`python DDS/console_runner.py <job_dir>/spec.json`

spec.json（控制台生成，你必须全部读取，`plan`/`logs` 也要用起来）：

```json
{
  "middleware": "dds",
  "config":  { "…你自己的引擎全局配置…" },
  "cases":   [ { "scenario_id": "S01", "scenario_name": "S01_1KiB_100Hz",
                 "scenario_title": "1 KiB @ 100 Hz point-to-point latency",
                 "case_repeats": 2,
                 "payload_size_bytes": 1024, "publish_rate_hz": 100,
                 "publisher_count": 1, "subscriber_count": 1,
                 "message_count": 1000, "duration_seconds": 10.0,
                 "warmup_seconds": 1, "drain_seconds": 0.5,
                 "transport_mode": "udp", "qos_profile": "reliable",
                 "network_profile": "baseline", "random_seed": 42 } ],
  "plan":    [ { "scenario_id": "S01", "scenario_name": "S01_1KiB_100Hz", "planned_repeats": 2 } ],
  "output":  "…/songfei/results/dds/<job_id>/output",
  "logs":    "…/songfei/results/dds/<job_id>/output/logs"
}
```

必须做到：

1. **重复次数**：优先 `case["case_repeats"]`，其次 `case["repeats"]`，默认 1；**每个条件都要跑满重复次数**（PDF §6.7：至少 5 次有效重复）。
2. **stdout 格式**：每行 `[HH:MM:SS] 消息`；每轮输出 `START <case> repeat=i/N` 与 `END <case> status=… sent=… received=…`。
3. **结束行**：`[HH:MM:SS] RESULT <output>/result.json status=completed|error|cancelled`。
4. **退出码**：全部完成 `0`；被取消 `130`；其他 `1`。
5. **产物**：`output/result.json`（套件级，含 `status/planned_runs/test_start_time/test_end_time/environment/configuration/scenario_summaries/runs/limitations`）；每轮 `output/runs/<run_id>.json`；原始证据 `output/artifacts/<run_id>/`。
6. **原子写**：写临时文件 + `os.replace`，控制台在运行中会持续读取。
7. **进程自清理**：被杀死时确保自己启动的所有端点进程一起退出（Windows 用 Job Object，参考 `VSOA/vsoa_py/standalone/processes.py`）。

### 3.6 单轮结果字段（根 README §6，缺测写 `null`，禁止写 0 或 "N/A"）

```text
run_id, middleware_id, middleware_version, scenario_name, scenario_title, repeat, status
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
messages_sent, messages_received, unique_deliveries, expected_deliveries
achieved_publish_rate_hz
latency_ms, latency_p95_ms, latency_p99_ms, latency_std_ms, throughput_mbps, jitter_ms
packet_loss, final_packet_loss
duplicate_count, out_of_order_count, corrupted_count
startup_time_ms, discovery_time_ms, recovery_time_ms
cpu_percent, memory_mb, latency_sample_count
configuration, statistics, link_metrics, environment, limitations
```

单位：丢失率 0–1 小数；吞吐 Mbit/s（仅应用 payload）；延迟/抖动/启动/发现/恢复 ms；CPU 百分比（1 逻辑核 = 100%）；内存 RSS MB（十进制）。时间戳 UTC ISO 8601。

状态枚举：`completed`、`error`、`cancelled`、`timeout`、`unsupported`、`not_tested`。

### 3.7 图表样本（前端延迟曲线依赖，务必提供）

控制台当前读取方式（与 VSOA 一致）：

```
output/artifacts/<run_id>/subscriber-0.result.json
  { "latencies_ms": [0.71, 0.62, 0.49, …] }        # 单位 ms，真实接收样本，按到达/序列顺序
```

同时建议在 run 结果里附 `samples_path` 指向该文件。**不得插值、不得随机生成。**

---

## 4. 场景要求（PDF §3 / §4 / §6）

| 场景 | PDF 要求 | DDS 现状 | 必须改成 |
|---|---|---|---|
| S01 | 1P/1S，1 KiB，100 Hz 与 1000 Hz | ✅ 2 条件 | 保持 |
| S02 | 固定拓扑/速率/QoS，遍历 1K/4K/16K/32K/48K/64K/256K/1M | ✅ 8 档完全一致 | 保持 |
| S03 | 64K/256K/1M，使用**最大稳定速率** | ✅ 3 条件（`publish_rate_hz:0` 不限速） | 结果里补 `rate_calibration`（候选扫描 + 选定速率 + 是否声称全局最大） |
| S04 | 100/1000/5000 Hz + 最大稳定速率 | ✅ 4 条件 | 同上 |
| S05 | 1P/4S，统计总交付与单订阅者 | ✅ 2 条件 | 保持 |
| S06 | 4P/1S，统计各发布者与总结果 | ✅ 2 条件 | 保持 |
| S07 | 4P/4S，交付矩阵 + 资源消耗 | ✅ 2 条件 | 保持 |
| S08 | ≥5 分钟，观察延迟漂移与内存增长 | ✅ 5 分钟 + `latency_drift_ms`/`memory_growth_mb` | 保持 |
| **S09** | 1% / 5% / 10% 丢包 + 延迟/抖动 | ❌ 3 条件全部 `enabled:false` 且**无 baseline → 场景完全缺席** | **至少新增 1 个可执行 baseline（0 损伤）**；3 档弱网保留并标 `not_tested`，或注入器就绪后启用 |
| **S10** | 冷启动、热启动、多端点发现、超时 | ⚠️ 仅 `S10_cold_start` | 补齐热启动、多端点发现、启动超时（共 4 项） |
| **S11** | 进程 / Broker / Router / 网络故障后的恢复 | ❌ 唯一条件 `enabled:false` → 场景缺席 | DDS 无 Broker：改为 **Participant 重启 / DataReader 断开重连 / 网络中断** 三类，至少一类可执行 |
| S12 | 校验长度、校验和、缺失、重复、乱序 | ✅ 1 条件 | 保持 |
| 通用 | 每条件 ≥5 次有效重复；每条消息含唯一序列号、发送时间戳、payload 长度、校验和 | 信封 ✅；重复由 CLI 参数控制 | 默认 5，正式 10，并回写进结果 |

---

## 5. DDS 现状差距 → 逐条改法（接口侧）

### 5.1 命名与状态

| 现状 | 位置 | 改成 |
|---|---|---|
| `suite.module_name: fastdds` | `config.yaml:5` | `module_name`/`middleware_id` = `dds`；`vendor: eProsima Fast DDS` 另存 |
| run 级 `status: "passed"` / `"failed"` | `fastdds_bench/results.py:41,50`、`cli.py:50`、`endpoint.py:346,378,563,597` | `completed` / `error`；能力缺口用 `unsupported` / `not_tested` |
| 分组字段用 `module_name` | `fastdds_bench/results.py:9` | 同时输出 `middleware_id`，与根 README §6 对齐 |

### 5.2 产物布局

| 现状 | 改成 |
|---|---|
| `outputs/results.json`（套件） | `output/result.json`（含 `status/planned_runs/environment/scenario_summaries/runs/limitations`） |
| `outputs/raw/<suite>/<run>/…` | `output/artifacts/<run_id>/`（保留原始 bin 与 SHA-256，同时在 `output/runs/<run_id>.json` 写单轮 JSON） |
| 无按轮 JSON | `output/runs/<run_id>.json`（字段见 3.6） |
| 无图表样本 | `output/artifacts/<run_id>/subscriber-0.result.json` 提供 `latencies_ms` |

### 5.3 缺失文件

新增 `DDS/adapter.py`、`DDS/console_runner.py`、`DDS/requirements.txt`、`DDS/SETUP.md`；`DDS/README.md` 增加 S01–S12 能力矩阵（含"不支持项 + 原因"）。

### 5.4 `scenario_name` 必须改用唯一来源

| 现在（DDS） | 规范（`interfaces/scenarios.py`） |
|---|---|
| `point_to_point_low_latency` | `point_to_point_latency` |
| `publish_rate_scan` | `send_rate_scan` |
| `many_to_one_aggregation` | `many_to_one_fanin` |
| `many_to_many_concurrency` | `many_to_many_mesh` |
| `long_running_stability` | `long_duration_stability` |
| `weak_network_and_recovery` | `weak_network_recovery` |
| `startup_and_discovery` | `startup_discovery` |
| `reconnect_and_fault_recovery` | `reconnect_fault_recovery` |

（`message_size_scan`、`large_message_throughput`、`one_to_many_broadcast`、`data_correctness` 已一致。）

### 5.5 transport 与 QoS 映射（PDF §2 用的是 TCP/UDP/QUIC 枚举）

你的实际 transport 是 `UDPv4`/`SHM`，必须显式映射并在结果中声明，禁止含糊：

| 你的实际配置 | 对外 `transport_mode` | 必须同时写入结果 |
|---|---|---|
| 显式 UDPv4、`useBuiltinTransports=false`、关闭 Data Sharing | `udp` | `transport_detail: "UDPv4, builtin transports disabled, data sharing off"` |
| 显式 SHM descriptor | `shm` | `transport_detail: "SHM descriptor, builtin transports disabled"` |

QoS 同理：`RELIABLE` ↔ `qos_profile: "reliable"`，`BEST_EFFORT` ↔ `"best_effort"`，并在 `limitations` 注明"DDS 可靠性是协议级重传，不等价于其他中间件的应用层恢复"。

### 5.6 速率口径必须统一为"每发布者"

现状（`UNIFIED_CONTRACT.md §2`）是**聚合**口径（4P@1000Hz = 合计 1000Hz），而 VSOA、MQTT 都是**每发布者**。PDF §5 要求"参数一致才可直接比较"，因此：

- 改为每发布者口径；或在结果中增加 `rate_scope: "aggregate"` 并**在报告里禁止与 `per_publisher` 的结果同组比较**。
- 推荐直接改口径，避免 4P/4S 场景横向对比失真。

---

## 6. DDS 专属重点：如何设计出"即插即用"

### 6.1 先承认：DDS 天生不即插即用（这不是代码 bug）

| # | 问题 | 性质 | 能否靠改代码消除 |
|---|---|---|---|
| 1 | 依赖 Fast DDS C++ + SWIG 绑定 + Fast DDS-Gen/IDL + MSVC + CMake + Java | **中间件本性** | ❌ 不可能"pip 装完就能跑" |
| 2 | "DDS" 是规范不是单一库（Fast DDS / Cyclone / Connext），QoS 默认值各家不同 | **中间件本性** | ⚠️ 只能锁定 vendor+version+build 并如实声明 |
| 3 | 无 Broker/Router 集中进程，组播做发现 | **中间件本性** | ⚠️ 语义层面可解（见 6.5） |
| 4 | 传输是 UDPv4/SHM，不是 TCP/UDP/QUIC | **中间件本性** | ✅ 映射 + 声明（5.5） |
| 5 | RELIABLE 是协议重传，不是应用层重放 | **中间件本性** | ✅ 用 `recovery_semantics` 字段声明 |

**结论：不要去消除这些差异，要把它变成"运行期可探测的状态"。** 这就是设计"即插即用"的全部思路。

### 6.2 三层解耦（必须遵守的分层）

```
DDS/adapter.py         ← 契约层：纯 Python，零原生依赖，任何解释器都能 import
DDS/console_runner.py  ← 编排层：纯 Python，负责拉起引擎、落盘、打印日志
DDS/fastdds_bench/     ← 引擎层：允许有 fastdds 依赖，必须【惰性导入】
```

硬规则：

1. `adapter.py` **顶层不得出现 `import fastdds`**（也不得 import 任何会触发它的模块）。
2. 引擎只能在 `run()` / `console_runner` 真正执行时导入（函数内 import 或 `importlib`）。
3. `available` 必须是**真实探测结果**：

```python
# DDS/adapter.py 骨架（契约层：零原生依赖）
import os, sys, json, shutil, importlib.util
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
MIDDLEWARE_ID = "dds"

def probe_environment():
    """返回 (ok: bool, detail: str)。任何异常都不得抛出。"""
    reasons = []
    if importlib.util.find_spec("fastdds") is None:
        reasons.append("未安装 fastdds Python 绑定")
    if not (FOLDER / "runtime").exists():
        reasons.append("缺少编译后的 runtime（Fast DDS 绑定 / IDL 类型支持）")
    if not os.environ.get("FASTDDSHOME"):
        reasons.append("未设置 FASTDDSHOME")
    if shutil.which("cmake") is None:
        reasons.append("缺少 CMake")
    return (not reasons), "；".join(reasons)

def backend():
    ok, detail = probe_environment()
    return {"id": "dds", "name": "dds", "label": "DDS", "available": ok,
            "version": _fastdds_version() if ok else None,
            "transport_options": ["udp", "shm"],
            "qos_options": [{"value": "reliable", "label": "RELIABLE 可靠"},
                            {"value": "best_effort", "label": "BEST_EFFORT 尽力而为"}],
            "notes": (["Fast DDS 3.6.2 / Python 绑定 2.6.1；transport 使用显式 UDPv4 或 SHM",
                       "S09 弱网与 S11 故障需四种中间件共用的外部注入器，注入器确认前标 not_tested"]
                      if ok else [f"环境未就绪：{detail}", "请先执行 DDS/setup_windows.bat，详见 DDS/SETUP.md"])}

def catalog():
    """永远返回 S01–S12 全部条件（与是否编译无关）。"""
    return [_row(c) for c in _load_conditions()]      # 读 config.yaml，字段名按 3.4 输出

def run(scenario, parameters):
    ok, detail = probe_environment()
    if not ok:
        # 优雅降级：不伪造数据，明确说明原因
        return {**_empty_run(scenario, parameters),
                "status": "not_tested",
                "limitations": [f"Fast DDS 环境未就绪：{detail}"]}
    return _map_engine_result(_invoke_engine(scenario, parameters))
```

### 6.3 独立解释器（解决 Python 版本与重型依赖）

- 你面向 **Python 3.11**，仓库主解释器是 3.12；因此用**私有 venv**：`DDS/.venv/Scripts/python.exe`。
- `console_runner.py` 与引擎都跑在这个 venv 里。
- `adapter.py` 必须能被主解释器 import（因为控制台要在主进程里调 `metadata()`/`catalog()`）→ 这正是 6.2 分层的原因。
- 控制台侧会用 `runner_command()` 返回的命令行启动你；建议实现为：

```python
def runner_command():
    venv = FOLDER / ".venv" / "Scripts" / "python.exe"
    return [str(venv) if venv.exists() else sys.executable, str(FOLDER / "console_runner.py")]
```

（控制台侧的通用加载属于"控制台维护者"工作，你只需按此签名实现，不要让控制台写死解释器。）

### 6.4 优雅降级是"即插即用"的一部分

三种环境状态下，行为必须都是**可预期、不崩、不伪造**：

| 环境状态 | `available` | `catalog()` | `run()` 结果 |
|---|---|---|---|
| 完全就绪 | `true` | S01–S12 全部条件 | 真实测量 |
| 部分缺失（如缺 IDL 类型） | `false` + `notes` 说明 | S01–S12 全部条件 | `status="not_tested"`，全指标 `null`，`limitations` 写明 |
| 完全不支持的能力（如 S09 无注入器） | 条件级 | 条件仍在目录里 | 条件级 `status="not_tested"` + `limitations` |

**绝不允许**：删掉场景、伪造指标、把 `null` 写成 0、抛异常让控制台启动失败。

### 6.5 发现/资源统计的语义落地（应对无 Broker、组播发现）

- `startup_time_ms`：PDF §4.7 定义为"控制程序开始 → 所有必要组件 ready"，你已符合（`harness_start → all_ready`）。
- `discovery_time_ms`：定义为"发现/匹配完成"。DDS 的"发现完成"= 每个 DataWriter/DataReader 的 matched count 达到对端总数（你已实现）；请把该定义写进结果 `discovery_ready_condition`，便于跨中间件解释差异。
- `cpu_percent` / `memory_mb`：PDF §4.5 的资源范围包含 Broker/Router/Discovery 服务，DDS 无独立进程 → 明确写 `resource_scope: "participant processes (publishers + subscribers)"`，与 VSOA/MQTT 的差异在报告限制中声明。
- 稳定性：Windows 多网卡/防火墙/组播常导致发现偶发失败 → 固定 `domain_id`、显式 UDPv4、必要时配置单播 peer 列表，并把 `domain_id` 写进每轮 `configuration`。

### 6.6 分阶段交付路线（关键：先让契约通，再让测量通）

| 阶段 | 内容 | 是否依赖原生环境 | 完成标志 |
|---|---|---|---|
| **第 1 步（先做）** | `adapter.py` + `console_runner.py` + 条件目录（S01–S12 全部条件，S09/S11 标 `not_tested`）+ 字段映射 + 目录布局 | ❌ 不依赖 | 控制台能看到 DDS、能启动、能落盘、能显示 `not_tested` —— **此时"接入"已完成** |
| 第 2 步 | `SETUP.md` + `requirements.txt` + 环境探针（结构化输出 `config_ok`/`runtime_probe`） | 部分 | 环境问题一眼可查 |
| 第 3 步 | 打开真实测量：S01–S08、S10 冷启动、S12 | ✅ | 指标卡与曲线出现真实数据 |
| 第 4 步 | 补齐 S10 其余 3 项；外部注入器到位后打开 S09/S11 | ✅ | S01–S12 全覆盖 |

---

## 7. 自测验收（**冒烟级**：只需证明"跑通"，不需要全量测试）

> 目的：证明"接口链路通"，不是产出正式数据。正式的全量 S01–S12 测试（每条件 ≥5 次重复）由控制台统一发起，**不在你的交付范围内**。
> 执行时只需跑下面第 1、2 步并检查第 3 步产物：**spec.json 里 `case_repeats` 写 1 即可**；第 4 步控制台联调同样是"任选一个条件能出数据、或明确给出 `not_tested`"即可，不必跑完整场景矩阵。

```powershell
# 1) 契约层不依赖原生环境即可导入（用主解释器）
python -c "import importlib.util as u; s=u.spec_from_file_location('a','DDS/adapter.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.create_adapter().metadata()); print(len(m.create_adapter().catalog()))"
#   期望：打印 dict（available 为真实值）与 条件数量 >0，且不抛异常

# 2) 独立跑一次作业（把 <job> 换成真实路径，spec.json 按 3.5 节最小示例构造）
python DDS/console_runner.py <job>\spec.json
#   期望：stdout 每行 [HH:MM:SS]…，含 START/END 与 RESULT … status=…；退出码 0

# 3) 产物字段核对
#   output/result.json 含 status/planned_runs/environment/scenario_summaries/runs/limitations
#   output/runs/<run_id>.json 含 3.6 节全部字段；缺测为 null
#   output/artifacts/<run_id>/subscriber-0.result.json 含 latencies_ms

# 4) 控制台联调
python songfei/run.py        # 下拉框出现 DDS；S01–S12 齐全；运行有数据或明确的 not_tested

# 5) 边界
git status --short           # 只有 DDS/ 下改动
```

---

## 8. 交付文档要求：更新 `DDS/README.md`（只要接口清单）

改造完成后**更新 `DDS/README.md`**。不需要写接口详细文档，**只要列出"有哪些接口 + 每个接口是干什么的"（名称 + 一句话功能）**即可。
允许把所有接口都写在 `README.md` 一个文件里（推荐）；也可拆到 `DDS/INTERFACES.md`，但 `README.md` 里要给出链接。

### 8.1 `README.md` 需要包含的内容（保持简短）

1. 一句话说明本模块做什么 + `available = true` 的成立条件
2. 冒烟验证命令（一条，能证明跑通）
3. **接口清单：名称 + 一句话功能**（主体，见 8.2）
4. S01–S12 能力矩阵（支持 / 部分支持 / `not_tested` + 原因）
5. 已知限制 + "不伪造数据"声明

### 8.2 接口清单的写法（每行：名称 + 功能，各一句话）

| 接口 | 功能 |
|---|---|
| `create_adapter()` | 创建并返回本中间件的 Adapter 实例，供控制台注册 |
| `Adapter.name` | 中间件 ID，固定为 `dds` |
| `Adapter.metadata()` | 返回中间件元数据与可用性（含真实环境探测结果） |
| `Adapter.catalog()` | 返回 S01–S12 全部标准条件，供控制台条件下拉框使用 |
| `build_cases(index, configuration, matrix)` | 把"当前表单参数"或"完整矩阵"展开成引擎可执行条件列表（控制台必需） |
| `base_config()` | 返回引擎全局配置（可选，写入 spec.json 的 config） |
| `Adapter.run(scenario, parameters)` | 执行单轮真实测量，返回统一格式的单轮结果 |
| `probe_environment()` | 探测 Fast DDS 环境是否就绪（返回 ok 与原因） |
| `runner_command()` | 返回启动执行器的命令行（含解释器选择） |
| `console_runner.py <spec.json>` | 子进程执行器入口：按 spec 逐条件逐轮执行并落盘 |
| `output/result.json` | 套件级结果汇总文件，控制台轮询读取 |
| `output/runs/<run_id>.json` | 单轮结果文件（统一字段） |
| `output/artifacts/<run_id>/subscriber-0.result.json` | 延迟样本文件，供前端延迟曲线使用 |
| `launch.py` | 原有原生 CLI，供人工复现用 |

> 字段名、单位、状态枚举等以仓库根 `README.md` 与需求文档 PDF 为准，**不必在本文重复抄写**。

---

## 9. 禁止事项

1. 不得修改 `songfei/`、`interfaces/`（`interfaces/scenarios.py` 是场景唯一来源）。
2. 不得删场景、不得伪造/估算指标、不得把未测量写成 `0`。
3. 不得把 `module_name` 写成 `fastdds`、不得用厂商名做接口 ID。
4. 不得让 `adapter.py` 在顶层依赖原生库（会直接破坏"即插即用"）。
5. 不得把结果写到别的中间件目录或仓库根。
6. 提交前删除 `__pycache__/`、`outputs/`、编译中间产物。
