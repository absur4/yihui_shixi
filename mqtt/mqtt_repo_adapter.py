"""Repository contract adapter for MQTT.

This module is intentionally independent of the Songfei server.  It wraps the
existing real MQTT measurement engine and maps its result to the repository's
interfaces.MiddlewareAdapter contract.
"""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / 'package'
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

try:
    from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec  # type: ignore
except ImportError:  # Allows local contract tests before the parent repo is cloned.
    class MiddlewareAdapter:  # type: ignore
        name = ''
    @dataclass
    class ScenarioSpec:  # type: ignore
        scenario_name: str
        configuration: dict
    ScenarioResult = dict  # type: ignore

from mqtt_adapter import execute, expand, normalize

SCENARIO_TITLES = {
    'S01': 'point_to_point_latency', 'S02': 'message_size_scan',
    'S03': 'large_message_throughput', 'S04': 'send_rate_scan',
    'S05': 'one_to_many_broadcast', 'S06': 'many_to_one_fanin',
    'S07': 'many_to_many_mesh', 'S08': 'long_duration_stability',
    'S09': 'weak_network_recovery', 'S10': 'startup_discovery',
    'S11': 'reconnect_fault_recovery', 'S12': 'data_correctness',
}


def _config(spec, parameters):
    raw = copy.deepcopy(getattr(spec, 'configuration', spec if isinstance(spec, dict) else {}))
    raw.update(parameters or {})
    if hasattr(spec, 'scenario_name'):
        raw['scenario_name'] = spec.scenario_name
    return normalize(raw)


def _map_result(result: dict, cfg: dict) -> dict:
    """Map legacy engine names to the stable repository names without dropping evidence."""
    result = copy.deepcopy(result)
    result.update(middleware_id='mqtt', middleware_version=result.get('module_version', '2.0.0'),
                  scenario_title=SCENARIO_TITLES.get(result['scenario_name'], result['scenario_name']),
                  statistics=dict(sample_level_metrics={k: result.get(k) for k in (
                      'latency_ms', 'latency_p95_ms', 'latency_p99_ms', 'latency_std_ms',
                      'jitter_ms', 'throughput_mbps', 'cpu_percent', 'memory_mb', 'packet_loss')},
                                  formulas={'percentile': 'h=(n-1)*p; linear interpolation',
                                            'std': 'population standard deviation, denominator n',
                                            'jitter': 'mean absolute adjacent delay difference by link/sequence',
                                            'throughput': 'unique valid payload bytes / delivery window'},
                                  sample_level='sample_level', repeat_level='run_level'),
                  link_metrics=result.get('delivery_matrix', []),
                  actual_payload_size_bytes=result.get('actual_payload_size_bytes'),
                  transport_mode=cfg['transport_mode'], qos_profile=cfg['qos_profile'])
    # The repository contract expects these fields even when a capability is unavailable.
    for key in ('startup_time_ms', 'discovery_time_ms', 'recovery_time_ms', 'cpu_percent', 'memory_mb',
                'latency_ms', 'latency_p95_ms', 'latency_p99_ms', 'latency_std_ms', 'throughput_mbps',
                'jitter_ms', 'packet_loss', 'final_packet_loss', 'achieved_publish_rate_hz'):
        result.setdefault(key, None)
    result['configuration'] = cfg
    return result


class Adapter(MiddlewareAdapter):
    name = 'mqtt'

    def __init__(self, root: Path | None = None):
        default_root = PACKAGE if (PACKAGE / 'config.yaml').exists() else HERE
        self.root = Path(root or default_root).resolve()
        self.config_path = self.root / 'config.yaml'

    def metadata(self) -> dict:
        return {'id': 'mqtt', 'name': 'mqtt', 'label': 'MQTT', 'version': '2.0.0',
                'available': True, 'transport_options': ['tcp'],
                'qos_options': [{'value': 'qos0', 'label': 'QoS 0 · 至多一次'},
                                {'value': 'qos1', 'label': 'QoS 1 · 至少一次'},
                                {'value': 'qos2', 'label': 'QoS 2 · 恰好一次'}],
                'notes': ['本机 MQTT 3.1.1/TCP，Broker 由每轮独立启动',
                          '多机协同和真实网络层丢包注入未支持，相关结果明确 not_tested']}

    def catalog(self) -> list[dict]:
        config = json.loads('{}')
        import yaml
        config = yaml.safe_load(self.config_path.read_text(encoding='utf-8'))
        rows = []
        for item in expand(config):
            row = {k: item.get(k) for k in (
                'scenario_name', 'case', 'payload_size_bytes', 'publish_rate_hz',
                'publisher_count', 'subscriber_count', 'message_count', 'duration_seconds',
                'repeats', 'random_seed', 'warmup_seconds', 'drain_seconds', 'timeout_seconds',
                'network_delay_ms', 'network_jitter_ms', 'network_loss_rate', 'network_profile',
                'transport_mode', 'qos_profile')}
            row['scenario_title'] = SCENARIO_TITLES[item['scenario_name']]
            row['title'] = row['scenario_title']
            row['condition_id'] = f"{item['scenario_name']}:{item['case']}:{item['payload_size_bytes']}:{item['publish_rate_hz']}:{item['publisher_count']}:{item['subscriber_count']}:{item['qos_profile']}:{item['network_profile']}"
            rows.append(row)
        return rows

    def run(self, scenario: ScenarioSpec, parameters: dict) -> ScenarioResult:
        cfg = _config(scenario, parameters)
        output = Path(parameters.get('output_dir', self.root / 'results'))
        result = execute(cfg, output, repeat=int(parameters.get('repeat', 1)))
        return _map_result(result, cfg)


def create_adapter() -> Adapter:
    return Adapter()
