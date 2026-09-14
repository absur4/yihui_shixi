"""One condition, one repeat; a missing dependency is an explicit not_tested run."""
import argparse
import datetime as dt
import json
from pathlib import Path
from adapter import HERE, base_config, build_cases
from console_runner import run_spec
from mqtt_evidence import write_json


def main():
    from mqtt_processes import contain_children
    contain_children()
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', type=Path)
    args = parser.parse_args()
    job = (args.job_dir or HERE/'results'/('smoke-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    job.mkdir(parents=True, exist_ok=True)
    case = build_cases(0, dict(repeats=1, message_count=20, duration_seconds=0,
                             publish_rate_hz=100, warmup_seconds=.1, drain_seconds=.2), False)[0]
    write_json(job/'spec.json', dict(middleware='mqtt', config=base_config(), cases=[case],
        plan=[dict(scenario_id='S01', scenario_name='S01', planned_repeats=1)],
        output=str(job/'output'), logs=str(job/'output/logs')))
    return run_spec(job/'spec.json')


if __name__ == '__main__':
    raise SystemExit(main())
