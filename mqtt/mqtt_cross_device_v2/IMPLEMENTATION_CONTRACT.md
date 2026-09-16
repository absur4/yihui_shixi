# Internal interface contract

This new directory is independent of the original MQTT delivery. Python 3.11+.
Config uses JSON. CLI module is `python -m mqtt_cd`.

## Agent HTTP protocol

All requests use Authorization: Bearer <MQTT_CD_TOKEN>. No secrets in saved specs.
GET /health -> node_id, hostname, pid, version, capabilities, dependencies.
POST /clock {} -> receive_ns, send_ns (node perf_counter_ns), node_id.
POST /prepare {run_id, role:sender|receiver, case:validated case, broker_host, broker_port}
 -> prepared state; asynchronously starts managed broker on sender and one local worker
 per endpoint; controller polls GET /status/<uuid> until ready.
POST /arm/<uuid> {start_ns:int} -> armed; timestamp is in that node's local monotonic clock.
GET /status/<uuid> -> state:preparing|ready|armed|running|completed|error|cancelled,
 workers, errors, run_id. Workers complete by start_ns+duration+drain+1s.
POST /cancel/<uuid> {} -> cancel stop file, reap managed children.
GET /artifact/<uuid> -> ZIP of completed run folder, flattened folder members,
 excluding broker auth/config secrets. Never include raw environment variables.
POST /release/<uuid> -> ensure children stopped; retain archived files.
Only one active run per agent. No arbitrary executable/shell/path provided through HTTP.
Agent broker path is configured locally via --broker-executable. On sender default managed
broker uses Mosquitto password file with user benchmark and MQTT_CD_TOKEN as password.
mosquitto_passwd can be a sibling executable or locally configured. If unavailable, the
standard-library fallback writes Mosquitto PBKDF2-HMAC-SHA512 format with a random salt.
Worker inherits token; private broker files are excluded from artifacts and removed on cleanup.
Broker binds agent --broker-bind (default 0.0.0.0); clients use broker_host supplied by controller.
Agent cancellation lease: 30s without authenticated current-run controller communication;
clock/status requests from authenticated controller may refresh lease. Shutdown cleans all children.

## On-node run files

spec.json: {run_id, role, case, broker_host, broker_port, node_id}
arm.json: {start_ns}; cancel file; worker-<role>-<index>.ready.json and .summary.json.
publisher-0.csv columns: sequence,send_ns,return_ns,accepted,rc,phase,payload_bytes,api_ms,api_called.
api_called=0 records rejection by the local bounded queue before calling publish; it does not
increase N_attempt. api_called=1 records a real API call, including API rejection.
subscriber-<index>.csv: publisher_id,sequence,send_ns,receive_ns,phase,validation,payload_bytes,wire_bytes.
phase 0 warmup, 1 formal; header 64 bytes network order !4sBBH16sIQQII12s.
Summary includes errors/status, protocol_completed, pending_peak, queue_rejected, max pending bytes,
discarded/skipped sends and start/end timestamps. No secret values.
resources.jsonl: {time_ns,roles:{publisher|subscriber|infrastructure:{cpu_percent,memory_mb}},
host_cpu_percent,logical_cpu_count,rss_total_mb}. All endpoint PIDs grouped by role.
node.json: physical node_id, hostname, OS, python, logical_cpu_count, total_memory_bytes,
role, broker_version, dependencies, source_sha256, capabilities and run errors.
agent_status.json is the final state; stage, failure_origin and failed_at_ns distinguish DUT
failures from configuration, controller, measurement and cleanup failures.
The agent caches its source hash on startup. Code updates require agent restart.

## Controller output and analysis

Each result folder: spec.json (flat validated case plus run_id, nodes sender/receiver IDs,
clock settings), clocks.json list of records {node_id,t1,t2,t3,t4}, nodes/A and nodes/B with
retrieved node artifacts, result.json, report.html. Top-level suite has result.json, report.html,
schedule.json, experiment_manifest.json and per-run directories. Node IDs are exactly A,B. Original local perf counters
remain in raw data. Controller common start time is `reference_start_ns` in spec.
controller.json preserves errors/cancellation, final observed states and control_json_payload_bytes.
These byte counts cover control JSON bodies only, not interface/TCP/HTTP header bytes.
comparison_fingerprint includes physical-node addresses, environment, broker port, clock settings,
cooldown and planning seed. reference_runs preserves transitive earlier evidence; the summary
uses earlier and current evidence together. Conflicting duplicate run identities are rejected.
Clock config: {model_validated:false, drift_bound_ppm:100, interval_s:5,
latency_limit_ms:1, window_limit_ms:20}. Unvalidated bound is an assumption, not accuracy proof.
Strict capacity/latency verdicts require validated model; raw counts/local receiver throughput
remain available. CD4 manual exploratory rates supported when validated references absent.
Validated configuration requires model_validation_reference describing the independent evidence.
Source accepted counters cover formal submissions; receiver received counters cover all phases.
Both are checked against raw CSV before counts are treated as complete.

analysis.py exports analyze_run(run_dir:Path,spec:dict,clocks:list)->dict.
clock.py exports estimate(samples:list)->dict including offset_ns,rtt_ns,uncertainty_ns;
each sample t1/t4 controller, t2/t3 node. Offset=node-reference.
Resource and latency analysis imports only standard library; unavailable values null with reason.
planner.py exports build_scan(scenarios, directions, repeats=5, seed=20260916)->list[case],
classify_conditions(results)->list[dict], derive_cd4(results, references=None, manual_rates=None, seed=20260916)
 -> {cases:list, skipped:list}. References include other middleware/direction capacities.
report.py exports write_report(path:Path,result:dict)->None, writes escaped self-contained HTML.
