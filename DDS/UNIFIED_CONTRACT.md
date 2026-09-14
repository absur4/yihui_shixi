# 四中间件统一适配契约（指标定义 1.0）

本文件把 Fast DDS 实现中与 VSOA、MQTT、Zenoh 适配器必须保持一致的部分单独列出。迁移到其他中间件时，替换 `fastdds_bench/endpoint.py` 和 transport/QoS 映射，保留配置字段、生命周期、原始观察、指标算法、JSON Schema 和分组规则。

## 1. 单轮生命周期

1. 控制器记录 `harness_start_ns`，启动本轮所需进程并开始资源采样。
2. 每个端点完成中间件对象创建后写 `ready_ns`。
3. 等待所有 Publisher/Subscriber 完成全匹配，记录发现窗口。
4. 控制器写统一 barrier 时间；所有端点使用同一个 `perf_counter_ns` 时间域。
5. 执行 warm-up；消息标为 phase 0，不写正式原始记录。
6. 正式发送；消息标为 phase 1，使用聚合速率/聚合条数调度。
7. 所有发布者写完后，控制器记录统一 `send_complete_ns` 并写 `send_complete.json`；订阅者在看到它时另存接收快照供审计。
8. 继续接收 `drain_seconds`，然后统一停止。
9. 端点关闭中间件对象并写 CPU 时间；控制器停止资源采样。
10. 只从不可变二进制原始记录计算单轮指标，再写 JSON 和 SHA-256。

`packet_loss` 的“before”边界使用统一时间域中的 `receive_timestamp_ns <= send_complete_ns`，不会把文件标记传播延迟混入恢复样本。订阅者本地快照用于核对标记传播和原始记录完整性；四种适配器必须使用相同边界。

## 2. 消息和计数

统一消息逻辑字段：

| 字段 | 类型 | 规则 |
|---|---:|---|
| `publisher_id` | uint32 | 单轮内从 0 开始 |
| `sequence_number` | uint64 | 每个发布者独立从 0 单调递增 |
| `send_timestamp_ns` | uint64 | `perf_counter_ns()` |
| `payload_length` | uint32 | 必须等于配置的应用 payload 字节数 |
| `checksum` | uint32 | 应用 payload 的 CRC32 |
| `phase` | uint8 | 0=warm-up，1=formal |
| `payload` | bytes/string | 确定性 ASCII；长度精确，最大 1 MiB |

唯一键为 `(publisher_id, sequence_number)`。广播时，同一消息在每个订阅者上分别构成一次预期交付。

`publish_rate_hz` 和 `message_count` 都使用聚合口径。若有 P 个发布者，调度槽为：

```text
global_slot = local_sequence * P + publisher_id
target_ns   = formal_start_ns + global_slot * 1e9 / publish_rate_hz
```

因此 4P、1000 Hz 是总计 1000 Hz，而不是每个发布者 1000 Hz。

## 3. 有效样本和恢复样本

一个接收观察只有同时满足以下条件才是有效交付：

- DDS/中间件声明数据有效；
- payload 可解码；
- `(publisher_id, sequence_number)` 能在成功发送原始记录中找到；
- 发送时间戳、声明长度、声明校验和与发送端原始记录一致；
- 实际长度和实际 CRC32 与本条件一致；
- 对本 Publisher→Subscriber 链路而言尚未出现过。

在 `send_complete_ns` 之前到达的有效唯一消息进入原始延迟和吞吐样本。该边界之后、排空结束前首次到达的有效唯一消息只进入：

- `valid_unique_delivery_count`；
- `recovered_count`；
- `final_packet_loss`；
- `recovery_time_ms`。

它们不进入延迟、抖动和接收吞吐样本。

## 4. 指标公式

设有效测量延迟为 `L`，应用 payload 为 B 字节。

- `latency_ms = mean(L)`。
- P95/P99：排序后 `h=(n-1)p`，非整数位置线性插值。
- `latency_std_ms = statistics.pstdev(L)`，即总体标准差。
- `jitter_ms`：先对每个 `(publisher, subscriber)` 链路按序列号排序，求相邻延迟差绝对值，再对全部差值取均值。
- `throughput_mbps = Nreceived_before * B * 8 / (Tdelivery * 1e6)`。
- `offered_throughput_mbps = Nsent * B * 8 / (Tsend * 1e6)`。
- `cpu_percent = sum(process_cpu_seconds) / Tresource * 100`；一个逻辑核为 100%。
- `memory_mb = max(sum(RSS_endpoint)) / 1e6`。
- `packet_loss = (Nexpected - Nreceived_before) / Nexpected`。
- `final_packet_loss = (Nexpected - Nreceived_after) / Nexpected`。
- `Nexpected = Nsent * subscriber_count`。
- `recovery_time_ms`：从发送完成标记到最后一个恢复交付；若发送完成前已全部交付则为 0，存在未恢复丢失且无恢复交付则为 `null`。
- `latency_drift_ms`：按发送时间排序的最后 10% 延迟均值减最前 10% 均值。
- `memory_growth_mb`：最后有效 RSS 和最初有效 RSS 的差，允许为负。

时间窗口不足、分母为 0、无有效样本、未测量或不适用时必须为 JSON `null`；不能输出 NaN、Infinity 或用 0 冒充。

## 5. 原始记录

每个端点使用独立文件，避免锁竞争和把 1 MiB payload 重复保存在 Python 对象中。

文件头均为 little-endian：

```text
<8sII = magic[8], format_version, record_size
```

发送记录：

```text
<IQQII = publisher_id, sequence, send_ns, payload_length, checksum
magic  = FDBSND01
```

接收记录：

```text
<IQQQIIIII = publisher_id, sequence, send_ns, receive_ns,
             declared_length, declared_checksum,
             actual_length, actual_checksum, flags
magic       = FDBRCV01
```

flags：bit0 数据有效，bit1 解码成功，bit2 长度正确，bit3 校验和正确。任何截断、magic、版本或 record size 不匹配都必须拒绝分析。

## 6. 统一 JSON 和分组

Suite 顶层包含环境、单轮 `runs` 和可重算 `summary`。每轮包含完整实际配置、统一指标、计数、错误、时间、原始文件路径和 SHA-256。

横向比较只能使用以下全部字段完全一致的组：

```text
module_name, scenario_name, payload_size_bytes, publish_rate_hz,
publisher_count, subscriber_count, transport_mode, qos_profile,
network_profile
```

此外报告层必须检查 duration、warm-up、drain、指标版本、操作系统和硬件一致；本实现把这些保存在单轮配置和环境中，不会偷偷混入组平均。`summary.valid_repeats` 只统计 `status=passed` 的轮次，少于 5 时 `formal_minimum_met=false`。

## 7. 外部注入接口边界

S09 网络仿真和 S11 故障动作必须放在四种中间件共用的控制层，不能在某个适配器的接收回调中伪造。共同编排器至少应记录：

- 目标进程/接口和可复核身份；
- 计划与实际故障开始、恢复时间（单调时钟和 UTC）；
- 延迟、抖动、丢包、带宽的计划值和工具实测值；
- 注入器名称、版本、命令/配置及退出状态；
- 故障前、故障中、恢复后的阶段边界；
- 原始注入日志及 SHA-256。

只有注入动作实际成功后，才允许把 `external_impairment_confirmed` 设为 `true`。进程/Broker/Router/网络故障也必须由编排器确认完成；配置中仅存在 `fault_plan` 不代表故障已发生。

## 8. Fast DDS 特有映射

| 统一字段 | Fast DDS 实现 |
|---|---|
| 发布/订阅 | DDS DataWriter / DataReader |
| 发现完成 | 每个 writer/reader 的 match count 达到对端总数 |
| reliable | `RELIABLE_RELIABILITY_QOS` |
| best effort | `BEST_EFFORT_RELIABILITY_QOS` |
| UDPv4 | 显式 UDPv4 descriptor，禁用 built-in transport 和 Data Sharing |
| SHM | 显式 SHM descriptor，禁用 built-in transport 和 Data Sharing |
| payload | IDL bounded string，精确 ASCII 字节数 |
| 类型支持 | Fast DDS-Gen `-python` 生成，CMake/SWIG 自动编译 |

未来适配 VSOA、MQTT、Zenoh 时，不要复用 Fast DDS 的 QoS 名称冒充等价语义；应建立明确映射，并把无法等价的字段写入 feature profile 和报告限制。
