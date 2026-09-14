SCENARIOS = {
    "S01": {"title":"点对点低延迟", "publisher_count":1,"subscriber_count":1,"payload_size_bytes":1024,"publish_rate_hz":100,"duration_seconds":10},
    "S02": {"title":"消息大小扫描", "matrix":"payload", "publisher_count":1,"subscriber_count":1,"publish_rate_hz":1000},
    "S03": {"title":"大消息吞吐", "matrix":"large_payload", "publisher_count":1,"subscriber_count":1,"publish_rate_hz":0},
    "S04": {"title":"发送速率扫描", "matrix":"rate", "publisher_count":1,"subscriber_count":1,"payload_size_bytes":1024},
    "S05": {"title":"一对多广播", "publisher_count":1,"subscriber_count":4},
    "S06": {"title":"多对一汇聚", "publisher_count":4,"subscriber_count":1},
    "S07": {"title":"多对多并发", "publisher_count":4,"subscriber_count":4},
    "S08": {"title":"长时间运行", "publisher_count":1,"subscriber_count":1,"duration_seconds":300},
    "S09": {"title":"弱网与恢复", "matrix":"network", "publisher_count":1,"subscriber_count":1},
    "S10": {"title":"启动与发现", "publisher_count":1,"subscriber_count":1,"duration_seconds":3},
    "S11": {"title":"重连与故障恢复", "publisher_count":1,"subscriber_count":1,"duration_seconds":30},
    "S12": {"title":"数据正确性", "publisher_count":1,"subscriber_count":1,"message_count":10000},
}

