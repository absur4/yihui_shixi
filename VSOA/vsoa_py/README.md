# VSOA Python 功能与性能测试

## Windows 独立交付（新增）

已在原有测试基础上新增独立 Windows x64 模块，当前交付包为 `release/vsoa_win64_v1.3.zip`。解压后双击 `run.bat`，无需安装 Python 或额外服务。

- 默认五类场景、每场景三轮；统一 `config.yaml`，结果写入 `outputs/result.json`。
- `dashboard.html` 支持最多四模块的统一结果和功能画像导入。本次只实现 VSOA，未虚构其他中间件。
- 打包版说明与验收：`release/vsoa_win64_v1.3/README.md`、`release/vsoa_win64_v1.3/ACCEPTANCE.md`。
- 源码入口为 `vsoa_module.py`，场景引擎在 `standalone/`；构建与复验方法见 `BUILD_AND_ACCEPTANCE.md`。
- 原有 15 个独立脚本仍保留，以下是它们的使用说明。原脚本 schema 1.0 的闭环 RPC 数据与打包版 schema 2.0 的发布订阅数据**不能直接混用比较**。

本项目只测试 VSOA，不包含与其他中间件的对比结论。共 **15 个独立指标文件**，共用进程管理、统计和结果输出工具。功能结果区分“实测”“文档声明”“尚未验证”；不把缺少证据当作“不支持”。

## 1. 安装与快速运行

要求 Python >= 3.10。已验证环境为 Windows 11 / Python 3.13.9；Linux、macOS 需要在各自机器上执行验证。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_all.py
```

当前目录已经创建 `.venv` 并安装 `vsoa==1.0.4`、`psutil==7.2.2`。锁定版本是为了复现，不表示它们是最新版本。

Linux / macOS 对应使用 `.venv/bin/python`，其余参数不变。

独立执行一个指标：

```powershell
.\.venv\Scripts\python.exe test_latency.py --samples 1000 --warmup 100 --payload-bytes 1024 --output results\latency.json
.\.venv\Scripts\python.exe test_throughput.py --duration 10 --window 64 --payload-bytes 1024 --output results\throughput.json
.\.venv\Scripts\python.exe test_packet_loss_recovery.py --samples 200 --loss-rate 0.1 --seed 42 --retries 3 --output results\loss.json
```

快速检查、重复测量及结果校验：

```powershell
.\.venv\Scripts\python.exe run_all.py --samples 20 --warmup 3 --duration 0.4 --timeout 0.5 --output-dir results\quick-run
.\.venv\Scripts\python.exe run_all.py --repeat 3 --output-dir results\repeat-run
.\.venv\Scripts\python.exe validate_results.py results\repeat-run
```

批量运行是**串行**的，避免多个指标同时竞争 CPU 和网络。默认输出目录为 `results/年月日_时分秒/`。指定输出目录时，不允许覆盖已有同名指标结果，请更换目录。独立脚本显式指定 `--output` 时允许替换该文件。

## 2. 指标文件与输出字段

### 功能 / 特性层

| 指标 | 独立文件 | 主要验证和输出 |
| --- | --- | --- |
| 发现机制 | `test_discovery.py` | 启动真实 Position 进程；`mechanism`、`resolved_endpoint`、`unknown_service_returns_none`、`warm_lookup` |
| 通信模型 | `test_communication_models.py` | RPC、URL 前缀发布订阅、TCP / quick UDP datagram、TCP 双向 stream 的真实收发与内容检查 |
| 跨平台 | `test_cross_platform.py` | 当前 OS 的 TCP / UDP 冒烟测试；`platform_matrix`；其他系统及异构互通标记 `not_tested` |
| QoS | `test_qos.py` | 设置并读取优先级 0 / 3 / 7；拒绝 -1 / 8；线缆 DSCP 和拥塞下优先级效果未验证 |
| 可靠性 | `test_reliability.py` | TCP 请求序号和内容完整性、无响应超时、超时后连接可用性、显式新建客户端后的恢复 |
| 实时性 | `test_realtime.py` | `deadline_misses`、`deadline_miss_rate`、计划释放至完成的时延、发送调度迟到；不宣称硬实时 |
| 传输方式 | `test_transports.py` | TCP datagram、UDP quick datagram / publish、TCP stream；TLS 文档证据与 IPv6 未验证状态 |
| 生态成熟度 | `test_ecosystem.py` | 发行包版本、许可证、Python 要求、项目地址、文档量、公开 API；缺失的活跃度和采用率明确留空，不生成主观分数 |

### 性能层

| 指标 | 独立文件 | 核心字段 / 单位 |
| --- | --- | --- |
| 端到端延迟 | `test_latency.py` | `client_to_server_handler` 单向交付时间、`rtt` 往返时间；ms |
| 吞吐量 | `test_throughput.py` | `rpc_rate`，RPC/s；单方向有效载荷 byte/s、MiB/s；双方向有效载荷 byte/s |
| 抖动 | `test_jitter.py` | RTT 总体标准差、连续成功样本的绝对 RTT 差、接收间隔与发送间隔之差；ms |
| CPU 占用 | `test_cpu.py` | 分别输出客户端 / 服务端 CPU 时间 s、单核口径百分比、全机容量口径百分比 |
| 内存占用 | `test_memory.py` | 分别输出客户端 / 服务端 RSS 基线、末值、增量、采样峰值和分位数；byte |
| 启动 / 发现时间 | `test_startup_discovery.py` | 进程启动至监听就绪、首次 RPC 成功、新连接握手、首次 RPC、Position 首次解析和热查询；ms |
| 丢包恢复能力 | `test_packet_loss_recovery.py` | 实际代理丢弃数、初始丢包率、原生等待期、应用补发轮次 / 恢复率 / 残余丢包 / 耗时 |

## 3. 统一结果格式

每个脚本把一个 JSON 对象写到标准输出；`--output` 同时保存 UTF-8 JSON 文件。异常堆栈写到标准错误。批量运行保留每个指标的 `.log`，另输出：

- `<metric>.<repeat>.json`：单项原始结果，包括参数、环境、时间戳、单位和限制条件。
- `summary.json`：完整结果及 `pass / partial / fail / error / skip` 数量。
- `summary.csv`：UTF-8 BOM 编码，可在 Excel 中打开；列为 `middleware,metric,repeat,status,field,value`。字段路径扁平化，数组以 JSON 保存在单元格中，单位在对应 `.unit` 行。

结构定义见 `result.schema.json`。量值使用 `{"value": 数值或null, "unit": "单位"}`，计数、布尔探针和序号直接输出。比例使用 `ratio`，取值 0～1；百分比使用 `%`。无法测得的值不填 0，使用 `null` 或显式未验证状态。

示例结构（数值仅示意，实际输出还包括环境和原始样本）：

```json
{
  "schema_version": "1.0",
  "middleware": "vsoa",
  "metric": "latency",
  "layer": "performance",
  "status": "pass",
  "measurements": {
    "attempted": 200,
    "successful": 200,
    "failed": 0,
    "rtt": {
      "count": 200, "unit": "ms",
      "min": 0.08, "mean": 0.12,
      "p50": 0.11, "p95": 0.18, "p99": 0.25,
      "max": 0.30, "stddev": 0.04
    }
  }
}
```

分位数采用排序后线性插值，标准差采用总体标准差。延迟、抖动、可靠性、实时性文件保留逐请求样本，失败样本不冒充成功时延。内存文件保留 RSS 采样序列。

状态含义：

- `pass`：规定探针或测量已成功完成；**不是**“性能优于其他中间件”。丢包测试 `pass` 只表示实验与计数有效，不表示没有丢包或全部恢复。
- `partial`：已测部分通过，但该维度存在明确未验证项目，例如其他 OS、TLS、线上活跃度、拥塞 QoS。
- `fail`：内容错误、请求失败、指定软实时 deadline 不满足等可解释的不通过。
- `error`：测试异常、启动失败或整项超时，不能形成完整测量。
- `skip`：格式保留的跳过状态。

退出码：`0` 表示 `pass / partial / skip`；`1` 表示 `fail / error`；非法 CLI 参数由 argparse 返回 `2`。非法参数没有测量结果 JSON。`validate_results.py` 检查结构、量值、分位数和计数恒等式；它验证报告是否自洽，不把 `fail / error` 改为通过，也不是通用 JSON Schema 验证器。

## 4. 参数

各独立文件都支持 `--help`。公共参数统一接受，只有相关指标使用它们，所有参数均记录在结果中。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--samples` | 200 | 有限样本次数；启动测试是新客户端次数，发现测试是热查询次数；丢包测试最大 10000 |
| `--warmup` | 20 | 延迟 / 抖动 / 实时性 / 可靠性 / 吞吐量 / CPU / 内存的预热请求数，可为 0 |
| `--payload-bytes` | 256 | 单方向二进制数据区长度，1～60000；不是含协议开销的包长 |
| `--duration` | 2.0 s | 吞吐量、CPU、内存的测量时长；排空或最后一个请求可能延长总时长 |
| `--timeout` | 2.0 s | RPC / 探针等待；丢包测试中也用于原生恢复观察期；Position 使用发行包内部查询超时 |
| `--case-timeout` | 120 s | 整项硬截止时间，超时输出 error 并结束本项子进程树；长测量需增大 |
| `--window` | 32 | 吞吐测试的最大并发未完成 RPC 数 |
| `--interval-ms` | 10 ms | 抖动 / 实时性计划发送间隔；丢包测试实际间隔为该值与 10ms 的较小者 |
| `--deadline-ms` | 10 ms | 实时性测试用户自选的软截止阈值 |
| `--loss-rate` | 0.1 | 丢包代理 Bernoulli 丢弃概率；不是有限样本必然出现的丢包比例 |
| `--seed` | 42 | 丢包代理随机种子 |
| `--retries` | 3 | 应用层最大缺失 ID 补发轮数，可为 0 |
| `--sample-interval` | 0.05 s | 资源监视器 RSS 采样间隔 |
| `--output` | 不保存文件 | 独立脚本结果路径 |

`run_all.py` 另支持 `--repeat`、`--output-dir`；其他参数原样转发给各指标。不支持给批量运行器传 `--output`。

## 5. 测量边界：对比其他中间件前必须阅读

1. **只测本机 IPv4 loopback。** 服务端是独立进程，客户端和负载发生器在本项进程内。Position 使用另外的 UDP 服务进程。丢包指标增加本地代理。无远端部署模式，不代表真实网卡、交换机、NAT、WAN 或跨 OS 表现。
2. **单向延迟不是 RTT / 2。** 用同机共享单调时钟分别在 `client.call` 前和服务端 handler 开始处取时间差；RTT 在客户端发起至响应回调处单独测量。包括 Python 序列化、协议栈、调度、事件派发，不是纯网络时延。
3. **负载模型必须匹配。** 吞吐是单连接、有界异步并发；CPU / 内存是单连接、顺序 RPC。延迟采用闭环请求；实时性保留计划释放时间并计入调度迟到，但不会生成真正独立的开环积压流量。
4. **吞吐量计已验证完成的消息。** 分母包括最后排空时间；只计算成功回显的数据区字节，不把 `send()` 成功、JSON 参数或传输头计作有效载荷。双方向 goodput 是请求及响应各一份数据，不是单方向链路带宽。
5. **CPU 100% 表示占满一颗逻辑核。** 客户端包含发生器和监视器开销；服务端包含解释器和库线程；Windows 启动器不作为服务端工作进程来计量。短测量受 CPU 计时粒度影响。
6. **内存是 RSS。** 不是 USS、Python 堆或泄漏判定；采样可能漏掉瞬态峰值，两进程 RSS 之和可能重复计算共享页。长期保留采样本身也占客户端内存。
7. **丢的是实际 UDP 数据报，不是假装丢业务消息。** TCP 控制通道原样转发；UDP 客户端到服务端方向按随机种子丢弃。先等待并报告原生观察结果，再通过 TCP 获取缺失序号，由测试程序补发。**应用补发和按序号去重不属于 VSOA 原生保证。** 未实现内核 TCP 丢包注入，不能宣称验证 TCP 重传性能。
8. **启动和发现分开计量。** 进程启动含 Python 导入及就绪轮询开销；Position 首次解析包含服务启动和库内部等待；热查询单独统计。Windows 在 Position 尚未绑定时可能收到 UDP connection reset，启动重试记录在 `startup_transient_errors`，不会计入热查询样本。
9. **保留未验证项。** 当前 QoS 仅测配置与读回；跨平台仅测当前机器；TLS、IPv6、硬实时、自动重连、故障转移、持久化、网络设备调度、线上生态活跃度均没有冒充实测结果。
10. **控制比较条件。** 后续其他中间件应使用同样机器、Python 版本、数据区大小、并发窗口、预热、时长、采样间隔和确认语义；先重复采样再比较分布。这里不生成置信区间或跨中间件排名。

## 6. 实现与证据

- `bench/worker.py`：真实 VSOA Server / Position 进程，不修改第三方包。
- `bench/common.py`：结果封装、进程清理、动态端口、单调时钟、统计、整项超时保护。
- `bench/probes.py`：datagram、发布订阅、stream 内容校验。
- `bench/resources.py`：实际服务端工作进程与客户端的资源统计。
- `bench/loss_proxy.py`：只影响本项测试的 TCP 控制 / UDP 丢包代理。

功能依据是安装的 `vsoa-1.0.4.dist-info/METADATA` 官方随包文档，以及 `vsoa/position.py`、`server.py`、`client.py`、`sockopt.py`、`sslwork.py`。项目地址由包元数据输出；没有使用未经核实的网络搜索结论。

测试不需要管理员权限，不更改防火墙 / QoS 全局策略，不关闭无关进程。服务只绑定 `127.0.0.1`。Server / Position 的阻塞循环由独立进程隔离，完成时清理本项子进程。端口是先选择再绑定，仍可能被其他程序抢占；这类失败会报告 error，而不会静默换成模拟结果。

实际验证记录见 `VALIDATION.md`。
