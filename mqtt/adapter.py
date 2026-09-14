"""MQTT contract entry. Importing and catalog access require only the standard library."""
from __future__ import annotations
import copy
import importlib.util
import importlib.metadata
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for directory in (HERE.parent, HERE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

try:
    from interfaces import MiddlewareAdapter, ScenarioResult, ScenarioSpec
    from interfaces.scenarios import SCENARIO_BY_ID
except ModuleNotFoundError as exc:
    if exc.name not in ('interfaces', 'interfaces.scenarios'):
        raise
    # Standalone packaging: no duplicated scene titles; repository catalog is authoritative.
    SCENARIO_BY_ID = {}
    class MiddlewareAdapter:
        pass
    class ScenarioSpec:
        pass
    class ScenarioResult:
        def __init__(self, **fields):
            self.__dict__.update(fields)

from mqtt_config import VERSION, normalize, expand
from mqtt_evidence import COUNTS, unavailable_result, write_json
from mqtt_metrics import describe
from mqtt_results import METRICS

MIDDLEWARE_ID = 'mqtt'


def _load_config():
    text = (HERE / 'config.yaml').read_text(encoding='utf-8')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError('配置已改为 YAML 文本，请安装 PyYAML 或保留交付的 JSON 兼容 YAML 格式') from exc
        return yaml.safe_load(text)


def base_config():
    return normalize({k: v for k, v in _load_config().items() if k not in ('scenarios', 'description')})


def _probe(cfg=None):
    cfg = cfg or base_config()
    problems = []
    for module, package in [('paho.mqtt.client', 'paho-mqtt'), ('yaml', 'PyYAML'),
                            ('psutil', 'psutil'), ('jsonschema', 'jsonschema')]:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            problems.append(f'缺少 {package}：请使用执行器解释器运行 pip install -r mqtt/requirements.txt')
    broker = Path(cfg['broker_executable'])
    broker = broker if broker.is_absolute() else HERE / broker
    if not broker.is_file():
        problems.append(f'未找到 MQTT broker：请安装 Mosquitto 并配置 broker_executable（当前 {broker}）')
    return problems


def metadata():
    problems = _probe()
    try:
        paho_version = importlib.metadata.version('paho-mqtt')
    except importlib.metadata.PackageNotFoundError:
        paho_version = None
    return dict(id=MIDDLEWARE_ID, name=MIDDLEWARE_ID, label='MQTT', version=VERSION,
                paho_version=paho_version, available=not problems, transport_options=['tcp'],
                qos_options=[dict(value=f'qos{i}', label=label) for i, label in enumerate(
                    ['QoS 0 · 至多一次', 'QoS 1 · 至少一次', 'QoS 2 · 恰好一次'])],
                notes=['version 为 mqtt_config.VERSION 适配器版本，paho_version 为安装包实际版本',
                       '本机独立 Mosquitto broker；未支持跨机测量及真实包级弱网注入'] + problems +
                      ([] if SCENARIO_BY_ID else ['独立目录尚无仓库 interfaces；放入 mqtt/ 后从 interfaces/scenarios.py 读取标题']))


def _title(scenario_id):
    scenario = SCENARIO_BY_ID.get(scenario_id)
    return scenario.title if scenario is not None else scenario_id


def catalog():
    rows = expand(_load_config())
    for index, row in enumerate(rows):
        sid = row['scenario_name']
        if SCENARIO_BY_ID and sid not in SCENARIO_BY_ID:
            raise ValueError(f'配置中的场景 {sid} 不在 interfaces/scenarios.py 中')
        row.update(scenario_id=sid, title=_title(sid), scenario_title=_title(sid),
                   condition_id=f'{sid}_{index+1:02d}', case_repeats=row['repeats'])
    return rows


def build_cases(template_index, configuration, matrix):
    rows = catalog()
    if isinstance(template_index, bool) or not isinstance(template_index, int) or not 0 <= template_index < len(rows):
        raise ValueError('条件索引超出范围')
    if not isinstance(matrix, bool):
        raise ValueError('matrix 必须为布尔值')
    selected = rows[template_index]
    if matrix:
        return [copy.deepcopy(row) for row in rows if row['scenario_name'] == selected['scenario_name']]
    if not isinstance(configuration, dict):
        raise ValueError('configuration 必须为对象')
    editable = set('payload_size_bytes publish_rate_hz publisher_count subscriber_count message_count duration_seconds repeats random_seed warmup_seconds drain_seconds timeout_seconds network_delay_ms network_jitter_ms network_loss_rate network_profile transport_mode qos_profile'.split())
    unexpected = set(configuration)-editable
    if unexpected:
        raise ValueError('不允许修改字段：' + ', '.join(sorted(unexpected)))
    row = normalize(dict(selected, **configuration))
    if row['transport_mode'] != 'tcp' or row['qos_profile'] not in ('qos0', 'qos1', 'qos2'):
        raise ValueError('传输必须为 tcp，QoS 必须为 qos0/qos1/qos2')
    row['case_repeats'] = row['repeats']
    return [row]


def runner_command():
    # Use the same interpreter as metadata's probe so availability cannot disagree.
    return [sys.executable, str(HERE / 'console_runner.py')]


def _config(scenario, parameters):
    if isinstance(scenario, dict):
        raw = copy.deepcopy(scenario)
    else:
        raw = dict(getattr(scenario, 'default_parameters', {}))
        raw['scenario_id'] = scenario.scenario_id
    raw = dict(base_config(), **raw)
    raw.update(parameters or {})
    if raw.get('scenario_id'):
        raw['scenario_name'] = raw['scenario_id']
    # Runtime paths do not belong in serializable engine configuration.
    raw.pop('output_dir', None)
    return normalize(raw)


def _map_result(result, cfg):
    result = copy.deepcopy(result)
    actual = dict(result.get('configuration') or cfg)
    actual['rate_scope'] = 'per_publisher'
    sid = actual['scenario_name']
    result.update(middleware_id=MIDDLEWARE_ID, middleware_version=VERSION,
                  scenario_id=sid, scenario_name=sid, scenario_title=_title(sid),
                  configuration=actual, rate_scope='per_publisher')
    for key in METRICS + COUNTS:
        result.setdefault(key, None)
    stats = result.setdefault('statistics', {})
    for key in ('latency_ms', 'jitter_ms', 'cpu_percent', 'memory_mb', 'discovery_time_ms'):
        stats.setdefault(key, describe([]))
    result['link_metrics'] = [dict(publisher=row['publisher_id'], subscriber=row['subscriber_id'],
        messages_sent=row['messages_sent'], messages_received=row['unique_deliveries'],
        messages_received_after_recovery=row['final_unique_deliveries'],
        duplicate_count=row['duplicate_count'], out_of_order_count=row['out_of_order_count'],
        packet_loss=row['missing_count']/row['messages_sent'] if row['messages_sent'] else None,
        final_packet_loss=(row['messages_sent']-row['final_unique_deliveries'])/row['messages_sent'] if row['messages_sent'] else None,
        latency_ms=row.get('latency_distribution', describe([]))) for row in result.get('delivery_matrix', [])]
    result.setdefault('limitations', [])
    return result


def result_dict(result):
    """Recover the complete common run record from the repository ScenarioResult."""
    return copy.deepcopy(result.metrics)


class Adapter(MiddlewareAdapter):
    name = MIDDLEWARE_ID
    metadata = staticmethod(metadata)
    catalog = staticmethod(catalog)
    build_cases = staticmethod(build_cases)
    base_config = staticmethod(base_config)
    runner_command = staticmethod(runner_command)

    def run(self, scenario: ScenarioSpec, parameters: dict) -> ScenarioResult:
        parameters = parameters or {}
        cfg = _config(scenario, parameters)
        output = Path(parameters.get('output_dir', HERE/'results'))
        repeat = parameters.get('repeat', 1)
        problems = _probe(cfg)
        if problems:
            result = unavailable_result(cfg, output, repeat, problems)
        else:
            try:
                from mqtt_adapter import execute
                result = execute(cfg, output, repeat=repeat)
            except Exception as exc:
                result = unavailable_result(cfg, output, repeat, ['执行失败，请查看 errors'], 'error')
                result['errors'] = [repr(exc)]
        result = _map_result(result, cfg)
        write_json(output/'runs'/f"{result['run_id']}.json", result)
        sample = json.loads((output / result['samples_path']).read_text(encoding='utf-8'))
        return ScenarioResult(middleware=MIDDLEWARE_ID, middleware_version=VERSION,
                              scenario_id=result['scenario_id'], status=result['status'], metrics=result,
                              samples={'latencies_ms': sample['latencies_ms']}, configuration=result['configuration'],
                              errors=result.get('errors', []), started_at=result['test_start_time'],
                              finished_at=result['test_end_time'])


def create_adapter():
    return Adapter()
