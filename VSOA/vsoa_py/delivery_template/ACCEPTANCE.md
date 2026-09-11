# VSOA v1.3 验收说明

## 自动检查

- `vsoa.exe --validate-config`：统一配置字段、重复键、范围、拓扑和端口块。
- `python -m standalone.validate_delivery outputs\result.json`：计数、总体方差、分位数、吞吐窗口、丢包及资源口径复算。
- `result.schema.json` 与 `feature_profile.schema.json`：统一 JSON Schema。
- `checksum.txt` 与 ZIP 同名 `.sha256`：文件和压缩包完整性。

## 完成条件

S01–S12 均有可执行入口；每个测试条件至少 5 个有效轮次，S10 每条件至少 10 轮，正式报告建议 10 轮。S08 的每轮实际测量窗口不得少于 300 秒。S09 必须有代理注入/转发/丢弃守恒计数。S05/S06/S07 分别检查 4 个订阅者、4 个发布者和 16 条链路。S12 必须输出长度、checksum、缺失、重复、乱序、损坏与不可解析计数。

只有消息大小、频率、拓扑、传输、QoS、网络条件和环境一致的运行可比较；网页发现多个配置指纹时会给出禁止混合排名警告。失败运行不得用零值代替，有效重复不足时不得标记完成。

## 本次构建状态

源码回归会执行基础场景 5 轮、配置计划检查、公式复算、双 Schema 校验、独立 EXE 启动与 ZIP CRC/校验和检查。完整标准套件结果以随包 `outputs/qualification/result.json` 和构建后的验收记录为准。

