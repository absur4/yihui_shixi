"""MQTT unified benchmark 2.0; metric/schema version 1.0."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import platform
import queue
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import yaml

from mqtt_metrics import describe, resource_window_metrics
from mqtt_results import METRICS, analyze, group_results
from mqtt_worker import endpoint
from mqtt_signals import Signal

VERSION = '2.0.0'
ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
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


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def normalize(config):
    data = dict(config)
    for old, new in ALIASES.items():
        if old in data:
            if new in data and data[new] != data[old]:
                raise ValueError(f'Conflicting fields: {old} / {new}')
            data[new] = data.pop(old)
    c = dict(DEFAULTS, **data)
    if str(c['module_name']).lower() != 'mqtt':
        raise ValueError('This adapter only implements MQTT')
    c['module_name'], c['module_version'] = 'mqtt', VERSION
    if c['scenario_name'] not in [f'S{i:02}' for i in range(1, 13)]:
        raise ValueError('scenario_name must be S01..S12')
    for key in ['message_count', 'payload_size_bytes', 'publisher_count', 'subscriber_count', 'repeats', 'random_seed', 'max_inflight']:
        if isinstance(c[key], bool) or not isinstance(c[key], int) or c[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    if min(c['publisher_count'], c['subscriber_count'], c['repeats'], c['max_inflight']) < 1:
        raise ValueError('counts, repeats and max_inflight must be positive')
    for key in ['duration_seconds', 'publish_rate_hz', 'warmup_seconds', 'drain_seconds',
                'network_delay_ms', 'network_jitter_ms', 'network_loss_rate', 'timeout_seconds',
                'connect_timeout_seconds', 'publish_timeout_seconds', 'metric_window_seconds']:
        if not isinstance(c[key], (int, float)) or isinstance(c[key], bool) or not 0 <= c[key] < float('inf'):
            raise ValueError(f'{key} must be a finite nonnegative number')
    if c['network_loss_rate'] > 1 or c['metric_window_seconds'] == 0 or c['timeout_seconds'] == 0:
        raise ValueError('Invalid loss rate/window/timeout')
    if not c['message_count'] and not c['duration_seconds']:
        raise ValueError('Set a positive duration_seconds or message_count')
    qos = {'qos0': 0, 'best_effort': 0, 'qos1': 1, 'reliable': 1, 'qos2': 2}
    if c['qos_profile'] not in qos:
        raise ValueError('qos_profile must be qos0/qos1/qos2/best_effort/reliable')
    c['qos'] = qos[c['qos_profile']]
    c['qos_semantics'] = ['at_most_once', 'at_least_once', 'exactly_once'][c['qos']]
    c['stop_rule'] = 'message_count_per_publisher' if c['message_count'] else 'duration_seconds'
    c['rate_scope'] = 'per_publisher'
    return c


def environment(broker_version):
    data = dict(os=platform.platform(), machine=platform.machine(), processor=platform.processor(),
                hostname=socket.gethostname(), logical_cpu_count=psutil.cpu_count(),
                physical_cpu_count=psutil.cpu_count(logical=False),
                total_memory_bytes=psutil.virtual_memory().total,
                python_version=platform.python_version(), broker_version=broker_version,
                clock='time.perf_counter_ns; same-host Windows QueryPerformanceCounter',
                one_way_latency_clock_synchronized=True, adapter_version=VERSION)
    import paho.mqtt
    data['paho_version'] = paho.mqtt.__version__
    data['fingerprint'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data


class Broker:
    def __init__(self, cfg, folder):
        exe = Path(cfg['broker_executable'])
        self.exe = exe if exe.is_absolute() else ROOT / exe
        self.folder = Path(folder)
        self.process = None
        self.log = None
        self.port = cfg['port']
        if not self.port:
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                self.port = s.getsockname()[1]
        self.conf = self.folder / 'mosquitto.conf'
        self.conf.write_text(f'listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n'
                             'max_packet_size 16777216\nmax_queued_messages 100000\n'
                             'log_type error\nlog_type warning\n', encoding='ascii')
        self.version = subprocess.run([str(self.exe), '-h'], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        self.restarts = 0

    def start(self, timeout=10):
        self.log = open(self.folder / 'broker.log', 'ab')
        self.process = subprocess.Popen([str(self.exe), '-c', str(self.conf)], stdout=self.log,
                                        stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.perf_counter()+timeout
        while time.perf_counter() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('Managed broker exited; see broker.log (possibly port occupied)')
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.2):
                    return
            except OSError:
                time.sleep(.02)
        raise TimeoutError('Broker listener timeout')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(3)
        if self.log:
            self.log.close()


class Resources:
    def __init__(self, processes, folder, infrastructure=()):
        self.processes = [(role, psutil.Process(pid)) for role, pid in processes]
        self.start_ns = time.perf_counter_ns()
        self.previous_time = self.start_ns
        self.initial, self.previous, self.peaks = {}, {}, {}
        for role, p in self.processes:
            cpu = p.cpu_times()
            self.initial[p.pid] = self.previous[p.pid] = cpu.user+cpu.system
            self.peaks[p.pid] = 0
        self.peak = 0
        self.samples = []
        self.infrastructure = []
        for role, pid in infrastructure:
            p = psutil.Process(pid)
            c = p.cpu_times()
            self.infrastructure.append(dict(role=role, pid=pid, process=p, initial_cpu=c.user+c.system, peak=0))
        self.file = open(Path(folder)/'resources.jsonl', 'w', encoding='utf-8')

    def sample(self):
        now, total_rss, delta = time.perf_counter_ns(), 0, 0
        details = []
        for role, p in self.processes:
            cpu = p.cpu_times()
            value, rss = cpu.user+cpu.system, p.memory_info().rss
            delta += value-self.previous[p.pid]
            self.previous[p.pid] = value
            total_rss += rss
            self.peaks[p.pid] = max(self.peaks[p.pid], rss)
            details.append(dict(role=role, pid=p.pid, cpu_time_seconds=value, rss_bytes=rss))
        self.peak = max(self.peak, total_rss)
        row = dict(timestamp_ns=now, cpu_percent=delta/((now-self.previous_time)/1e9)*100,
                   memory_mb=total_rss/1e6, processes=details)
        row['infrastructure_processes'] = []
        for item in self.infrastructure:
            c = item['process'].cpu_times()
            rss = item['process'].memory_info().rss
            item['last_cpu'] = c.user+c.system
            item['peak'] = max(item['peak'], rss)
            row['infrastructure_processes'].append(dict(role=item['role'],pid=item['pid'],rss_bytes=rss,cpu_time_seconds=item['last_cpu']))
        self.previous_time = now
        self.samples.append(row)
        self.file.write(json.dumps(row)+'\n')

    def finish(self):
        self.sample()
        elapsed = (self.previous_time-self.start_ns)/1e9
        roles = [dict(role=role, pid=p.pid, rss_peak_mb=self.peaks[p.pid]/1e6,
                      cpu_time_delta_seconds=self.previous[p.pid]-self.initial[p.pid])
                 for role, p in self.processes]
        cpu = sum(r['cpu_time_delta_seconds'] for r in roles)/elapsed*100
        self.file.close()
        return dict(cpu_percent=cpu, cpu_machine_percent=cpu/psutil.cpu_count(), memory_mb=self.peak/1e6,
                    resource_window_seconds=elapsed, resource_start_ns=self.start_ns,
                    resource_end_ns=self.previous_time, resource_processes=roles,
                    test_infrastructure_resources=[dict(role=i['role'],pid=i['pid'],rss_peak_mb=i['peak']/1e6,
                        cpu_percent=(i['last_cpu']-i['initial_cpu'])/elapsed*100) for i in self.infrastructure],
                    resource_scope='Dedicated publisher/subscriber processes and managed Mosquitto broker; excludes harness/report',
                    rss_note='Sum of RSS may count shared pages multiple times')


def base_result(cfg, run_id, repeat, env):
    r = dict(schema_version='1.0', metric_definition_version='1.0', module_name='mqtt',
             module_version=VERSION, run_id=run_id, repeat=repeat, status='error',
             test_start_time=utc(), test_end_time=None, configuration=cfg, environment=env,
             measurement_window={}, limitations=[], errors=[], sample_level='sample_level')
    for k in ['scenario_name', 'payload_size_bytes', 'publish_rate_hz', 'publisher_count', 'subscriber_count',
              'transport_mode', 'qos_profile', 'network_profile', 'benchmark_profile', 'suite_kind']:
        r[k] = cfg[k]
    for k in METRICS + ['messages_planned', 'messages_sent', 'messages_received', 'expected_deliveries',
                        'duplicate_count', 'out_of_order_count', 'corrupted_count', 'application_recovered',
                        'late_native_deliveries', 'actual_payload_size_bytes', 'metadata_size_bytes',
                        'wire_message_size_bytes', 'messages_not_sent', 'latency_sample_count']:
        r[k] = None
    r['configured_publish_rate_hz'] = cfg['publish_rate_hz']
    r['injected_network_loss_rate'] = None
    r['discovery_supported'] = True
    r['discovery_ready_condition'] = 'All publisher CONNACKs and all subscriber successful SUBACKs'
    r['startup_ready_condition'] = 'Harness launches managed broker and endpoint processes; broker listening, all CONNACK/SUBACK ready'
    return r


def execute(cfg, output, repeat=1, purpose='measurement'):
    cfg = copy.deepcopy(cfg)
    run_id = str(uuid.uuid4())
    folder = Path(output)/'runs'/run_id
    folder.mkdir(parents=True)
    env = environment(None)
    result = base_result(cfg, run_id, repeat, env)
    result['purpose'] = purpose
    result['raw_files'] = dict(directory=f'runs/{run_id}',
        sent='publisher-N.csv: sequence_id,send_ns,ack_ns,phase,success,mqtt_rc',
        received='subscriber-N.csv: publisher_id,sequence_id,send_ns,receive_ns,phase,validation,wire_bytes,recovery_stage',
        phases={'0': 'warmup', '1': 'original', '2': 'independent_recovery_probe'})
    start_utc, harness_ns = result['test_start_time'], time.perf_counter_ns()
    broker, meter = None, None
    relay = relay_stop = relay_pause = relay_generation = None
    ctx = mp.get_context('spawn')
    ready, start, stop, recovery = ctx.Queue(), Signal(ctx), Signal(ctx), Signal(ctx)
    start_ns = ctx.RawValue('q', 0)
    connect_gate = Signal(ctx) if cfg['case'] == 'simultaneous_discovery' else None
    processes = {}
    formal_ns = drain_ns = None
    marks = result['measurement_window']

    def spawn(role, index, restart=False):
        p = ctx.Process(target=endpoint, args=(role, index, cfg, run_id, str(folder), ready,
                        start, stop, recovery, start_ns, restart, connect_gate), name=f'{role}-{index}')
        p.start()
        processes[role, index] = p
        return p

    def check_alive():
        if broker.process.poll() is not None:
            raise RuntimeError('Broker crashed during measurement')
        for key, p in processes.items():
            if p.exitcode is not None:
                raise RuntimeError(f'{key} exited during measurement: {p.exitcode}')
        if time.perf_counter_ns()-harness_ns > cfg['timeout_seconds']*1e9:
            raise TimeoutError('Scenario timeout')

    def wait_period(seconds, sampling=False):
        end = time.perf_counter()+seconds
        while time.perf_counter() < end:
            check_alive()
            if sampling:
                meter.sample()
            time.sleep(min(.1, max(0, end-time.perf_counter())))

    try:
        if cfg['transport_mode'].lower() != 'tcp':
            result['status'] = 'unsupported'
            result['limitations'].append('This adapter supports MQTT 3.1.1 over local TCP only')
            return result
        if cfg['host'] not in ('127.0.0.1', 'localhost'):
            result['status'] = 'unsupported'
            result['limitations'].append('Cross-host one-way timing and remote resource metering require synchronized agents; not implemented')
            return result
        if cfg['payload_size_bytes'] > 4*1024*1024:
            result['status'] = 'unsupported'
            result['limitations'].append('Validated payload limit is 4 MiB; larger sizes are not supported by this adapter')
            return result
        if cfg['network_loss_rate'] or cfg['network_delay_ms'] or cfg['network_jitter_ms']:
            result['status'] = 'not_tested'
            result['limitations'].append('No verified packet-layer injector is installed. TCP stream/application drops do not emulate network packet loss. Requested conditions were NOT injected.')
            return result
        broker = Broker(cfg, folder)
        cfg['port'] = broker.port
        cfg['host'] = '127.0.0.1'
        cfg['broker_config'] = broker.conf.read_text(encoding='ascii')
        result['environment'] = environment(broker.version)
        if cfg['startup_mode'] == 'hot':
            broker.start()
            time.sleep(.25)
            harness_ns = time.perf_counter_ns()
            result['test_start_time'] = utc()
            result['startup_ready_condition'] += '; hot condition: broker already running before harness timing'
        else:
            broker.start()
        if cfg['scenario_name'] == 'S11' and cfg['case'] in ('network_interruption', 'remote_close'):
            from mqtt_faults import relay_worker
            relay_ready, relay_stop, relay_pause, relay_generation = ctx.Queue(), Signal(ctx), Signal(ctx), ctx.RawValue('i', 0)
            relay = ctx.Process(target=relay_worker, args=(broker.port, relay_ready, relay_stop, relay_pause, relay_generation))
            relay.start()
            cfg['port'] = relay_ready.get(timeout=10)
            cfg['fault_relay'] = dict(upstream_port=broker.port, listener_port=cfg['port'], pid=relay.pid)
            result['limitations'].append('S11 uses a TCP relay for transport outage/remote close, not packet-loss emulation; relay baseline has extra forwarding overhead')
        if cfg['scenario_name'] == 'S10' and cfg['case'] == 'discovery_failure':
            broker.stop()
        for sub in range(cfg['subscriber_count']):
            spawn('subscriber', sub)
        for pub in range(cfg['publisher_count']):
            spawn('publisher', pub)
        endpoints = []
        initialized = set()
        timeout = cfg['connect_timeout_seconds']
        if cfg['scenario_name'] == 'S10' and cfg['case'] == 'startup_timeout':
            timeout = .0001  # real deadline expires while spawned endpoints initialize
        deadline = min(harness_ns/1e9+cfg['timeout_seconds'], time.perf_counter()+timeout)
        while len(endpoints) < len(processes):
            if time.perf_counter() >= deadline:
                raise TimeoutError('Endpoint startup/discovery deadline expired')
            try:
                msg = ready.get(timeout=min(.1, max(.00001, deadline-time.perf_counter())))
            except queue.Empty:
                if cfg['case'] != 'discovery_failure':
                    check_alive()
                continue
            if 'error' in msg:
                raise RuntimeError(msg['error'])
            if msg.get('event') == 'initialized':
                initialized.add((msg['role'], msg['index']))
                if len(initialized) == len(processes):
                    connect_gate.set()
                continue
            endpoints.append(msg)
        all_ready_ns = max(m['ready_ns'] for m in endpoints)
        result['startup_time_ms'] = (all_ready_ns-harness_ns)/1e6
        result['discovery_time_ms'] = (all_ready_ns-min(m['conn_start_ns'] for m in endpoints))/1e6
        result['endpoint_readiness'] = endpoints
        result['injected_network_loss_rate'] = 0
        result['network_injection'] = dict(method='none', observed_network_packet_loss_rate=None,
                                          requested_profile=cfg['network_profile'])
        marks['warmup_start'] = utc()
        start_ns.value = time.perf_counter_ns()+int(cfg['warmup_seconds']*1e9)
        formal_ns = start_ns.value
        start.set()
        wait_period(max(0, (formal_ns-time.perf_counter_ns())/1e9))
        marks['measurement_start'] = utc()
        marks['measurement_start_ns'] = formal_ns
        meter = Resources([('broker', broker.process.pid)] +
                          [(f'{role}-{i}', p.pid) for (role, i), p in processes.items()], folder,
                          [('tcp_fault_relay', relay.pid)] if relay else [])
        done = set()
        while len(done) < cfg['publisher_count']:
            check_alive()
            meter.sample()
            try:
                msg = ready.get(timeout=.05)
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                if msg.get('event') == 'sent':
                    done.add(msg['index'])
            except queue.Empty:
                pass
        marks['send_end'] = utc()
        marks['send_end_ns'] = time.perf_counter_ns()
        wait_period(cfg['drain_seconds'], sampling=True)
        drain_ns = time.perf_counter_ns()
        marks['drain_end'], marks['drain_end_ns'] = utc(), drain_ns
        result.update(meter.finish())
        if cfg['scenario_name'] == 'S11':
            fault = dict(type=cfg['case'], fault_timestamp_ns=time.perf_counter_ns(), fault_time=utc(),
                         original_measurement_preserved=True)
            result['fault'] = fault
            victim = None
            recovery.set()
            if cfg['case'] == 'broker_restart':
                broker.stop()
                broker.restarts += 1
            elif cfg['case'] in ('publisher_restart', 'subscriber_restart'):
                role = cfg['case'].replace('_restart', '')
                victim = processes[role, 0]
                victim.terminate()
                victim.join(3)
                fault['stopped_pid'] = victim.pid
            elif cfg['case'] == 'network_interruption':
                relay_pause.set()
                # Send probes while forwarding is stopped to measure restoration of actual delivery.
                recovery.set()
                fault['injection_method'] = 'TCP relay suspends forwarding in both directions; kernel buffers remain intact'
            elif cfg['case'] == 'remote_close':
                relay_generation.value += 1
                fault['injection_method'] = 'Remote TCP relay closes existing sockets with FIN; broker stays running'
            else:
                raise ValueError('Unknown S11 fault case')
            fault['fault_applied_ns'] = time.perf_counter_ns()
            time.sleep(cfg['fault_duration_seconds'])
            fault['reconnect_start_ns'] = time.perf_counter_ns()
            cfg['recovery_eligible_after_ns'] = fault['reconnect_start_ns']
            if cfg['case'] == 'broker_restart':
                broker.start()
            elif cfg['case'] == 'network_interruption':
                relay_pause.clear()
            elif cfg['case'] == 'remote_close':
                pass
            else:
                p = spawn(role, 0, restart=True)
                fault['replacement_pid'] = p.pid
            recovery.set()
            # This is an independent recovery phase. Its probes cannot enter original latency/throughput.
            end = min(harness_ns/1e9+cfg['timeout_seconds'], time.perf_counter()+cfg['recovery_timeout_seconds'])
            while time.perf_counter() < end:
                if all(p.is_alive() for p in processes.values()):
                    # Give probes enough time for reconnect/backoff and deliveries.
                    time.sleep(.1)
                else:
                    raise RuntimeError('Endpoint crashed during recovery')
            marks['recovery_end'] = utc()
        result['broker'] = dict(version=broker.version, pid=broker.process.pid,
                                restart_count=broker.restarts, configuration=cfg['broker_config'])
        result['status'] = 'completed'
    except TimeoutError as exc:
        result['status'] = 'timeout'
        result['errors'].append(str(exc))
    except KeyboardInterrupt:
        result['status'] = 'cancelled'
        result['errors'].append('Interrupted by user')
    except Exception as exc:
        result['status'] = 'error'
        result['errors'].append(repr(exc))
    finally:
        stop.set()
        for p in processes.values():
            p.join(3)
            if p.is_alive():
                p.terminate()
                p.join(3)
                result['errors'].append(f'Endpoint {p.name} did not stop gracefully')
                if result['status'] == 'completed':
                    result['status'] = 'error'
        if meter and not meter.file.closed:
            meter.file.close()
        if broker:
            broker.stop()
        if relay:
            relay_stop.set()
            relay.join(3)
            if relay.is_alive():
                relay.terminate()
                relay.join(3)
        if formal_ns and drain_ns:
            try:
                result.update(analyze(folder, cfg, formal_ns, drain_ns))
                for window in result['time_windows']:
                    a = formal_ns + window['start_offset_seconds']*1e9
                    b = formal_ns + window['end_offset_seconds']*1e9
                    window.update(resource_window_metrics(meter.samples, meter.start_ns, a, b))
                measured_windows = [w for w in result['time_windows'] if w['latency_ms'] is not None]
                result['long_run_trends'] = dict(
                    latency_last_minus_first_ms=measured_windows[-1]['latency_ms']-measured_windows[0]['latency_ms'] if measured_windows else None,
                    rss_last_minus_first_mb=meter.samples[-1]['memory_mb']-meter.samples[0]['memory_mb'] if meter.samples else None,
                    source_queue_policy='At most one synchronously awaited publish per publisher; no application backlog queue')
                result['endpoint_event_counts'] = dict(disconnect=0, error=0, reconnect=0)
                for file in folder.glob('*-events.jsonl'):
                    events = [json.loads(line) for line in file.read_text(encoding='utf-8').splitlines()]
                    for event in events:
                        stamp = event['timestamp_ns']
                        if formal_ns <= stamp <= drain_ns:
                            key = 'reconnect' if event['event']=='connack' else event['event']
                            if key in result['endpoint_event_counts']:
                                result['endpoint_event_counts'][key] += 1
                            for window in result['time_windows']:
                                if window['start_offset_seconds'] <= (stamp-formal_ns)/1e9 < window['end_offset_seconds']:
                                    window.setdefault('endpoint_events', []).append(dict(file=file.name, **event))
                if result['status'] == 'completed' and result['messages_sent'] == 0:
                    result['status'] = 'error'
                    result['errors'].append('No successful formal messages')
                if 'fault' in result:
                    recovered = result['recovery_first_receive_ns']
                    result['recovery_time_ms'] = (recovered-result['fault']['fault_timestamp_ns'])/1e6 if recovered else None
                    result['fault']['recovery_complete_ns'] = recovered
                    detected = []
                    for file in folder.glob('*-events.jsonl'):
                        for line in file.read_text(encoding='utf-8').splitlines():
                            e = json.loads(line)
                            if e['event'] == 'disconnect' and e['timestamp_ns'] >= result['fault']['fault_timestamp_ns']:
                                detected.append(e['timestamp_ns'])
                    result['fault']['endpoint_detection_ns'] = min(detected) if detected else None
                    result['fault']['controller_detection_ns'] = result['fault']['fault_applied_ns']
                    result['recovery_success'] = recovered is not None
                    if not recovered:
                        result['status'] = 'timeout'
                        result['errors'].append('No valid recovery probe delivered within recovery deadline')
                loss_value = result['packet_loss']
                achieved = result['achieved_publish_rate_hz']
                reasons = []
                if loss_value is not None and loss_value > cfg['saturation_loss_threshold']:
                    reasons.append('application_loss_threshold')
                if result['latency_p99_ms'] is not None and result['latency_p99_ms'] > cfg['saturation_latency_p99_ms']:
                    reasons.append('p99_threshold')
                if cfg['publish_rate_hz'] and achieved is not None and achieved < cfg['publish_rate_hz']*cfg['stable_rate_min_fraction']:
                    reasons.append('configured_rate_not_achieved')
                if result.get('cpu_percent') is not None and result['cpu_percent'] > cfg['saturation_cpu_percent']:
                    reasons.append('cpu_threshold')
                if result.get('memory_mb') is not None and result['memory_mb'] > cfg['saturation_memory_mb']:
                    reasons.append('memory_threshold')
                if result['publish_failed_count']:
                    reasons.append('publish_queue_or_ack_failure')
                p99_windows = [w['latency_p99_ms'] for w in measured_windows]
                if len(p99_windows) >= 3 and all(a < b for a,b in zip(p99_windows[-3:],p99_windows[-2:])) and p99_windows[-1] > p99_windows[-3]*1.5:
                    reasons.append('p99_growing_across_three_windows')
                result['saturation'] = dict(detected=bool(reasons), reasons=reasons,
                                            queue_policy='bounded synchronous protocol completion; no unbounded application queue')
                if cfg['require_stable_rate'] and reasons:
                    result['status'] = 'error'
                    result['errors'].append('Selected rate was unstable in measurement: '+', '.join(reasons))
            except Exception as exc:
                result['status'] = 'error'
                result['errors'].append('Raw analysis failed: '+repr(exc))
        if cfg['scenario_name'] == 'S10' and cfg['case'] in ('discovery_failure', 'startup_timeout'):
            expected = 'timeout' if cfg['case'] == 'startup_timeout' else 'error'
            result['expected_negative_outcome'] = result['status'] == expected
            result['expected_status'] = expected
        result['limitations'] += [
            'Local loopback benchmark; results do not predict physical-network performance.',
            'MQTT application message size is measured; total on-wire bytes including MQTT/TCP/IP headers are not measured (null).',
            'Endpoint CPU includes serialization, checksum and raw recording; harness/report CPU is excluded.',
            'Successful source send requires transport completion (QoS0) or MQTT protocol acknowledgement (QoS1/2).',
            'Publisher waits for each message completion; maximum stable rate is specific to this adapter and candidate grid.',
        ]
        result['configuration'] = cfg
        result['test_end_time'] = utc()
        schema_file = ROOT/'result.schema.json'
        if schema_file.exists():
            import jsonschema
            jsonschema.Draft202012Validator(json.loads(schema_file.read_text(encoding='utf-8')),
                format_checker=jsonschema.FormatChecker()).validate(result)
        write_json(folder/'result.json', result)
        ready.close()
    return result


def expand(config):
    common = {k: v for k, v in config.items() if k not in ('scenarios', 'description')}
    return [normalize(dict(common, **scenario)) for scenario in config.get('scenarios', [{}])]


def save_suite(output, runs, start, kind='measurement'):
    from mqtt_report import render
    suite = dict(schema_version='1.0', metric_definition_version='1.0', module_name='mqtt',
                 suite_start_time=start, suite_end_time=utc(), sample_level='suite_level',
                 suite_kind=kind, runs=runs)
    summaries = group_results(runs)
    write_json(Path(output)/'result.json', suite)
    write_json(Path(output)/'summary.json', dict(sample_level='run_level', groups=summaries))
    render(output, suite, summaries)


def main():
    parser = argparse.ArgumentParser(description='MQTT unified benchmark S01-S12')
    parser.add_argument('--config', default=str(ROOT/'config.yaml'))
    parser.add_argument('--output')
    parser.add_argument('--scenarios', help='Comma-separated S01,S02,...')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--validate', metavar='RESULT_DIR')
    parser.add_argument('--features', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.validate:
        from mqtt_validate import validate_output
        validate_output(Path(args.validate))
        return
    if args.features:
        from mqtt_features import run_features
        run_features(Path(args.output or ROOT/'outputs'/'features'))
        return
    config_path = Path(args.config).resolve()
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    scenarios = expand(config)
    if args.scenarios:
        scenarios = [s for s in scenarios if s['scenario_name'] in args.scenarios.split(',')]
    if args.list:
        print(json.dumps(scenarios, ensure_ascii=False, indent=2))
        return
    output = Path(args.output or config_path.parent/config.get('output_dir', 'outputs')).resolve()
    output.mkdir(parents=True, exist_ok=True)
    runs, suite_start = [], utc()
    if args.resume and (output/'result.json').exists():
        previous = json.loads((output/'result.json').read_text(encoding='utf-8'))
        runs, suite_start = previous['runs'], previous['suite_start_time']
    elif (output/'result.json').exists():
        raise FileExistsError('Output already contains results; use --resume or a new --output directory')
    stable_cache = {}
    for index, cfg in enumerate(scenarios):
        condition_id = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
        existing = [r for r in runs if r.get('condition_id') == condition_id]
        if all(any(r['repeat'] == repeat for r in existing) for repeat in range(1, cfg['repeats']+1)):
            continue
        calibration = []
        if cfg['rate_search']:
            cache_key = (cfg['payload_size_bytes'], cfg['publisher_count'], cfg['subscriber_count'], cfg['qos'])
            previous_calibration = next((r['rate_calibration'] for r in existing if 'rate_calibration' in r), None)
            if previous_calibration:
                stable_cache[cache_key] = (previous_calibration['selected_rate_hz'], previous_calibration['trials'])
            if cache_key not in stable_cache:
                selected = None
                for rate in cfg['stable_rate_candidates']:
                    trial = dict(cfg, duration_seconds=2, message_count=0, publish_rate_hz=rate,
                                 rate_search=False, repeats=1, require_stable_rate=False, warmup_seconds=.2)
                    print(f'CALIBRATION {cfg["scenario_name"]} payload={cfg["payload_size_bytes"]} rate={rate}', flush=True)
                    r = execute(trial, output/'calibration', purpose='rate_calibration')
                    calibration.append(dict(run_id=r['run_id'], rate=rate, status=r['status'],
                                            saturation=r.get('saturation'), achieved=r.get('achieved_publish_rate_hz')))
                    if r['status'] != 'completed' or r.get('saturation', {}).get('detected', True):
                        break
                    selected = rate
                stable_cache[cache_key] = (selected, calibration)
            selected, calibration = stable_cache[cache_key]
            if selected is None:
                # Preserve an explicit failed condition rather than invent a stable maximum.
                cfg = dict(cfg, publish_rate_hz=cfg['stable_rate_candidates'][0], require_stable_rate=True)
            else:
                cfg = dict(cfg, publish_rate_hz=selected, require_stable_rate=True)
        for repeat in range(1, cfg['repeats']+1):
            if any(r.get('condition_id') == condition_id and r['repeat'] == repeat for r in runs):
                continue
            print(f'RUN {index+1}/{len(scenarios)} {cfg["scenario_name"]}/{cfg["case"]} '
                  f'{cfg["payload_size_bytes"]}B {cfg["publish_rate_hz"]}Hz '
                  f'{cfg["publisher_count"]}P/{cfg["subscriber_count"]}S {repeat}/{cfg["repeats"]}', flush=True)
            r = execute(cfg, output, repeat)
            r['condition_id'] = condition_id
            if calibration:
                r['rate_calibration'] = dict(method='highest passing candidate before first failing candidate',
                                            trials=calibration, selected_rate_hz=cfg['publish_rate_hz'],
                                            exact_global_maximum_claimed=False)
            write_json(output/'runs'/r['run_id']/'result.json', r)
            runs.append(r)
            save_suite(output, runs, suite_start)
            print(f'  {r["status"]}: sent={r["messages_sent"]} loss={r["packet_loss"]} errors={r["errors"]}', flush=True)
            if r['status'] == 'cancelled':
                raise SystemExit(130)
    from mqtt_validate import validate_output
    validate_output(output)
    if any(r['status'] in ('error', 'timeout', 'cancelled') and not r.get('expected_negative_outcome') for r in runs):
        raise SystemExit(1)


if __name__ == '__main__':
    mp.freeze_support()
    main()
