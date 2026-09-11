# 验证记录

验证日期：2026-09-08。以下是本工作目录真实执行的结果，不是模拟输出。

## 环境

- Windows 11，AMD64，16 个逻辑 CPU。
- Python 3.13.9；VSOA 1.0.4；psutil 7.2.2。
- 本机 IPv4 loopback，独立服务端进程；默认 256-byte 数据区。
- 没有修改 VSOA 安装包源码，没有开启管理员权限或改变系统网络策略。

## 完整运行

1. 默认参数连续两轮：`results/validation-default/summary.json`，30 项结果，22 pass、8 partial、0 fail、0 error。
2. 超时保护修复后的最终完整运行：`results/validation-final/summary.json`，15 项结果，11 pass、4 partial、0 fail、0 error。
3. `validate_results.py results/validation-default` 校验 30 份结果通过；最终目录 15 份结果也全部通过结构与统计不变量校验。

最终运行命令：

```powershell
.\.venv\Scripts\python.exe run_all.py --output-dir results\validation-final
.\.venv\Scripts\python.exe validate_results.py results\validation-final
```

重跑时请使用新的输出目录。

### 四项 partial 的含义

| 指标 | 已通过部分 | 未验证部分 |
| --- | --- | --- |
| 跨平台 | 当前 Windows 的 RPC / UDP 收发 | Linux、macOS、跨 OS 互通 |
| QoS | 0 / 3 / 7 优先级设置读回、非法值拒绝 | 线缆 DSCP 抓包、交换机调度、拥塞效果 |
| 传输方式 | TCP / UDP datagram、UDP publish、TCP stream | TLS 证书握手、IPv6、真实网络环境 |
| 生态成熟度 | 包元数据、随包文档、API 可导入 | 在线提交活跃度、issue 响应、下载量、生产采用率 |

这些 partial 是有意保留的证据边界，不是将未做的测试计为通过。

## 边界和负向检查

- 0% UDP 注入丢包：初始丢包率为 0。
- 100% UDP 注入丢包：补发后残余丢包率仍为 1，没有假报恢复成功。
- 0 次应用重试：没有补发轮次记录。
- 60000-byte 数据区：延迟测试成功。
- 极小软实时 deadline：正确报告 fail，退出码 1。
- `--samples 0`：拒绝非法参数，退出码 2。
- 极小整项 timeout：正确报告 error，退出码 1；连续 5 次结果均为可解析且自洽的 JSON。
- 手动破坏成功计数：结果校验器正确拒绝报告。
- 空样本、单样本、中位数、p99 线性插值计算检查通过。
- 23 个 Python 文件全部编译检查通过，其中 15 个为独立指标文件。
- `pip check`：没有依赖冲突。
- 最终检查当前工作目录对应的 `bench.worker` 进程：无残留。

边界结果在 `results/validation-edge/` 和 `results/validation-watchdog/`。其中 `deadline-fail.json` 和 `watchdog.json` 的不通过状态是预期的负向测试结果。

## 检查中修复的问题

1. Windows 子进程退出后日志句柄释放时序：增加子进程树清理和有限文件清理重试。
2. Position 启动前 UDP 查询可能返回 Windows connection reset：只在启动就绪阶段重试，并把瞬态错误保存到结果，热查询异常不静默忽略。
3. 整项 watchdog 与主线程可能同时写报告：增加单次报告锁、启动 / 取消同步及超时后的非零退出码，避免损坏 JSON 或遗漏新启动的服务进程。

早期 `results/validation-smoke/` 中保留了排错记录；它不是最终验收结果。请以 `results/validation-final/` 为准。

## 对测量数值的解释

最终运行的一次本机观测：RTT 平均约 0.0451 ms，单向 client-to-handler 平均约 0.0235 ms；窗口 32 的完成吞吐约 47881 RPC/s。默认 200 条 UDP 数据报首次缺失 24 条，测试程序显式补发后剩余 0 条。

这些数值只描述此次本机运行，不代表网络环境中的保证，不证明优于其他中间件，也不把应用层补发归为 VSOA 原生丢包恢复。比较或引用时必须附带相应 JSON 中的参数、环境和限制说明。
