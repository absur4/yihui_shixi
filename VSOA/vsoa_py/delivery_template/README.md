# VSOA Windows x64 独立测试模块 v1.3

本包提供不依赖目标电脑 Python 环境的 `vsoa.exe`，内置测试发布者、订阅者、VSOA Position 和 UDP 弱网代理。无需管理员权限，也无需额外服务。

## 启动与停止

- 双击 `run.bat`：读取 `config.yaml`，运行五类基础场景，输出 `outputs/result.json`。
- 命令行标准验收：`vsoa.exe --config config.yaml --suite qualification --output outputs\qualification`。
- 单场景：`vsoa.exe --suite qualification --scenario S05_1P_4S_broadcast --output outputs\S05`。
- 分阶段：`vsoa.exe --suite qualification --phase weak_network --output outputs\weak`。
- 查看计划：`vsoa.exe --suite qualification --list-scenarios`。
- 按 `Ctrl+C` 停止；程序使用 Windows Job Object 清理子进程。中断后可用原命令增加 `--resume`。

日志在 `logs/`；逐轮 JSON 在输出目录的 `runs/`；原始计数、资源样本和工作进程证据在 `artifacts/`。

## 配置

统一字段包括 `module_name`、`module_version`、`scenario_name`、`duration_seconds`、`message_count`、`payload_size_bytes`、`publish_rate_hz`、`publisher_count`、`subscriber_count`、`transport_mode`、`qos_profile`、`warmup_seconds`、`drain_seconds`、`repeats`、`timeout_seconds`、`network_delay_ms`、`network_jitter_ms`、`network_loss_rate`、`random_seed`、端口、输出和日志字段。兼容字段会在读取时转换并检查冲突。默认有效重复为 5；正式报告应将 `qualification.repeats` 设置为 10。

默认端口块从 `30050` 开始。本机进程仅监听 IPv4 loopback；请确保对应连续端口未被占用。

## S01–S12

- S01：1 KiB、100/1000 Hz、每轮至少 10000 条。
- S02：1/4/16/32/48/64/256 KiB 和 1 MiB 消息大小扫描。
- S03：64/256 KiB、1 MiB 大消息稳定吞吐候选。
- S04：100/1000/5000 Hz 和最大稳定候选速率扫描。
- S05/S06/S07：1P/4S、4P/1S、4P/4S，输出端点和链路结果。
- S08：至少 300 秒长稳。
- S09：baseline、1%+5/5 ms、5%+20/20 ms、10%+50/50 ms。
- S10：冷/热启动、多端点、并发发现、启动超时、发现失败入口；每条件至少 10 轮。
- S11：发布者、订阅者、Position、连接和短时网络中断恢复。
- S12：长度、CRC32 checksum、缺失、重复、乱序、损坏及不可解析统计。

## 计量口径与限制

延迟使用同机单调时钟；P95/P99 使用线性插值，标准差为总体标准差，jitter 按同一发布者到同一订阅者的相邻序列计算。交付吞吐以首次成功发送至最后计入接收的窗口计算，另输出 offered throughput。中间件资源包含发布者、订阅者和 Position；控制器与 UDP 代理单列为测试基础设施。

1 MiB 等消息使用应用分片后经 VSOA 发送，不能解释为原生单帧能力。应用补发、去重和日志不是 VSOA 原生持久化。未验证 TLS、IPv6、跨主机、跨操作系统和硬实时。

