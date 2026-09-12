"""通信中间件统一测试需求文档定义的 S01-S12 场景。"""

from . import ScenarioSpec


SCENARIOS = (
    ScenarioSpec("S01", "point_to_point_latency", "点对点低延迟", "1P/1S，在 1 KiB 消息和 100/1000 Hz 下测量延迟。", ("latency_ms", "latency_p95_ms", "latency_p99_ms", "latency_std_ms"), {"payload_size_bytes": 1024, "publish_rate_hz": 100}),
    ScenarioSpec("S02", "message_size_scan", "消息大小扫描", "固定拓扑遍历推荐消息大小矩阵，观察延迟、吞吐和丢包变化。", ("latency_ms", "throughput_mbps", "packet_loss"), {"payload_sizes": [1024, 4096, 16384, 32768, 49152, 65536, 262144, 1048576]}),
    ScenarioSpec("S03", "large_message_throughput", "大消息吞吐", "使用 64 KiB、256 KiB、1 MiB 等大消息寻找最大稳定速率。", ("throughput_mbps", "latency_p95_ms", "cpu_percent", "memory_mb"), {"payload_sizes": [65536, 262144, 1048576]}),
    ScenarioSpec("S04", "send_rate_scan", "发送速率扫描", "遍历 100/1000/5000/10000 Hz，记录速率升高后的退化。", ("throughput_mbps", "latency_p99_ms", "packet_loss"), {"publish_rates_hz": [100, 1000, 5000, 10000]}),
    ScenarioSpec("S05", "one_to_many_broadcast", "一对多广播", "1 个发布者向 4 个订阅者广播，分别统计各订阅者交付。", ("throughput_mbps", "latency_p95_ms", "packet_loss", "memory_mb"), {"publisher_count": 1, "subscriber_count": 4}),
    ScenarioSpec("S06", "many_to_one_fanin", "多对一汇聚", "4 个发布者向 1 个订阅者汇聚，统计各发布者和总交付。", ("throughput_mbps", "latency_p95_ms", "packet_loss", "cpu_percent"), {"publisher_count": 4, "subscriber_count": 1}),
    ScenarioSpec("S07", "many_to_many_mesh", "多对多并发", "4P/4S 并发通信，展示 16 条链路及资源消耗。", ("throughput_mbps", "latency_p95_ms", "packet_loss", "cpu_percent", "memory_mb"), {"publisher_count": 4, "subscriber_count": 4}),
    ScenarioSpec("S08", "long_duration_stability", "长时间稳定性", "至少 5 分钟运行，观察延迟漂移、抖动和内存增长。", ("latency_ms", "jitter_ms", "memory_mb", "cpu_percent"), {"duration_seconds": 300}),
    ScenarioSpec("S09", "weak_network_recovery", "弱网与恢复", "在 1/5/10% 丢包及不同延迟、抖动下测量恢复效果。", ("packet_loss", "final_packet_loss", "latency_p95_ms", "jitter_ms"), {"loss_rates": [0.01, 0.05, 0.10]}),
    ScenarioSpec("S10", "startup_discovery", "启动与发现", "覆盖冷/热启动、多端点发现和发现超时。", ("startup_time_ms", "discovery_time_ms", "latency_ms"), {"conditions": ["cold_start", "warm_start", "multi_endpoint", "startup_timeout"]}),
    ScenarioSpec("S11", "reconnect_fault_recovery", "重连与故障恢复", "进程、连接或网络故障后恢复，并记录恢复时间。", ("startup_time_ms", "discovery_time_ms", "final_packet_loss", "latency_p95_ms"), {"fault_types": ["publisher_restart", "subscriber_restart", "connection_reset"]}),
    ScenarioSpec("S12", "data_correctness", "数据正确性", "校验长度、校验和、序列号，统计缺失、重复、乱序和损坏。", ("packet_loss", "final_packet_loss"), {"correctness_checks": True}),
)

SCENARIO_BY_ID = {scenario.scenario_id: scenario for scenario in SCENARIOS}


def get_scenario(scenario_id: str) -> ScenarioSpec:
    try:
        return SCENARIO_BY_ID[scenario_id]
    except KeyError as exc:
        raise ValueError(f"未知测试场景: {scenario_id}") from exc


__all__ = ["SCENARIOS", "SCENARIO_BY_ID", "get_scenario"]
