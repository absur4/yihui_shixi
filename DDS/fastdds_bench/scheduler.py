"""每发布者口径的发送调度。

统一口径（DDS_readme_1.md §5.6）：`publish_rate_hz` 与 `message_count` 都是
**每个发布者**的值，与 VSOA/MQTT/Zenoh 保持一致，因此多发布者条件可以直接
横向比较。

```text
target_ns = formal_start_ns + sequence * 1e9 / publish_rate_hz
```

每个发布者只按自己的序号排队，不再把序号摊到全局槽位上。
"""

from __future__ import annotations


def paced_target_ns(start_ns: int, sequence: int, rate_hz: float) -> int:
    """返回第 ``sequence`` 条消息在每发布者口径下的计划发送时刻（单调时钟 ns）。"""
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if rate_hz <= 0:
        raise ValueError("rate_hz must be positive")
    return int(start_ns) + int(sequence * 1_000_000_000 / rate_hz)
