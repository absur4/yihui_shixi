"""Unified Fast DDS benchmark engine.

本包属于「引擎层」：允许依赖 fastdds 原生绑定与第三方库，但只能在真正执行
测量时被导入。契约层（`DDS/adapter.py`）与编排层（`DDS/console_runner.py`）
不得在顶层导入本包中依赖原生的模块。
"""

# 统一命名（根 README §2）：中间件 ID 必须是小写 `dds`，厂商与版本单独记录。
MIDDLEWARE_ID = "dds"
MIDDLEWARE_LABEL = "DDS"
VENDOR = "eProsima Fast DDS"

ADAPTER_VERSION = "2.0.0"
SCHEMA_VERSION = "1.0"
METRICS_DEFINITION_VERSION = "1.0"

# 根 README §6 的状态枚举。
RUN_STATUSES = (
    "completed",
    "error",
    "cancelled",
    "timeout",
    "unsupported",
    "not_tested",
)

# 资源统计口径：DDS 没有 Broker/Router 进程，只统计端点进程。
RESOURCE_SCOPE = "participant processes (publishers + subscribers)"

# 发现完成定义（写进每轮结果，便于跨中间件解释差异）。
DISCOVERY_READY_CONDITION = (
    "每个 DataWriter/DataReader 的 matched count 达到对端总数（0 -> P 或 0 -> S）"
)

# 恢复语义（写进每轮结果）：DDS 的可靠性是协议级重传，不是应用层重放。
RECOVERY_SEMANTICS = (
    "RELIABLE 使用 DDS 协议级重传；BEST_EFFORT 不重传。"
    "本适配器不做应用层重放或补发，恢复时间只是断点后再次收到数据的时刻"
)
