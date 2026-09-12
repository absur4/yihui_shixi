# 统一适配器接口

`scenarios.py` 是需求文档 S01–S12 的唯一场景目录。每个中间件目录可放置一个 `adapter.py`，提供以下任一形式：

```python
from interfaces import ScenarioResult

class Adapter:
    name = "dds"

    def metadata(self):
        return {"name": self.name, "version": "厂商版本", "available": True}

    def run(self, scenario, parameters):
        # 运行真实中间件测试，返回 ScenarioResult
        return ScenarioResult(self.name, "厂商版本", scenario.scenario_id, "completed")

def create_adapter():
    return Adapter()
```

`run()` 返回的 `metrics` 键使用统一名称：`latency_ms`、`latency_p95_ms`、`latency_p99_ms`、`latency_std_ms`、`throughput_mbps`、`cpu_percent`、`memory_mb`、`packet_loss`、`final_packet_loss`、`startup_time_ms`、`discovery_time_ms`、`jitter_ms`。`samples` 用于图表绘制，例如 `{"latency_ms": [..]}`。

适配器只负责中间件调用和测量，不负责启动浏览器或拼装 UI。
