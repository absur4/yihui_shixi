# Zenoh 统一测试适配器

本目录是交给统一测试控制台的 Zenoh 适配器。机器接口 ID 固定为 `zenoh`，页面显示名为 `Zenoh`。适配器内部复用仓库根目录的 `zenoh_bench` 真实测量引擎；发布者和订阅者在独立进程中使用 `eclipse-zenoh` session、publisher 和 subscriber，不生成模拟性能数据。

## 接口

```python
from Zenoh.adapter import create_adapter

adapter = create_adapter()
metadata = adapter.metadata()
catalog = adapter.catalog()
result = adapter.run(catalog[0], {"repeat": 1, "run_id": "demo-r01", "artifact_dir": "..."})
```

`catalog()` 返回 S01-S12 的展开标准条件。配置字段使用接入规范中的 `payload_size_bytes`、`publish_rate_hz`、`network_loss_rate` 等名称；`network_loss_rate` 是 0-1 比例，`transport_mode` 使用小写值。单轮 `result` 使用统一字段、UTC 时间、十进制 Mbit/s 和 RSS MB，并保存真实样本与链路矩阵。

## 独立运行协议

```powershell
python Zenoh/console_runner.py <job_dir>/spec.json
```

`spec.json` 可以包含 `config`、`cases` 或 `plan`，以及 `output`、`logs` 相对 job 目录的路径。例如：

```json
{
  "config": {"repeats": 5},
  "cases": [{"scenario_name": "S01", "payload_size_bytes": 1024, "publish_rate_hz": 100, "message_count": 10000}],
  "output": "output",
  "logs": "output/logs"
}
```

作业目录会生成 `job.json`、`spec.json`（由调用方提供）、`console.log`、`output/result.json`、`output/runs/<run_id>.json`，以及每轮的 `output/artifacts/<run_id>/` 原始引擎结果。

## 能力矩阵

| 场景 | 状态 | 真实测量或限制 |
|---|---|---|
| S01 点对点低延迟 | supported | 1P/1S，1 KiB，100/1000 Hz，测量延迟和尾延迟 |
| S02 消息大小扫描 | supported | 1 KiB 至 1 MiB 八种 payload，逐条件逐轮运行 |
| S03 大消息吞吐 | supported | 64 KiB、256 KiB、1 MiB，记录吞吐、P95、CPU、RSS |
| S04 发送速率扫描 | supported | 100、1000、5000 Hz 和最大稳定速率入口 |
| S05 一对多广播 | supported | 1P/4S，保存每个订阅者交付和 1x4 链路 |
| S06 多对一汇聚 | supported | 4P/1S，保存每个发布者交付 |
| S07 多对多并发 | supported | 4P/4S，保存完整 4x4 链路矩阵 |
| S08 长时间运行 | supported | 目录配置为至少 300 秒，资源窗口由 worker 采样 |
| S09 弱网与丢包 | unsupported | 没有 Windows 真实网络注入器时不报告完成；结果明确标记 |
| S10 启动与发现 | partial | 冷启动测量 session ready 与 Key Expression 首条有效样本；热启动、发现失败和超时显式记录为 `not_tested` |
| S11 重连与故障恢复 | unsupported | 未自动停止/重启 peer、Router 或进程，结果明确标记 |
| S12 数据正确性 | supported | 长度、magic、checksum、缺失、重复、乱序和损坏统计 |

可靠性选项通过 `qos_profile` 映射为 Zenoh `Reliability` 和 `CongestionControl`；`transport_mode` 支持 `tcp`、`udp`、`quic`、`other`，其中无 TLS 材料的 QUIC 回环由底层配置拒绝。S10 的发现完成条件是订阅 wildcard Key Expression 后收到第一条有效消息，不把 session 启动时间复制为发现时间。

## 依赖和验收

依赖为 `eclipse-zenoh>=1.10,<2`、`psutil>=7,<8` 和 `jsonschema>=4,<5`，与根目录 `pyproject.toml` 一致。正式报告每个条件使用至少 5 次重复，建议 10 次。`result.schema.json` 校验单轮结果，`feature_profile.schema.json` 校验原生能力画像。

S09 和 S11 当前是有意显式未完成项；它们不能因为已有应用层延迟/丢弃或等待超时而被标为 `completed`。完成这两项需要在适配器外提供 Windows 网络注入和 peer/Router/进程故障编排，并把实际恢复时间、故障阶段计数写回结果。
