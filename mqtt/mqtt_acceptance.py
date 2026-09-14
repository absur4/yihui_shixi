"""An evidence audit, distinct from formula/schema validation."""
import hashlib
import json
from pathlib import Path

import yaml


def audit(output, runs):
    from mqtt_adapter import ROOT, expand, write_json
    formal = any(r['configuration']['benchmark_profile']=='formal' for r in runs)
    expected = expand(yaml.safe_load((ROOT/('config.formal.yaml' if formal else 'config.yaml')).read_text(encoding='utf-8')))
    rows=[]
    for cfg in expected:
        condition_id=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest()[:16]
        found=[r for r in runs if r.get('condition_id')==condition_id]
        good=[r for r in found if r['status']=='completed']
        reasons=[]
        negative=cfg['scenario_name']=='S10' and cfg['case'] in ('startup_timeout','discovery_failure')
        excluded=cfg['scenario_name']=='S09' and (cfg['network_loss_rate'] or cfg['network_delay_ms'] or cfg['network_jitter_ms'])
        if negative:
            good=[r for r in found if r.get('expected_negative_outcome')]
        if excluded:
            good=[r for r in found if r['status']=='not_tested' and r['limitations'] and r['injected_network_loss_rate'] is None]
        required=cfg['repeats']
        if len(good)<required:
            reasons.append(f'Need {required} valid repetitions; have {len(good)}')
        for run in good:
            if cfg['scenario_name']=='S01' and run['messages_sent']<10000:
                reasons.append('S01 successful source messages below 10000')
            if cfg['scenario_name'] in ('S03','S08'):
                required_seconds=300 if cfg['scenario_name']=='S08' else 10
                actual=(run['measurement_window']['send_end_ns']-run['measurement_window']['measurement_start_ns'])/1e9
                if actual<required_seconds:
                    reasons.append(f'Actual measurement duration {actual} below {required_seconds}')
            topology={'S05':(1,4),'S06':(4,1),'S07':(4,4)}.get(cfg['scenario_name'])
            if topology and (run['publisher_count'],run['subscriber_count'])!=topology:
                reasons.append('Incorrect topology')
            if topology and len(run.get('delivery_matrix',[]))!=topology[0]*topology[1]:
                reasons.append('Incomplete delivery matrix')
        rows.append(dict(scenario_name=cfg['scenario_name'],case=cfg['case'],payload_size_bytes=cfg['payload_size_bytes'],
            requested_publish_rate_hz=cfg['publish_rate_hz'],qos_profile=cfg['qos_profile'],network_profile=cfg['network_profile'],
            required_repeats=required,evidence_count=len(good),
            status='incomplete' if reasons else 'declared_not_tested' if excluded else 'negative_case_verified' if negative else 'verified',
            reasons=sorted(set(reasons)),run_ids=[r['run_id'] for r in found]))
    complete=all(row['status']!='incomplete' for row in rows)
    data=dict(status='verified_with_declared_S09_exclusions' if complete else 'incomplete',
              all_network_tests_executed=False,conditions=rows,
              note='S09 nonbaseline conditions are explicitly not_tested under document 11.7; no packet-loss experiment is claimed')
    write_json(Path(output)/'acceptance.json',data)
    lines=['# MQTT 验收证据核对','','结论：'+data['status'],'',
           'S09 非零弱网条件明确 not_tested，未声称完成网络层丢包实验。负向发现/超时案例独立核对，不伪装为正常通信 completed。', '',
           '| 场景 / 条件 | payload B | Hz/pub | QoS / 网络 | 有效证据 / 要求 | 状态 |',
           '|---|---:|---:|---|---:|---|']
    for row in rows:
        lines.append(f"| {row['scenario_name']}/{row['case']} | {row['payload_size_bytes']} | {row['requested_publish_rate_hz']} | "
                     f"{row['qos_profile']}/{row['network_profile']} | {row['evidence_count']}/{row['required_repeats']} | {row['status']} |")
    lines += ['', '逐轮原始结果、运行编号和缺口原因见 acceptance.json；公式与 Schema 校验见 validation.json。', '']
    (Path(output)/'ACCEPTANCE.md').write_text('\n'.join(lines),encoding='utf-8')
    return data
