# 四种通信中间件统一测试与可视化接入规范

本文是本仓库四个中间件（VSOA、DDS、MQTT、Zenoh）接入统一可视化控制台的交付契约。后续负责 DDS、MQTT、Zenoh 的 AI 必须先阅读本文，再在各自的中间件目录内完成实现。目标是让 `songfei/example.html` 这一套界面使用同一组参数、同一组接口名称和同一组指标，切换中间件后仍能测试并对比 S01–S12 全部场景。

## 1. 当前基线：VSOA 已接入

VSOA 是唯一的参考实现，不能另造一套字段名或单位。当前控制台位于 `songfei/`，页面原型是 `songfei/example.html`，运行页面是 `songfei/static/index.html`。

- 启动：`python songfei/run.py`
- 地址：`http://127.0.0.1:8787/`
- VSOA 引擎：`VSOA/vsoa_py/standalone`
- 场景目录唯一来源：`interfaces/scenarios.py`
- VSOA 结果目录：`songfei/results/vsoa/<job_id>/`
- VSOA 已覆盖：S01–S12，共 12 个场景和配置文件中展开的全部标准条件；指标必须来自真实测量，不得生成模拟数据。

VSOA 的控制台逻辑在 `songfei/app.py` 中已经把引擎结果映射为 UI 字段。新中间件必须输出下面同名字段，控制台才能复用同一套页面和图表。

## 2. 稳定命名：ID、显示名和目录

`middleware_id` 是机器接口中的稳定值，永远使用小写 ASCII，不随厂商库名变化。`label` 只用于页面显示。适配器的 `name` 必须等于 `middleware_id`。

| 中间件 | `middleware_id` / `Adapter.name` | 页面 `label` | 代码目录 |
|---|---|---|---|
| VSOA | `vsoa` | `VSOA` | `VSOA/` |
| DDS | `dds` | `DDS` | `DDS/` |
| MQTT | `mqtt` | `MQTT` | `mqtt/` |
| Zenoh | `zenoh` | `Zenoh` | `Zenoh/` |

不要使用 `Vsoa`、`VSOA-PY`、厂商产品名、协议版本号等作为接口 ID。版本号放在 `metadata().version` 和结果的 `environment` 中。

## 3. 仓库目录和所有权

目录必须按下列职责组织。三位后续实现者只修改自己负责的中间件目录；不要直接改 `songfei/`、`interfaces/` 或其他中间件目录来绕过适配器契约。

```text
README.md                         # 本规范，所有实现者的入口
interfaces/
  __init__.py                     # ScenarioSpec、ScenarioResult、MiddlewareAdapter
  scenarios.py                    # S01–S12 唯一场景目录，不复制、不改名
VSOA/                             # 已完成的参考实现，不要求重写
  vsoa_py/standalone/             # 真实测量引擎
DDS/                              # DDS AI 的唯一工作目录
  adapter.py                      # 统一适配器（必须）
  console_runner.py               # 子进程入口（必须）
  README.md                       # DDS 接入说明、依赖和能力矩阵（必须）
  requirements.txt                # 可选：DDS 专用依赖
  results/<job_id>/                # 运行产物，禁止写到其他中间件目录
mqtt/                             # MQTT AI 的唯一工作目录，文件结构同 DDS
  adapter.py
  console_runner.py
  README.md
  requirements.txt                # 可选
  results/<job_id>/
Zenoh/                            # Zenoh AI 的唯一工作目录，文件结构同 DDS
  adapter.py
  console_runner.py
  README.md
  requirements.txt                # 可选
  results/<job_id>/
songfei/
  example.html                    # 目标可视化原型
  static/index.html               # 服务实际加载的页面
  app.py                          # 统一 HTTP 控制台
  vsoa_runner.py                  # VSOA 参考子进程入口
  results/<middleware_id>/<job_id>/# 控制台归档目录
```

每个中间件的 `results/<job_id>/` 至少包含 `job.json`、`spec.json`、`console.log`、`output/result.json`、`output/runs/<run_id>.json`；原始样本、资源采样和进程日志放在 `output/artifacts/<run_id>/`、`output/logs/<run_id>/`。

## 4. 适配器必须暴露的 Python 接口

每个目录的 `adapter.py` 必须实现 `interfaces.MiddlewareAdapter`，并导出 `create_adapter()`。不要求 UI 了解具体厂商库 API。

```python
from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec

class Adapter(MiddlewareAdapter):
    name = "dds"  # mqtt / zenoh；必须与目录对应的 middleware_id 一致

    def metadata(self) -> dict:
        return {"id": self.name, "name": self.name, "label": "DDS",
                "version": "实际库版本", "available": True,
                "transport_options": ["tcp", "udp"],
                "qos_options": [{"value": "default", "label": "原生默认"}],
                "notes": ["真实测量说明", "不支持的能力及原因"]}

    def catalog(self) -> list[dict]:
        """返回 S01–S12 展开的标准条件，字段名必须见第 5 节。"""

    def run(self, scenario: ScenarioSpec, parameters: dict) -> ScenarioResult:
        """执行一轮真实测试；不得返回随机数或示例数据。"""

def create_adapter() -> Adapter:
    return Adapter()
```

`catalog()` 可以读取 `interfaces.scenarios.SCENARIOS`，但不得另建一份场景 ID。每个场景都必须出现；能力不足时仍返回该场景，并在结果中使用 `unsupported` 或 `not_tested`，同时写明原因，不能静默删除或伪造指标。

## 5. 与 `example.html` 对齐的输入字段

`/api/start` 的 `configuration` 和 `/api/init` 的 `catalogs[middleware_id]` 使用以下字段名：

```text
scenario_name, case, condition_id, title
payload_size_bytes, publish_rate_hz, publisher_count, subscriber_count
message_count, duration_seconds, repeats, random_seed
warmup_seconds, drain_seconds, timeout_seconds
network_delay_ms, network_jitter_ms, network_loss_rate, network_profile
transport_mode, qos_profile
```

其中 `payload_size_bytes` 是字节，速率是 Hz，时长是秒，网络延迟/抖动是 ms，`network_loss_rate` 是 0–1 比例（不是百分数）。所有配置数值必须是 JSON number；`transport_mode` 使用小写值。`message_count` 与 `duration_seconds` 按引擎规则二选一或同时提供。

## 6. 统一单轮结果字段

每轮写入 `output/runs/<run_id>.json`，并在 `output/result.json` 的 `runs` 数组中保留同一对象。以下字段名是 UI、报告和对比逻辑的唯一名称；无法测量时填 `null`，不要填 `0` 或 `"N/A"`。

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

状态统一为 `completed`、`error`、`cancelled`、`timeout`、`unsupported`、`not_tested`。丢失率是 0–1 比例（5% 返回 `0.05`），吞吐是 Mbit/s，延迟/抖动/启动/发现/恢复是 ms，CPU 是百分比，内存是 RSS MB。多拓扑的 `link_metrics` 中 publisher/subscriber 从 0 开始。

图表样本必须来自真实接收样本，并转换为 `[{'sequence': 0, 'latency': 1.23}]` 结构（latency 单位 ms）；不得插值或随机生成。

## 7. 统一 HTTP API（页面已经使用的名称）

新中间件适配后，页面只改变 `middleware` 查询参数，不改变路径、请求字段或返回字段。

- `GET /api/init` → `middleware` 后端元数据数组、`catalogs` 条件目录、`names` 场景标题、`history` 历史作业。
- `GET /api/state` → `{"job": {"id", "middleware_id", "status", "total"}, "logs": [{"time", "text"}]}`。
- `GET /api/history?middleware=dds` → 仅该中间件的 `{id,status}` 作业数组。
- `GET /api/results?middleware=dds&job=<job_id>[&run=<run_id>]` → `{runs,result,samples,link,report,logs}`，其中 `result` 使用第 6 节字段。
- `POST /api/start` 请求体：`{"middleware":"dds","template_index":0,"configuration":{...},"matrix":false}`；`matrix=true` 执行所选场景全部标准条件和重复轮次。
- `POST /api/stop`、`POST /api/shutdown` 沿用 VSOA 语义；请求头使用现有页面要求的 `X-MQTT-Token`。停止必须清理全部子进程。

## 8. 子进程运行协议

每个中间件的 `console_runner.py` 必须支持 `python <middleware>/console_runner.py <job_dir>/spec.json`。从 spec 读取 `config`、`cases`、`plan`、`output`、`logs`；标准输出建议使用 `[HH:MM:SS] message`；成功结束输出 `RESULT <output/result.json> status=completed`，失败/停止分别使用 `status=error`/`status=cancelled`；成功退出码为 0。所有产物只能写入所属中间件的作业目录。

## 9. S01–S12 验收要求

场景 ID 和顺序以 `interfaces/scenarios.py` 为准：

| ID | 场景 | 至少验证的重点指标 |
|---|---|---|
| S01 | `point_to_point_latency` | latency、P95、P99、标准差 |
| S02 | `message_size_scan` | latency、throughput、packet_loss |
| S03 | `large_message_throughput` | throughput、P95、CPU、内存 |
| S04 | `send_rate_scan` | throughput、P99、packet_loss |
| S05 | `one_to_many_broadcast` | 各订阅者交付、P95、丢失、内存 |
| S06 | `many_to_one_fanin` | 各发布者交付、P95、丢失、CPU |
| S07 | `many_to_many_mesh` | 16 条链路、吞吐、P95、丢失、资源 |
| S08 | `long_duration_stability` | 延迟漂移、jitter、内存增长、CPU |
| S09 | `weak_network_recovery` | 原始/最终丢失、P95、jitter |
| S10 | `startup_discovery` | startup、discovery、延迟 |
| S11 | `reconnect_fault_recovery` | recovery、startup/discovery、最终丢失 |
| S12 | `data_correctness` | 缺失、重复、乱序、损坏、最终丢失 |

每个中间件的 `README.md` 必须附能力矩阵，列出 S01–S12、支持的传输/QoS、已知限制、真实测量方式和运行命令。任何不支持项必须明确标注，不能用固定常数冒充测量。

## 10. 三位后续实现者的明确任务

### DDS（只改 `DDS/`）

- 创建 `DDS/adapter.py`、`DDS/console_runner.py`、`DDS/README.md`。
- `Adapter.name` 固定为 `dds`，实现 `metadata()`、`catalog()`、`run()`。
- 将 DDS 原生结果映射为第 6 节字段，并保存 `DDS/results/<job_id>/`。
- 完成 S01–S12 能力矩阵和至少一轮真实冒烟测试。

### MQTT（只改 `mqtt/`）

- 创建 `mqtt/adapter.py`、`mqtt/console_runner.py`、`mqtt/README.md`。
- `Adapter.name` 固定为 `mqtt`；broker、topic、QoS 等差异只能体现在 `configuration`、`qos_profile` 和 `notes`，不能改变公共字段名。
- 将 MQTT 原生结果映射为第 6 节字段，并保存 `mqtt/results/<job_id>/`。
- 完成 S01–S12 能力矩阵和至少一轮真实冒烟测试。

### Zenoh（只改 `Zenoh/`）

- 创建 `Zenoh/adapter.py`、`Zenoh/console_runner.py`、`Zenoh/README.md`。
- `Adapter.name` 固定为 `zenoh`；session、router、transport 等差异放入适配器内部或 `configuration`，不能泄漏到 UI 专用字段。
- 将 Zenoh 原生结果映射为第 6 节字段，并保存 `Zenoh/results/<job_id>/`。
- 完成 S01–S12 能力矩阵和至少一轮真实冒烟测试。

完成上述目录内工作后，再由控制台维护者在 `songfei/app.py` 中注册适配器并接入统一页面。中间件 AI 不应为了“让下拉框出现”而修改控制台；先把目录内契约实现完整。

## 11. 提交前检查清单

- [ ] `middleware_id`、`Adapter.name`、目录归档名三者一致（`vsoa`/`dds`/`mqtt`/`zenoh`）。
- [ ] `catalog()` 包含 S01–S12，字段名与第 5 节完全一致。
- [ ] `console_runner.py <spec.json>` 可独立运行并生成 `output/result.json`。
- [ ] 每轮结果包含第 6 节公共字段；未测量值为 `null`，限制写入 `limitations`。
- [ ] 比例、单位和 CPU/RSS 统计口径符合第 6 节。
- [ ] 日志包含 `[HH:MM:SS]` 前缀，结束时输出 `RESULT ... status=...`。
- [ ] stop 可以清理全部子进程；重复运行不会覆盖旧 job。
- [ ] 产物只写入所属目录，没有修改 `songfei/`、`interfaces/` 或其他中间件目录。
- [ ] `README.md` 写明依赖、启动命令、支持矩阵、限制和一次真实测试结果。

核心原则：**中间件可以不同，适配器内部可以不同，但交给 `songfei/example.html` 的参数、接口、字段、单位和状态必须相同。**
