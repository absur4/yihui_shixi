"""Standard-library configuration shared by the contract and engine."""
VERSION = '2.0.0'
DEFAULTS = dict(module_name='mqtt', module_version=VERSION, scenario_name='S01',
    duration_seconds=10, message_count=0, payload_size_bytes=1024, publish_rate_hz=1000,
    publisher_count=1, subscriber_count=1, transport_mode='tcp', qos_profile='qos1',
    warmup_seconds=1, drain_seconds=.5, repeats=5, timeout_seconds=60,
    network_delay_ms=0, network_jitter_ms=0, network_loss_rate=0, random_seed=20260910,
    host='127.0.0.1', port=0, network_profile='baseline', connect_timeout_seconds=10,
    publish_timeout_seconds=5, max_inflight=20, metric_window_seconds=10,
    broker_executable='broker/mosquitto.exe', recovery_probe_count=100,
    fault_duration_seconds=1, recovery_timeout_seconds=12, benchmark_profile='acceptance',
    suite_kind='public_comparable', case='standard', startup_mode='cold',
    saturation_loss_threshold=.001, saturation_latency_p99_ms=100,
    saturation_cpu_percent=800, saturation_memory_mb=2048,
    stable_rate_min_fraction=.90, stable_rate_candidates=[100, 1000, 5000, 10000, 20000],
    require_stable_rate=False, rate_search=False, output_dir='outputs',
    sample_level='sample_level')
ALIASES = dict(test_duration_sec='duration_seconds', message_size_bytes='payload_size_bytes',
               send_frequency_hz='publish_rate_hz', test_timeout_sec='timeout_seconds')


def normalize(config):
    data = dict(config)
    for old, new in ALIASES.items():
        if old in data:
            if new in data and data[new] != data[old]:
                raise ValueError(f'字段冲突： {old} / {new}')
            data[new] = data.pop(old)
    c = dict(DEFAULTS, **data)
    if str(c['module_name']).lower() != 'mqtt':
        raise ValueError('此适配器仅支持 MQTT')
    c['module_name'], c['module_version'] = 'mqtt', VERSION
    if c['scenario_name'] not in [f'S{i:02}' for i in range(1, 13)]:
        raise ValueError('scenario_name 必须为标准场景 ID')
    for key in ['message_count', 'payload_size_bytes', 'publisher_count', 'subscriber_count', 'repeats', 'random_seed', 'max_inflight']:
        if isinstance(c[key], bool) or not isinstance(c[key], int) or c[key] < 0:
            raise ValueError(f'{key} 必须为非负整数')
    if min(c['publisher_count'], c['subscriber_count'], c['repeats'], c['max_inflight']) < 1:
        raise ValueError('端点数、重复次数和 max_inflight 必须为正整数')
    for key in ['duration_seconds', 'publish_rate_hz', 'warmup_seconds', 'drain_seconds',
                'network_delay_ms', 'network_jitter_ms', 'network_loss_rate', 'timeout_seconds',
                'connect_timeout_seconds', 'publish_timeout_seconds', 'metric_window_seconds']:
        if not isinstance(c[key], (int, float)) or isinstance(c[key], bool) or not 0 <= c[key] < float('inf'):
            raise ValueError(f'{key} 必须为有限非负数')
    if c['network_loss_rate'] > 1 or c['metric_window_seconds'] == 0 or c['timeout_seconds'] == 0:
        raise ValueError('丢包率必须在 0 到 1 之间，窗口和超时必须大于零')
    if not c['message_count'] and not c['duration_seconds']:
        raise ValueError('时长或消息数至少一项必须大于零')
    qos = {'qos0': 0, 'best_effort': 0, 'qos1': 1, 'reliable': 1, 'qos2': 2}
    if c['qos_profile'] not in qos:
        raise ValueError('qos_profile 必须为 qos0/qos1/qos2/best_effort/reliable')
    c['qos'] = qos[c['qos_profile']]
    c['qos_semantics'] = ['at_most_once', 'at_least_once', 'exactly_once'][c['qos']]
    c['stop_rule'] = 'message_count_per_publisher' if c['message_count'] else 'duration_seconds'
    c['rate_scope'] = 'per_publisher'
    candidates = c['stable_rate_candidates']
    if not isinstance(candidates, list) or not candidates or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < float('inf') for v in candidates):
        raise ValueError('stable_rate_candidates 必须为非空正数列表')
    if candidates != sorted(set(candidates)):
        raise ValueError('stable_rate_candidates 必须严格递增')
    return c


def expand(config):
    common = {k: v for k, v in config.items() if k not in ('scenarios', 'description')}
    return [normalize(dict(common, **scenario)) for scenario in config.get('scenarios', [{}])]

