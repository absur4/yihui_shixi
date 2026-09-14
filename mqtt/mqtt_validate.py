import json
import math
from pathlib import Path

import jsonschema

from mqtt_results import METRICS, analyze, group_results
from mqtt_metrics import resource_window_metrics


def equivalent(a, b, path='root'):
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), f'{path}: keys differ'
        for key in a:
            equivalent(a[key], b[key], path+'.'+key)
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f'{path}: lengths differ'
        for i, (x, y) in enumerate(zip(a, b)):
            equivalent(x, y, f'{path}[{i}]')
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), f'{path}: {a} != {b}'
    else:
        assert a == b, f'{path}: {a} != {b}'


def validate_output(output):
    from mqtt_adapter import ROOT, write_json
    suite = json.loads((output/'result.json').read_text(encoding='utf-8'))
    validator = jsonschema.Draft202012Validator(json.loads((ROOT/'result.schema.json').read_text(encoding='utf-8')),
                                               format_checker=jsonschema.FormatChecker())
    raw_count = 0
    for run in suite['runs']:
        validator.validate(run)
        stored = json.loads((output/'runs'/run['run_id']/'result.json').read_text(encoding='utf-8'))
        equivalent(run, stored)
        if 'drain_end_ns' in run['measurement_window']:
            recalculated = analyze(output/'runs'/run['run_id'], run['configuration'],
                                   run['measurement_window']['measurement_start_ns'], run['measurement_window']['drain_end_ns'])
            for key, value in recalculated.items():
                if key == 'time_windows':
                    # CPU/RSS are joined from a separate resource stream.
                    assert len(value)==len(run[key]), 'Time window count differs from raw data'
                    for a, b in zip(value, run[key]):
                        equivalent(a, {k: b[k] for k in a}, key)
                else:
                    equivalent(value, run[key], key)
            resources = [json.loads(x) for x in (output/'runs'/run['run_id']/'resources.jsonl').read_text().splitlines()]
            for window in run['time_windows']:
                a=run['measurement_window']['measurement_start_ns']+window['start_offset_seconds']*1e9
                b=run['measurement_window']['measurement_start_ns']+window['end_offset_seconds']*1e9
                expected=resource_window_metrics(resources,run['resource_start_ns'],a,b)
                equivalent(expected,{key:window[key] for key in expected},'resource_window')
            equivalent(max(s['memory_mb'] for s in resources), run['memory_mb'], 'memory_mb')
            cpu = sum(p['cpu_time_delta_seconds'] for p in run['resource_processes'])/run['resource_window_seconds']*100
            equivalent(cpu, run['cpu_percent'], 'cpu_percent')
            raw_count += 1
    summaries = json.loads((output/'summary.json').read_text(encoding='utf-8'))
    equivalent(group_results(suite['runs']), summaries['groups'], 'summary')
    result = dict(status='passed', schema_validated_runs=len(suite['runs']),
                  raw_recomputed_runs=raw_count, summary_recomputed=True,
                  note='Validation proves data/schema consistency, not full scenario acceptance')
    write_json(output/'validation.json', result)
    from mqtt_acceptance import audit
    acceptance=audit(output,suite['runs'])
    result['acceptance_status']=acceptance['status']
    print(json.dumps(result), flush=True)
