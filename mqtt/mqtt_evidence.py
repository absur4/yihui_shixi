"""Real sample export and dependency-free result persistence."""
import csv
import datetime as dt
import json
import hashlib
import os
import platform
import uuid
from pathlib import Path
from mqtt_config import VERSION
from mqtt_metrics import describe
from mqtt_results import METRICS

COUNTS = ['messages_sent', 'messages_received', 'unique_deliveries', 'expected_deliveries',
          'duplicate_count', 'out_of_order_count', 'corrupted_count', 'latency_sample_count']


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temp, path)


def finalize(result, cfg, output, folder):
    """Export only actual original-phase valid deliveries, in CSV arrival order."""
    output, folder = Path(output), Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for sub in range(cfg['subscriber_count']):
        values, points = [], []
        source = folder / f'subscriber-{sub}.csv'
        if source.exists():
            with source.open(encoding='utf-8', newline='') as f:
                for row in csv.reader(f):
                    if len(row) < 6:
                        continue
                    pub, seq, sent, received, phase = map(int, row[:5])
                    if phase == 1 and row[5] == 'valid' and received >= sent:
                        latency = (received - sent) / 1e6
                        values.append(latency)
                        points.append(dict(sequence=seq, publisher=pub, latency=latency))
        write_json(folder / f'subscriber-{sub}.result.json',
                   dict(latencies_ms=values, latency_sample_count=len(values), samples=points))
    result['samples_path'] = f"artifacts/{result['run_id']}/subscriber-0.result.json"
    stats = result.setdefault('statistics', {})
    for name in ('latency_ms', 'jitter_ms'):
        stats.setdefault(name, describe([]))
    resources = folder / 'resources.jsonl'
    rows = [json.loads(line) for line in resources.read_text(encoding='utf-8').splitlines() if line] if resources.exists() else []
    for name in ('cpu_percent', 'memory_mb'):
        stats[name] = describe([row.get(name) for row in rows])
    stats['discovery_time_ms'] = describe([(row['ready_ns']-row['conn_start_ns'])/1e6
                                         for row in result.get('endpoint_readiness', [])])
    result['rate_scope'] = 'per_publisher'
    result['test_end_time'] = result.get('test_end_time') or utc()


def unavailable_result(cfg, output, repeat, problems, status='not_tested'):
    run_id = str(uuid.uuid4())
    result = dict(run_id=run_id, module_name='mqtt', module_version=VERSION,
                  status=status, repeat=repeat, test_start_time=utc(), test_end_time=None,
                  configuration=dict(cfg), environment=dict(python_version=platform.python_version(),
                  os=platform.platform(), adapter_version=VERSION, broker_version=None, paho_version=None),
                  limitations=list(problems), errors=[], measurement_window={},
                  schema_version='1.0', metric_definition_version='1.0')
    for name in ('scenario_name', 'payload_size_bytes', 'publish_rate_hz', 'publisher_count',
                 'subscriber_count', 'transport_mode', 'qos_profile', 'network_profile'):
        result[name] = cfg[name]
    result.update({name: None for name in METRICS + COUNTS + [
        'injected_network_loss_rate', 'messages_planned', 'application_recovered', 'late_native_deliveries',
        'messages_not_sent', 'actual_payload_size_bytes', 'metadata_size_bytes', 'wire_message_size_bytes']})
    result['configured_publish_rate_hz'] = cfg['publish_rate_hz']
    result['discovery_supported'] = True
    result['environment']['logical_cpu_count'] = os.cpu_count()
    result['environment']['fingerprint'] = hashlib.sha256(json.dumps(result['environment'], sort_keys=True).encode()).hexdigest()

    finalize(result, cfg, output, Path(output)/'artifacts'/run_id)
    write_json(Path(output)/'runs'/f'{run_id}.json', result)
    return result
