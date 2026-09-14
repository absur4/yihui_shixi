import csv
import json
from collections import defaultdict
from pathlib import Path

from mqtt_metrics import describe, latency_metrics, loss, throughput
from mqtt_wire import HEADER

METRICS = ['latency_ms', 'latency_p95_ms', 'latency_p99_ms', 'latency_std_ms',
           'jitter_ms', 'throughput_mbps', 'offered_throughput_mbps', 'cpu_percent',
           'memory_mb', 'packet_loss', 'final_packet_loss', 'startup_time_ms',
           'discovery_time_ms', 'recovery_time_ms', 'achieved_publish_rate_hz']


def csv_rows(path):
    with open(path, encoding='utf-8', newline='') as stream:
        yield from csv.reader(stream)


def analyze(folder, cfg, formal_ns, drain_ns):
    folder = Path(folder)
    sent = defaultdict(dict)
    probe_sent = defaultdict(dict)
    acknowledgments = []
    failed = 0
    for pub in range(cfg['publisher_count']):
        for row in csv_rows(folder / f'publisher-{pub}.csv'):
            seq, stamp, ack, phase, ok, rc = map(int, row)
            if phase == 2 and ok:
                probe_sent[pub][seq] = stamp
            if phase != 1:
                continue
            if ok:
                sent[pub][seq] = stamp
                acknowledgments.append(ack)
            else:
                failed += 1
    all_sends = [t for messages in sent.values() for t in messages.values()]
    first = min(all_sends) if all_sends else None
    last = max(all_sends) if all_sends else None
    send_window = (last - first) / 1e9 if first is not None else None
    matrix, windows = [], defaultdict(list)
    raw_count = invalid = foreign = unparseable = invalid_metadata = 0
    duplicates = out_order = corrupted = length_errors = checksum_errors = 0
    late = recovered_probes = 0
    last_receive = None
    recovery_first = None
    probe_seen = set()
    probe_duplicates = probe_out_of_order = probe_corrupted = 0
    probe_high = defaultdict(lambda: -1)
    probe_matrix = defaultdict(int)
    window_original_keys = defaultdict(set)
    links = []
    for sub in range(cfg['subscriber_count']):
        link_data = {pub: dict(seen=set(), original=set(), lat=[], duplicate=0,
                              out_of_order=0, high=-1, last=None, late=0)
                     for pub in range(cfg['publisher_count'])}
        for row in csv_rows(folder / f'subscriber-{sub}.csv'):
            pub, seq, stamp, recv, phase = map(int, row[:5])
            status, wire_size, recovery = row[5], int(row[6]), int(row[7])
            if phase == 0:
                continue
            if phase == 2:
                key = (pub, sub, seq)
                if status != 'valid':
                    probe_corrupted += 1
                elif stamp == probe_sent[pub].get(seq):
                    if key in probe_seen:
                        probe_duplicates += 1
                        continue
                    probe_seen.add(key)
                    probe_out_of_order += seq < probe_high[pub, sub]
                    probe_high[pub, sub] = max(seq, probe_high[pub, sub])
                    probe_matrix[pub, sub] += 1
                    recovered_probes += 1
                    if recv >= cfg.get('recovery_eligible_after_ns', 0):
                        recovery_first = min(recovery_first or recv, recv)
                continue
            if phase == 1:
                raw_count += 1
            if status != 'valid':
                invalid += 1
                foreign += status == 'foreign_run'
                unparseable += status == 'unparseable'
                invalid_metadata += status == 'invalid_metadata'
                length_errors += status == 'length_error'
                checksum_errors += status == 'checksum_error'
                corrupted += status in ('length_error', 'checksum_error')
                continue
            # A valid delivery must correspond to an actually successful source send.
            if seq not in sent[pub] or stamp != sent[pub][seq] or recv < stamp:
                invalid_metadata += 1
                continue
            link = link_data[pub]
            if seq in link['seen']:
                link['duplicate'] += 1
                duplicates += 1
                continue
            link['seen'].add(seq)
            if seq < link['high']:
                link['out_of_order'] += 1
                out_order += 1
            link['high'] = max(link['high'], seq)
            if recovery or recv > drain_ns:
                late += 1
                link['late'] += 1
                continue
            link['original'].add(seq)
            latency = (recv - stamp) / 1e6
            link['lat'].append((seq, latency))
            link['last'] = max(link['last'] or recv, recv)
            last_receive = max(last_receive or recv, recv)
            bucket = int((recv - formal_ns) / 1e9 // cfg['metric_window_seconds'])
            windows[bucket].append((pub, sub, seq, latency))
            send_bucket = int((stamp-formal_ns)/1e9 // cfg['metric_window_seconds'])
            window_original_keys[send_bucket].add((pub, sub, seq))
        for pub, link in link_data.items():
            n_sent, n_orig, n_final = len(sent[pub]), len(link['original']), len(link['seen'])
            pfirst = min(sent[pub].values()) if sent[pub] else None
            window = (link['last'] - pfirst) / 1e9 if pfirst is not None and link['last'] else None
            matrix.append(dict(publisher_id=pub, subscriber_id=sub, messages_sent=n_sent,
                               unique_deliveries=n_orig, final_unique_deliveries=n_final,
                               missing_count=n_sent-n_orig, final_missing_count=n_sent-n_final, latency_distribution=describe([lat for _, lat in link['lat']]), duplicate_count=link['duplicate'],
                               out_of_order_count=link['out_of_order'], late_native_deliveries=link['late'],
                               packet_loss=loss(n_sent, n_orig), final_packet_loss=loss(n_sent, n_final),
                               throughput_mbps=throughput(n_orig, cfg['payload_size_bytes'], window),
                               delivery_window_seconds=window, **latency_metrics([link['lat']])))
            links.append(link['lat'])
    count_sent = len(all_sends)
    count_original = sum(r['unique_deliveries'] for r in matrix)
    count_final = sum(r['final_unique_deliveries'] for r in matrix)
    expected = count_sent * cfg['subscriber_count']
    duration = cfg['duration_seconds']
    planned = (cfg['message_count'] * cfg['publisher_count'] if cfg['message_count'] else
               int(duration * cfg['publish_rate_hz']) * cfg['publisher_count'] if cfg['publish_rate_hz'] else None)
    delivery_window = (last_receive-first)/1e9 if first is not None and last_receive else None
    actual_send_end = max(acknowledgments) if acknowledgments else formal_ns
    active_window = (actual_send_end-formal_ns)/1e9
    result = dict(**latency_metrics(links), actual_payload_size_bytes=cfg['payload_size_bytes'] if count_original else None,
                  metadata_size_bytes=HEADER.size, wire_message_size_bytes=None,
                  mqtt_application_message_size_bytes=HEADER.size+cfg['payload_size_bytes'],
                  configured_publish_rate_hz=cfg['publish_rate_hz'], rate_scope='per_publisher',
                  achieved_publish_rate_hz=count_sent/active_window/cfg['publisher_count'] if active_window > 0 else None,
                  achieved_aggregate_publish_rate_hz=count_sent/active_window if active_window > 0 else None,
                  messages_planned=planned, messages_not_sent=max(0, planned-count_sent) if planned is not None else None,
                  messages_sent=count_sent, messages_received=raw_count, expected_deliveries=expected,
                  unique_deliveries=count_original, final_unique_deliveries=count_final,
                  missing_count=expected-count_original, final_missing_count=expected-count_final,
                  packet_loss=loss(expected, count_original), final_packet_loss=loss(expected, count_final),
                  duplicate_count=duplicates, out_of_order_count=out_order, corrupted_count=corrupted,
                  length_error_count=length_errors, checksum_error_count=checksum_errors,
                  unparseable_count=unparseable, invalid_metadata_count=invalid_metadata,
                  foreign_run_count=foreign, publish_failed_count=failed,
                  late_native_deliveries=late, application_recovered=0,
                  recovery_probe_deliveries=recovered_probes, recovery_first_receive_ns=recovery_first,
                  throughput_mbps=throughput(count_original, cfg['payload_size_bytes'], delivery_window),
                  offered_throughput_mbps=throughput(count_sent, cfg['payload_size_bytes'], send_window),
                  send_window_seconds=send_window, delivery_window_seconds=delivery_window,
                  first_successful_send_ns=first, last_successful_send_ns=last,
                  last_counted_receive_ns=last_receive, delivery_matrix=matrix,
                  complete_delivery=expected > 0 and count_original == expected and corrupted == 0)
    result['statistics'] = dict(latency_ms=describe([lat for link in links for _, lat in link]),
        jitter_ms=describe([abs(b[1]-a[1]) for link in links for a,b in zip(sorted(link), sorted(link)[1:])]))
    result['per_publisher'] = []
    for pub in range(cfg['publisher_count']):
        rows = [r for r in matrix if r['publisher_id'] == pub]
        timestamps = list(sent[pub].values())
        window = (max(timestamps)-min(timestamps))/1e9 if timestamps else None
        result['per_publisher'].append(dict(publisher_id=pub, messages_sent=len(timestamps),
            unique_deliveries=sum(r['unique_deliveries'] for r in rows),
            offered_throughput_mbps=throughput(len(timestamps), cfg['payload_size_bytes'], window)))
    result['per_subscriber'] = []
    for sub in range(cfg['subscriber_count']):
        rows = [r for r in matrix if r['subscriber_id'] == sub]
        selected = [links[i] for i, r in enumerate(matrix) if r['subscriber_id'] == sub]
        received = sum(r['unique_deliveries'] for r in rows)
        sub_last = max((sent[r['publisher_id']][seq] + latency * 1e6
                        for r, link in zip(matrix, links) if r['subscriber_id'] == sub
                        for seq, latency in link), default=None)
        window = (sub_last-first)/1e9 if sub_last and first is not None else None
        result['per_subscriber'].append(dict(subscriber_id=sub, unique_deliveries=received,
            missing_count=count_sent-received, packet_loss=loss(count_sent, received),
            duplicate_count=sum(r['duplicate_count'] for r in rows),
            out_of_order_count=sum(r['out_of_order_count'] for r in rows),
            throughput_mbps=throughput(received, cfg['payload_size_bytes'], window),
            **latency_metrics(selected)))
    total_window = max(0, (drain_ns-formal_ns)/1e9)
    time_windows = []
    import math
    for bucket in range(math.ceil(total_window/cfg['metric_window_seconds'])):
        rows = windows[bucket]
        grouped = defaultdict(list)
        for p, s, seq, lat in rows:
            grouped[p, s].append((seq, lat))
        begin = bucket*cfg['metric_window_seconds']
        end = min(total_window, begin+cfg['metric_window_seconds'])
        time_windows.append(dict(start_offset_seconds=begin, end_offset_seconds=end,
            unique_deliveries=len(rows), throughput_mbps=throughput(len(rows), cfg['payload_size_bytes'], end-begin),
            **latency_metrics(list(grouped.values()))))
        window_sent = sum(begin <= (stamp-formal_ns)/1e9 < end for stamp in all_sends)
        window_expected = window_sent*cfg['subscriber_count']
        time_windows[-1].update(messages_sent=window_sent, expected_deliveries=window_expected,
            source_cohort_unique_deliveries=len(window_original_keys[bucket]),
            source_cohort_missing_count=window_expected-len(window_original_keys[bucket]),
            source_cohort_packet_loss=loss(window_expected,len(window_original_keys[bucket])))
    result['time_windows'] = time_windows
    probe_count = sum(len(messages) for messages in probe_sent.values())
    result['recovery_phase'] = dict(messages_sent=probe_count,
        expected_deliveries=probe_count*cfg['subscriber_count'], unique_deliveries=len(probe_seen),
        missing_count=probe_count*cfg['subscriber_count']-len(probe_seen),
        packet_loss=loss(probe_count*cfg['subscriber_count'],len(probe_seen)),
        duplicate_count=probe_duplicates,out_of_order_count=probe_out_of_order,corrupted_count=probe_corrupted,
        delivery_matrix=[dict(publisher_id=p,subscriber_id=s,unique_deliveries=probe_matrix[p,s])
                         for p in range(cfg['publisher_count']) for s in range(cfg['subscriber_count'])])
    return result


def group_results(runs):
    # Full actual comparison key, including fields beyond the minimum report grouping.
    keys = ['module_name', 'module_version', 'scenario_name', 'payload_size_bytes', 'publish_rate_hz',
            'publisher_count', 'subscriber_count', 'transport_mode', 'qos_profile', 'network_profile',
            'duration_seconds', 'message_count', 'warmup_seconds', 'drain_seconds', 'network_delay_ms',
            'network_jitter_ms', 'network_loss_rate', 'case', 'random_seed', 'metric_window_seconds',
            'max_inflight', 'metric_definition_version', 'benchmark_profile', 'suite_kind']
    groups = defaultdict(list)
    for run in runs:
        config = run['configuration']
        conditions = {k: run.get(k, config.get(k)) for k in keys}
        conditions['environment_fingerprint'] = run['environment'].get('fingerprint')
        conditions['broker_version'] = run['environment'].get('broker_version')
        groups[json.dumps(conditions, sort_keys=True)].append(run)
    output = []
    for key, group in groups.items():
        valid = [r for r in group if r['status'] == 'completed']
        output.append(dict(conditions=json.loads(key), sample_level='run_level', count=len(group),
            valid_repeats=len(valid), failed_count=sum(r['status']=='error' for r in group),
            timeout_count=sum(r['status']=='timeout' for r in group),
            unsupported_count=sum(r['status'] in ('unsupported', 'not_tested') for r in group),
            expected_negative_count=sum(bool(r.get('expected_negative_outcome')) for r in group),
            run_ids=[r['run_id'] for r in group],
            statistics={metric: describe([r.get(metric) for r in valid]) for metric in METRICS}))
    return output
