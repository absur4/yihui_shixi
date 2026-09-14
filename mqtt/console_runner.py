"""Repository-required subprocess entry point: python mqtt/console_runner.py spec.json."""
from __future__ import annotations
import json
import signal
import sys
from pathlib import Path

from mqtt_repo_adapter import Adapter, _map_result


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: python mqtt/console_runner.py <job_dir>/spec.json', file=sys.stderr)
        return 2
    spec_path = Path(sys.argv[1]).resolve()
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    config = dict(spec.get('config') or {})
    cases = spec.get('cases') or [config]
    output = Path(spec.get('output') or spec_path.parent / 'output')
    output.mkdir(parents=True, exist_ok=True)
    (spec_path.parent / 'console.log').write_text('', encoding='utf-8')
    stopped = False

    def stop(_sig, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    adapter = Adapter()
    runs = []
    for index, case in enumerate(cases, 1):
        if stopped:
            break
        merged = dict(config, **case, output_dir=output)
        repeat = int(merged.get('repeat', index))
        print(f'[RUN] {merged.get("scenario_name")} repeat={repeat}', flush=True)
        result = adapter.run(case, merged)
        result['repeat'] = repeat
        runs.append(result)
        run_path = output / 'runs' / f"{result['run_id']}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    status = 'cancelled' if stopped else ('completed' if runs and all(r['status'] == 'completed' for r in runs) else 'error')
    for result in runs:
        result['status'] = status if stopped else result['status']
    suite = {'schema_version': '1.0', 'metric_definition_version': '1.0', 'middleware_id': 'mqtt',
             'middleware_version': '2.0.0', 'status': status, 'runs': runs,
             'configuration': config, 'environment': runs[-1].get('environment', {}) if runs else {},
             'limitations': ['Repository adapter wraps the standalone MQTT engine; all unsupported capabilities remain explicit.']}
    (output / 'result.json').write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'RESULT {output / "result.json"} status={status}', flush=True)
    return 0 if status in ('completed', 'cancelled') else 1


if __name__ == '__main__':
    raise SystemExit(main())
