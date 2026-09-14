# Zenoh 统一测试适配器

本目录是一个可独立上传、安装和运行的 Zenoh 测试库。只需将整个 `Zenoh/` 放到统一测试仓库根目录，不依赖仓库顶层的 `zenoh_bench/`。

## 目录结构

```text
Zenoh/
  __init__.py
  adapter.py
  console_runner.py
  requirements.txt
  pyproject.toml
  result.schema.json
  feature_profile.json
  feature_profile.schema.json
  zenoh_bench/                 # 内置真实测量引擎
  examples/smoke-job/spec.json # 最小冒烟配置
```

`__pycache__`、`*.pyc`、`build/`、`dist/` 和运行时 `results/` 不应提交。

## 环境安装

在统一仓库根目录执行：

```powershell
py -3 -m pip install -r Zenoh/requirements.txt
```

也可以将本目录安装为库：

```powershell
py -3 -m pip install ./Zenoh
```

安装后可使用：

```python
from Zenoh import create_adapter

adapter = create_adapter()
print(adapter.metadata())
print(len(adapter.catalog()))
```

适配器固定使用 `Adapter.name = "zenoh"`，并优先复用统一仓库中的 `interfaces.MiddlewareAdapter`、`ScenarioSpec` 和 `ScenarioResult`。独立运行时会使用内置兼容类型。

## 运行测试

从统一仓库根目录运行最小真实 Zenoh 测试：

```powershell
py -3 Zenoh/console_runner.py Zenoh/examples/smoke-job/spec.json
```

安装为库后也可以运行：

```powershell
zenoh-console Zenoh/examples/smoke-job/spec.json
```

运行产物位于：

```text
Zenoh/examples/smoke-job/job.json
Zenoh/examples/smoke-job/console.log
Zenoh/examples/smoke-job/output/result.json
Zenoh/examples/smoke-job/output/runs/<run_id>.json
Zenoh/examples/smoke-job/output/artifacts/<run_id>/
```

这些运行产物已被 `.gitignore` 排除，不会误提交到 PR。正式验收产物应由测试组按作业目录归档。

## 统一接口

```python
adapter.metadata()                  # Zenoh 元数据和依赖状态
adapter.catalog()                   # S01-S12，共 31 个展开条件
adapter.run(scenario, parameters)   # 执行一轮真实测试
```

配置和结果字段遵循 `测试与可视化接入规范.md`。`network_loss_rate` 使用 0-1 比例；吞吐单位是 Mbit/s；延迟、抖动、发现和启动时间单位是 ms；内存是 RSS MB。无有效测量时返回 `null`，不使用固定数值伪造结果。

## 能力矩阵

| 场景 | 状态 | 测量或限制 |
|---|---|---|
| S01 点对点低延迟 | supported | 真实 1P/1S Zenoh 发布订阅，记录平均/P95/P99/总体标准差 |
| S02 消息大小扫描 | supported | 1 KiB 至 1 MiB payload 条件 |
| S03 大消息吞吐 | supported | 64 KiB、256 KiB、1 MiB，记录吞吐和资源 |
| S04 发送速率扫描 | supported | 100、1000、5000 Hz 和不限速入口 |
| S05 一对多广播 | supported | 1P/4S 和逐订阅者链路结果 |
| S06 多对一汇聚 | supported | 4P/1S 和逐发布者序列空间 |
| S07 多对多并发 | supported | 4P/4S、完整 16 条链路矩阵 |
| S08 长时间运行 | supported | 标准条件至少 300 秒 |
| S09 弱网与丢包 | unsupported | 没有真实 Windows 网络注入器时明确标记，不以应用层丢弃冒充 |
| S10 启动与发现 | partial | 冷启动和首条有效 Key Expression 交付可测；其他变体为 not_tested |
| S11 故障恢复 | unsupported | 尚未自动控制 peer、Router 或进程停止重启 |
| S12 数据正确性 | supported | 校验长度、magic、checksum、缺失、重复、乱序和损坏 |

## 依赖和限制

- `eclipse-zenoh>=1.10,<2`
- `psutil>=7,<8`
- `jsonschema>=4,<5`
- Python 3.10 及以上

QUIC 回环需要额外 TLS 证书和 Zenoh endpoint 配置。跨主机单向延迟要求 NTP/PTP 等时钟同步；同步不可靠时结果会保留警告。S09 和 S11 在外部注入/故障编排完成前不会标记为 `completed`。
