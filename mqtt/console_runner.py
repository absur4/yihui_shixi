"""Execute a console spec with atomic progress and a complete per-job archive."""
from __future__ import annotations
import contextlib
import datetime as dt
import json
import signal
import sys
import traceback
from pathlib import Path
from adapter import Adapter, VERSION, _config, _map_result, result_dict
from mqtt_evidence import utc, write_json, unavailable_result
from mqtt_results import group_results


def repeats_for(case):
    value = case.get('case_repeats', case.get('repeats', 1))
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('重复次数必须为正整数')
    return value


def suite_status(runs, stopped):
    if stopped or any(r['status'] == 'cancelled' for r in runs):
        return 'cancelled'
    if all(r['status'] in ('completed', 'not_tested', 'unsupported') or r.get('expected_negative_outcome') for r in runs):
        return 'completed'
    return 'error'


class Log:
    def __init__(self, job, output):
        self.paths = list(dict.fromkeys([job/'console.log', output/'console.log']))
        self.stdout = sys.stdout
        self.pending = ''

    def line(self, message):
        line = f"[{dt.datetime.now():%H:%M:%S}] {message}"
        for path in self.paths:
            with path.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        print(line, file=self.stdout, flush=True)

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line:
                self.line(line)
        return len(text)

    def flush(self):
        if self.pending:
            self.line(self.pending)
            self.pending = ''


def _inside_job(value, default, job):
    path = Path(value) if value else default
    path = (path if path.is_absolute() else job/path).resolve()
    if path == job or job not in path.parents:
        raise ValueError('output 和 logs 必须位于 spec.json 所在作业目录内')
    return path


def run_spec(spec_path):
    spec_path = Path(spec_path).resolve()
    job_dir = spec_path.parent
    spec = json.loads(spec_path.read_text(encoding='utf-8-sig'))
    output = _inside_job(spec.get('output'), job_dir/'output', job_dir)
    if (output/'result.json').exists():
        raise ValueError('作业已存在结果，请使用新的作业目录')
    logs = _inside_job(spec.get('logs'), output/'logs', job_dir)
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    logger = Log(job_dir, output)
    started = utc()
    runs, problems, plan, config, cases = [], [], [], {}, []
    planned = 0
    stopped = False
    old_handlers = {}
    job_path = job_dir/'job.json'
    job = json.loads(job_path.read_text(encoding='utf-8')) if job_path.exists() else {}
    job.update(id=job.get('id', job_dir.name), middleware_id='mqtt', started=started,
               ended=None, status='running', total=0, matrix=bool(spec.get('matrix', False)),
               case=spec.get('case'))

    def stop(_sig, _frame):
        nonlocal stopped
        if not stopped:
            stopped = True
            raise KeyboardInterrupt

    def persist(status, ended=None):
        limitations = list(dict.fromkeys(problems + [v for r in runs for v in r.get('limitations', [])]))
        suite = dict(schema_version='1.0', metric_definition_version='1.0',
                     middleware_id='mqtt', middleware_version=VERSION, status=status,
                     planned_runs=planned, completed_runs=len(runs), plan=plan,
                     test_start_time=started, test_end_time=ended,
                     environment=runs[-1].get('environment', {}) if runs else {},
                     configuration=config, scenario_summaries=group_results(runs), runs=runs,
                     limitations=limitations)
        write_json(output/'result.json', suite)
        write_json(output/'summary.json', dict(sample_level='run_level', groups=suite['scenario_summaries']))
        job.update(status=status, ended=ended, total=planned, completed=len(runs))
        write_json(job_path, job)

    current = None
    try:
        if (output/'result.json').exists():
            raise ValueError('作业已存在结果，请使用新的作业目录')
        if spec.get('middleware', 'mqtt') != 'mqtt':
            raise ValueError('此执行器只处理 mqtt 作业')
        config = spec.get('config') or {}
        cases = spec.get('cases') or []
        if not isinstance(config, dict) or not isinstance(cases, list) or not cases:
            raise ValueError('spec 必须包含非空 cases 列表和 config 对象')
        planned = sum(repeats_for(case) for case in cases)
        plan = spec.get('plan') or []
        if not isinstance(plan, list):
            raise ValueError('plan 必须为列表')
        if plan:
            if len(plan) != len(cases):
                raise ValueError('plan 与 cases 条件数量不一致')
            for case, item in zip(cases, plan):
                if item.get('planned_repeats') != repeats_for(case):
                    raise ValueError('plan.planned_repeats 与实际重复次数不一致')
                for field in ('scenario_id', 'scenario_name'):
                    if field in case and field in item and case[field] != item[field]:
                        raise ValueError('plan 与 cases 场景标识不一致')
        else:
            plan = [dict(scenario_id=c.get('scenario_id', c.get('scenario_name')),
                         scenario_name=c.get('scenario_name'), planned_repeats=repeats_for(c)) for c in cases]
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, stop)
        persist('running')
        adapter = Adapter()
        for case in cases:
            repeats = repeats_for(case)
            for repeat in range(1, repeats+1):
                if stopped:
                    break
                params = dict(config, **case)
                params.update(output_dir=str(output), logs_dir=str(logs), repeat=repeat)
                cfg = _config(case, params)
                current = (cfg, repeat)
                name = case.get('condition_id', case.get('scenario_name', cfg['scenario_name']))
                logger.line(f'START {name} repeat={repeat}/{repeats}')
                with contextlib.redirect_stdout(logger), contextlib.redirect_stderr(logger):
                    result = result_dict(adapter.run(case, params))
                result['repeat'] = repeat
                write_json(output/'runs'/f"{result['run_id']}.json", result)
                runs.append(result)
                current = None
                if result['status'] == 'cancelled':
                    stopped = True
                persist('running')
                logger.line(f"END {name} status={result['status']} sent={result.get('messages_sent')} received={result.get('messages_received')}")
            if stopped:
                break
    except KeyboardInterrupt:
        stopped = True
        if current:
            cfg, repeat = current
            result = _map_result(unavailable_result(cfg, output, repeat, ['用户取消了本轮执行'], 'cancelled'), cfg)
            write_json(output/'runs'/f"{result['run_id']}.json", result)
            runs.append(result)
            logger.line(f"END {cfg['scenario_name']} status=cancelled sent=None received=None")
    except Exception as exc:
        problems.append(str(exc))
        logger.line('ERROR ' + str(exc))
        with (logs/'runner-error.log').open('a', encoding='utf-8') as f:
            traceback.print_exc(file=f)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    status = 'cancelled' if stopped else ('error' if problems else suite_status(runs, False))
    persist(status, utc())
    logger.flush()
    logger.line(f'RESULT {output / "result.json"} status={status}')
    return 130 if status == 'cancelled' else 0 if status == 'completed' else 1


def main():
    if len(sys.argv) != 2:
        print(f'[{dt.datetime.now():%H:%M:%S}] 用法：python mqtt/console_runner.py <job>/spec.json', flush=True)
        return 1
    try:
        from mqtt_processes import contain_children
        contain_children()
        return run_spec(sys.argv[1])
    except Exception as exc:
        print(f'[{dt.datetime.now():%H:%M:%S}] ERROR {exc}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
