# DDS 侧统一测试任务（DDS_test.md）

用与 VSOA / MQTT / Zenoh **完全相同**的 S01–S08 × 4 组参数（**32 组，每组 1 轮**）跑一遍 DDS，
把结果目录原样回传，用来补全"四种中间件同一参数矩阵"的横向对比。
参数即 `songfei/test/run_matrix.py` 的 `PLAN`，没有任何自定义成分。

---

## 1. 要跑什么

把 `matrix_s01_s08.yaml` 放到项目**根目录**（与 `launch.py`、`config.yaml` 同级），文件名不变。
里面是 32 个条件（S01–S08 各 4 组，明细见 §3），统一固定：`transport_mode=udp`（显式 UDPv4）、
`qos_profile=reliable`、`network_profile=normal`、`warmup_seconds=1.0`、`drain_seconds=2.0`、
`repeats=1`、`timeout_seconds=120`。

---

## 2. 怎么跑

```bat
cd /d <你的项目根>
set FASTDDSHOME=D:\fastdds

rem 校验（几秒，不发数据）
.venv\Scripts\python.exe launch.py check --config matrix_s01_s08.yaml

rem 全量 32 组（约 30–45 分钟），终端输出同时存文件
.venv\Scripts\python.exe launch.py run --config matrix_s01_s08.yaml --output results\matrix_s01_s08 --overwrite > results\matrix_s01_s08.console.txt 2>&1
```

想先跑 1 组试试（约 15 秒）：

```bat
.venv\Scripts\python.exe launch.py run --config matrix_s01_s08.yaml --output results\smoke --condition S01_G1_100Hz --overwrite
```

边跑边看进度（另开一个窗口）：

```powershell
Get-Content results\matrix_s01_s08.console.txt -Wait
```

运行期间：机器插电、不要睡、别同时跑其它重负载，中途不要手动杀进程。

---

## 3. 参数明细（32 组，逐组核对用）

`P×S` = 发布者数 × 订阅者数；`message_count: 0` 表示以时长为停止条件。
**本次没有"不限速"档**：VSOA 引擎要求速率 ≥1 Hz，四家统一改用官方档位（S03 = 20 Hz、S04 最高档 = 10000 Hz、S08 大消息 = 5 Hz）。
条件 ID 与组一一对应：`S01_G1_100Hz` … `S08_G4_1MiB_5Hz`。

### 3.1 每个场景测什么（自变量 → 要交回的指标）

每组**只变一个自变量**（4 档），其余参数固定；交回的核心指标按场景不同（同一套字段名，见 §3.2）。
全局固定量（32 组共有）：`warmup_seconds=1.0`、`drain_seconds=2.0`、`repeats=1`、`timeout_seconds=120`、
`network_profile=normal`（不注入损伤）、1P×1S（S05/S06/S07 除外）、同机同时钟。

| 场景 | **自变量**（4 档） | 固定量 | **该交回的重点指标** | 我方已测到的量级（三家，供核对） |
|---|---|---|---|---|
| **S01** 点对点低延迟 | 发送速率 100 / 500 / 1000 / 2000 Hz | 1 KiB、1P1S、10 s | `latency_ms`、`latency_p95_ms`、`latency_p99_ms`、`latency_std_ms`、`jitter_ms`、`final_packet_loss` | 延迟 0.36–1.0 ms 量级 |
| **S02** 消息大小扫描 | payload 1 / 4 / 16 / 64 KiB | 100 Hz、1P1S、10 s | 同上 + `throughput_mbps`（随尺寸上升）、`memory_mb` | 吞吐 0.82→52 Mbps |
| **S03** 大消息吞吐 | payload 64 / 128 / 256 / 512 KiB | **20 Hz 固定**、1P1S、10 s | `throughput_mbps`、`latency_p95_ms`、`cpu_percent`、`memory_mb` | 吞吐 11 / 22 / 44 / 88 Mbps |
| **S04** 发送速率扫描 | 速率 100 / 1000 / 5000 / **10000** Hz | 1 KiB、1P1S、8 s | `throughput_mbps`、`latency_p99_ms`、`final_packet_loss`、`achieved_publish_rate_hz` | 100 Hz 档 ≈0.83 Mbps |
| **S05** 一对多广播 | 订阅者数 1 / 2 / 3 / 4 | 1 KiB、1P、1000 Hz、10 s | **`link_metrics` 里每个订阅者的交付**、`latency_p95_ms`、`final_packet_loss`、`memory_mb` | 8.19 Mbps、0.16–0.23 ms |
| **S06** 多对一汇聚 | 发布者数 1 / 2 / 3 / 4 | 1 KiB、1S、1000 Hz/发布者、10 s | **`link_metrics` 里每个发布者的交付**、`latency_p95_ms`、`final_packet_loss`、`cpu_percent` | 8.19 Mbps、0.16–0.23 ms |
| **S07** 多对多并发 | 并发规模 1P1S / 2P2S / 3P3S / 4P4S | 1 KiB、1000 Hz/发布者、8 s | **16 条链路的交付矩阵**、`throughput_mbps`、`latency_p95_ms`、`cpu_percent`、`memory_mb` | 8.19 Mbps、0.17–0.21 ms |
| **S08** 长时间稳定性 | 持续负载：1 KiB@100 Hz / 1 KiB@1000 Hz / 64 KiB@100 Hz / 1 MiB@**5 Hz** | **每组 100 s**、1P1S | **延迟随时间漂移**、`jitter_ms`、`memory_mb`（增长趋势）、`cpu_percent` | 组1：0.55–0.91 ms、0.82 Mbps |

> S08 请额外交回逐采样证据：`artifacts\<run_id>\subscriber-0.result.json` 的 `latencies_ms` 数组与资源逐采样序列 —— 我方用它画"延迟/内存随时间"的对照图。

### 3.2 交回时每个字段的含义（四家统一口径，勿改字段名）

| 字段 | 含义 | 单位 |
|---|---|---|
| `latency_ms` / `latency_p95_ms` / `latency_p99_ms` / `latency_std_ms` | 订阅端收到时刻 − 发布端发送时刻；对该轮全部有效样本统计 | ms |
| `latency_sample_count` | 计入统计的有效延迟样本数 | 条 |
| `jitter_ms` | 同一链路相邻两个成功交付样本的 \|Δlatency\| 均值 | ms |
| `throughput_mbps` / `offered_throughput_mbps` | 实收 / 应发吞吐 | Mbit/s |
| `packet_loss` / `final_packet_loss` | 测量窗口内 / 轮次结束时未交付比例 | 0–1 |
| `achieved_publish_rate_hz` | 实际达成速率（与 `publish_rate_hz` 同为 per_publisher 口径） | Hz |
| `startup_time_ms` / `discovery_time_ms` | 端点就绪 / 所有对端匹配数达到对端总数 | ms |
| `recovery_time_ms` | 仅注入故障并恢复时填值，其余为 `null` | ms 或 null |
| `cpu_percent` / `memory_mb` | 该中间件**全部自有服务进程**（DDS 无 broker/router，等于端点） | % / MB |
| `cpu_percent_endpoints` / `memory_mb_endpoints` | **仅端点进程**（DDS 与上面两项相同，我方对比时按等价处理，你侧无需改动） | % / MB |
| `messages_sent` / `messages_received` / `unique_deliveries` / `expected_deliveries` | 发送 / 接收 / 唯一有效交付 / 期望交付 | 条 |
| `duplicate_count` / `out_of_order_count` / `corrupted_count` | 重复 / 乱序 / 校验失败 | 条 |
| `link_metrics` | 多端点逐链路指标，publisher/subscriber 从 0 开始 | 数组 |

无法测量一律 `null`（不写 `0`、不写 `N/A`）。

| 场景 | 组 | 影响因子 | payload | 速率 | P×S | 时长 / 条数 |
|---|---|---|---|---|---|---|
| S01 点对点低延迟 | 组1 | 发送速率 | 1 KiB | 100 Hz | 1×1 | 10 s / 1000 条 |
| S01 | 组2 | 发送速率 | 1 KiB | 500 Hz | 1×1 | 10 s / 5000 条 |
| S01 | 组3 | 发送速率 | 1 KiB | 1000 Hz | 1×1 | 10 s / 10000 条 |
| S01 | 组4 | 发送速率 | 1 KiB | 2000 Hz | 1×1 | 10 s / 20000 条 |
| S02 消息大小扫描 | 组1 | payload 大小 | 1 KiB | 100 Hz | 1×1 | 10 s / 1000 条 |
| S02 | 组2 | payload 大小 | 4 KiB | 100 Hz | 1×1 | 10 s / 1000 条 |
| S02 | 组3 | payload 大小 | 16 KiB | 100 Hz | 1×1 | 10 s / 1000 条 |
| S02 | 组4 | payload 大小 | 64 KiB | 100 Hz | 1×1 | 10 s / 1000 条 |
| S03 大消息吞吐 | 组1 | 尺寸（20 Hz 固定） | 64 KiB | 20 Hz | 1×1 | 10 s |
| S03 | 组2 | 尺寸（20 Hz 固定） | 128 KiB | 20 Hz | 1×1 | 10 s |
| S03 | 组3 | 尺寸（20 Hz 固定） | 256 KiB | 20 Hz | 1×1 | 10 s |
| S03 | 组4 | 尺寸（20 Hz 固定） | 512 KiB | 20 Hz | 1×1 | 10 s |
| S04 发送速率扫描 | 组1 | 发送速率 | 1 KiB | 100 Hz | 1×1 | 8 s |
| S04 | 组2 | 发送速率 | 1 KiB | 1000 Hz | 1×1 | 8 s |
| S04 | 组3 | 发送速率 | 1 KiB | 5000 Hz | 1×1 | 8 s |
| S04 | 组4 | 发送速率 | 1 KiB | 10000 Hz | 1×1 | 8 s |
| S05 一对多广播 | 组1 | 订阅者数量 | 1 KiB | 1000 Hz | 1×1 | 10 s |
| S05 | 组2 | 订阅者数量 | 1 KiB | 1000 Hz | 1×2 | 10 s |
| S05 | 组3 | 订阅者数量 | 1 KiB | 1000 Hz | 1×3 | 10 s |
| S05 | 组4 | 订阅者数量 | 1 KiB | 1000 Hz | 1×4 | 10 s |
| S06 多对一汇聚 | 组1 | 发布者数量 | 1 KiB | 1000 Hz/发布者 | 1×1 | 10 s |
| S06 | 组2 | 发布者数量 | 1 KiB | 1000 Hz/发布者 | 2×1 | 10 s |
| S06 | 组3 | 发布者数量 | 1 KiB | 1000 Hz/发布者 | 3×1 | 10 s |
| S06 | 组4 | 发布者数量 | 1 KiB | 1000 Hz/发布者 | 4×1 | 10 s |
| S07 多对多并发 | 组1 | 并发规模 | 1 KiB | 1000 Hz/发布者 | 1×1 | 8 s |
| S07 | 组2 | 并发规模 | 1 KiB | 1000 Hz/发布者 | 2×2 | 8 s |
| S07 | 组3 | 并发规模 | 1 KiB | 1000 Hz/发布者 | 3×3 | 8 s |
| S07 | 组4 | 并发规模 | 1 KiB | 1000 Hz/发布者 | 4×4 | 8 s |
| S08 长时间稳定性 | 组1 | 持续负载 | 1 KiB | 100 Hz | 1×1 | 100 s |
| S08 | 组2 | 持续负载 | 1 KiB | 1000 Hz | 1×1 | 100 s |
| S08 | 组3 | 持续负载 | 64 KiB | 100 Hz | 1×1 | 100 s |
| S08 | 组4 | 持续负载 | 1 MiB | 5 Hz | 1×1 | 100 s |

---

## 4. 预期结果

- `check`：`total_condition_count=32`、`enabled_condition_count=32`、`planned_run_count=32`、`not_tested_condition_count=0`。
  其中 32 条 `repeats=1 is below the formal minimum of 5` 的 warning 是**预期**：本次为了与其余三种对齐，
  每组只采 1 轮。**不要**改成 5 轮，否则对比口径不一致。
- 全量结尾：`Runs: 32, completed: 32, not completed: 0`。
- 耗时：S01–S07 共 28 组约 8–10 分钟，S08 四组各 100 s 约 8 分钟，合计约 **20 分钟**。
- ⚠️ **S08 本次每组只跑 100 s**（规范要求 ≥5 分钟）：这是四家共用的口径，报告里必须注明。
- 若某组 `status` 不是 `completed`：不要改参数、不要删目录，把该组的
  `runs\<run_id>.json` 与 `artifacts\<run_id>\logs\*.log` 一起回传即可，套件会继续往下跑。

---

## 5. 回传什么

1. 整个 `results\matrix_s01_s08\` 打包：`result.json`、`runs\*.json`（32 个）、
   `artifacts\<run_id>\subscriber-0.result.json`（延迟曲线样本）。
2. `results\matrix_s01_s08.console.txt`（终端全文）。
3. `launch.py check` 的输出。
4. 机器配置：CPU 型号/核心数、内存、Windows 版本；以及 `echo %FASTDDSHOME%`、`type runtime\build_info.json`。

请发 JSON 原文而不是截图/摘要 —— 对比表按字段取值，截图无法回溯。

---

## 6. 可比性备注（写报告时带上）

- **传输不同**：DDS 为显式 UDPv4；其余三种按其目录模板默认（MQTT/Zenoh 为 TCP）→ 跨传输模式的数字不得直接平均或排名。
- **每格 1 次采样**（`repeats=1`），与其余三种一致；需要置信区间再单独跑一份，不要混进本次结果。
- **S08 时长**：本次四组各 100 s（短于规范的 ≥5 分钟），四家口径一致，但报告必须注明。
- **资源口径**：DDS 没有 Broker/Router，`cpu_percent` / `memory_mb` 只统计端点进程。
- **可靠性语义**：DDS 的 `reliable` 是协议级重传，不等价于其它中间件的应用层恢复。
