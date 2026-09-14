from __future__ import annotations

import argparse
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import psutil

from .payload import crc32_text, make_payload_text, read_payload_text
from .rawio import (
    FLAG_CHECKSUM_OK,
    FLAG_DDS_VALID,
    FLAG_DECODE_OK,
    FLAG_LENGTH_OK,
    ReceiveRecord,
    ReceiveRecordWriter,
    SendRecord,
    SendRecordWriter,
)
from .scheduler import aggregate_slot, aggregate_target_ns
from .util import atomic_write_json, read_json, utc_now_iso, wait_until_ns

PROCESS_WALL_START_NS = time.perf_counter_ns()


class StatusTracker:
    def __init__(self, path: Path, role: str, endpoint_id: int) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "role": role,
            "endpoint_id": endpoint_id,
            "pid": os.getpid(),
            "state": "starting",
            "matched_count": 0,
            "participant_created_ns": None,
            "ready_ns": None,
            "matched_ns": None,
            "send_done_ns": None,
            "error": None,
        }
        self.write()

    def update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            atomic_write_json(self.path, self._state)

    def update_match(self, current_count: int, expected: int) -> None:
        with self._lock:
            self._state["matched_count"] = int(current_count)
            if current_count >= expected and self._state["matched_ns"] is None:
                self._state["matched_ns"] = time.perf_counter_ns()
                self._state["state"] = "matched"
            atomic_write_json(self.path, self._state)

    def write(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self._state)


def _process_cpu_seconds() -> float:
    cpu = psutil.Process(os.getpid()).cpu_times()
    return float(cpu.user + cpu.system)


def _wait_json(path: Path, deadline_ns: int) -> dict[str, Any]:
    while time.perf_counter_ns() < deadline_ns:
        if path.exists():
            try:
                return read_json(path)
            except (OSError, ValueError):
                pass
        time.sleep(0.005)
    raise TimeoutError(f"Timed out waiting for {path.name}")


def _duration(fastdds: Any, milliseconds: int) -> Any:
    milliseconds = max(0, int(milliseconds))
    return fastdds.Duration_t(milliseconds // 1000, (milliseconds % 1000) * 1_000_000)


def _configure_endpoint_qos(fastdds: Any, qos: Any, profile: dict[str, Any]) -> None:
    qos.reliability().kind = (
        fastdds.RELIABLE_RELIABILITY_QOS
        if profile["reliability"] == "reliable"
        else fastdds.BEST_EFFORT_RELIABILITY_QOS
    )
    max_blocking = int(profile["max_blocking_time_ms"])
    qos.reliability().max_blocking_time.seconds = max_blocking // 1000
    qos.reliability().max_blocking_time.nanosec = (max_blocking % 1000) * 1_000_000
    qos.durability().kind = (
        fastdds.TRANSIENT_LOCAL_DURABILITY_QOS
        if profile["durability"] == "transient_local"
        else fastdds.VOLATILE_DURABILITY_QOS
    )
    qos.history().kind = (
        fastdds.KEEP_ALL_HISTORY_QOS
        if profile["history_kind"] == "keep_all"
        else fastdds.KEEP_LAST_HISTORY_QOS
    )
    depth = int(profile["history_depth"])
    qos.history().depth = depth
    qos.resource_limits().max_samples = depth
    qos.resource_limits().max_instances = 1
    qos.resource_limits().max_samples_per_instance = depth
    qos.resource_limits().allocated_samples = min(depth, 32)
    qos.endpoint().history_memory_policy = fastdds.PREALLOCATED_WITH_REALLOC_MEMORY_MODE
    if profile.get("disable_data_sharing", True):
        qos.data_sharing().off()


def _make_participant_and_topic(
    fastdds: Any,
    message_module: Any,
    domain_id: int,
    profile_name: str,
    topic_name: str,
    status: StatusTracker,
) -> tuple[Any, Any, Any, Any, Any]:
    factory = fastdds.DomainParticipantFactory.get_instance()
    participant = factory.create_participant_with_profile(domain_id, profile_name)
    if participant is None:
        profile_path = os.environ.get("FASTDDS_DEFAULT_PROFILES_FILE")
        raise RuntimeError(
            f"Fast DDS could not create participant profile {profile_name!r}; "
            f"FASTDDS_DEFAULT_PROFILES_FILE={profile_path!r}"
        )
    status.update(
        state="participant_created", participant_created_ns=time.perf_counter_ns()
    )

    topic_data_type = message_module.BenchmarkMessagePubSubType()
    topic_data_type.set_name("BenchmarkMessage")
    type_support = fastdds.TypeSupport(topic_data_type)
    register_code = participant.register_type(type_support)
    if register_code != fastdds.RETCODE_OK:
        raise RuntimeError(f"register_type failed: {register_code}")
    topic = participant.create_topic(
        topic_name, topic_data_type.get_name(), fastdds.TOPIC_QOS_DEFAULT
    )
    if topic is None:
        raise RuntimeError("create_topic returned None")
    return factory, participant, topic, topic_data_type, type_support


def _cleanup(factory: Any, participant: Any) -> str | None:
    if participant is None:
        return None
    try:
        participant.delete_contained_entities()
        factory.delete_participant(participant)
        return None
    except Exception as exc:  # cleanup must not hide the primary result
        return f"{type(exc).__name__}: {exc}"


def run_publisher(args: argparse.Namespace, condition: dict[str, Any]) -> int:
    endpoint_id = int(args.endpoint_id)
    run_dir = Path(args.run_dir)
    status = StatusTracker(
        run_dir / "status" / f"publisher_{endpoint_id}.json",
        "publisher",
        endpoint_id,
    )
    raw_name = f"publisher_{endpoint_id}.send.bin"
    raw_writer = SendRecordWriter(run_dir / raw_name)
    result_path = run_dir / f"publisher_{endpoint_id}.result.json"
    factory = participant = None
    sent_failure_count = 0
    warmup_success_count = 0
    ack_result: str | None = None
    cleanup_error: str | None = None

    try:
        import BenchmarkMessage
        import fastdds

        factory, participant, topic, _topic_data_type, _type_support = (
            _make_participant_and_topic(
                fastdds,
                BenchmarkMessage,
                int(args.domain_id),
                args.profile_name,
                args.topic_name,
                status,
            )
        )

        matched = threading.Event()
        expected_matches = int(condition["subscriber_count"])

        class WriterListener(fastdds.DataWriterListener):
            def __init__(self) -> None:
                super().__init__()

            def on_publication_matched(self, writer: Any, info: Any) -> None:
                count = int(info.current_count)
                status.update_match(count, expected_matches)
                if count >= expected_matches:
                    matched.set()

        publisher = participant.create_publisher(fastdds.PUBLISHER_QOS_DEFAULT)
        if publisher is None:
            raise RuntimeError("create_publisher returned None")
        writer_qos = fastdds.DataWriterQos()
        publisher.get_default_datawriter_qos(writer_qos)
        _configure_endpoint_qos(fastdds, writer_qos, condition["qos"])
        writer_qos.publish_mode().kind = (
            fastdds.ASYNCHRONOUS_PUBLISH_MODE
            if condition["qos"]["publish_mode"] == "asynchronous"
            else fastdds.SYNCHRONOUS_PUBLISH_MODE
        )
        listener = WriterListener()
        writer = publisher.create_datawriter(topic, writer_qos, listener)
        if writer is None:
            raise RuntimeError("create_datawriter returned None")
        status.update(state="ready", ready_ns=time.perf_counter_ns())

        deadline_ns = time.perf_counter_ns() + int(
            float(condition["endpoint_timeout_seconds"]) * 1_000_000_000
        )
        if not matched.wait(timeout=float(condition["discovery_timeout_seconds"])):
            raise TimeoutError(
                f"Publisher {endpoint_id} did not match {expected_matches} subscriber(s)"
            )
        start_info = _wait_json(run_dir / "start.json", deadline_ns)

        payload = make_payload_text(
            int(condition["payload_size_bytes"]),
            int(condition["random_seed"]),
            condition["condition_id"],
        )
        checksum = crc32_text(payload)
        if checksum != int(condition["payload_checksum"]):
            raise RuntimeError("Publisher payload checksum differs from controller")
        data = BenchmarkMessage.BenchmarkMessage()
        data.publisher_id(endpoint_id)
        data.payload_length(len(payload))
        data.checksum(checksum)
        data.payload(payload)

        publisher_count = int(condition["publisher_count"])
        aggregate_rate = float(condition["publish_rate_hz"])
        spin_threshold_us = int(condition["spin_threshold_us"])
        warmup_start_ns = int(start_info["warmup_start_ns"])
        formal_start_ns = int(start_info["formal_start_ns"])
        wait_until_ns(warmup_start_ns, spin_threshold_us)

        warmup_rate = (
            aggregate_rate
            if aggregate_rate > 0
            else float(condition["warmup_unlimited_rate_hz"])
        )
        warmup_sequence = 0
        while True:
            global_slot = aggregate_slot(
                warmup_sequence, endpoint_id, publisher_count
            )
            target_ns = aggregate_target_ns(
                warmup_start_ns, global_slot, warmup_rate
            )
            if target_ns >= formal_start_ns:
                break
            wait_until_ns(target_ns, spin_threshold_us)
            data.phase(0)
            data.sequence_number(warmup_sequence)
            data.send_timestamp_ns(time.perf_counter_ns())
            if writer.write(data) == fastdds.RETCODE_OK:
                warmup_success_count += 1
            warmup_sequence += 1

        wait_until_ns(formal_start_ns, spin_threshold_us)
        data.phase(1)
        message_count = int(condition["message_count"])
        duration_ns = int(float(condition["duration_seconds"]) * 1_000_000_000)
        formal_end_ns = formal_start_ns + duration_ns
        sequence = 0
        while True:
            global_slot = aggregate_slot(sequence, endpoint_id, publisher_count)
            if message_count > 0 and global_slot >= message_count:
                break
            if aggregate_rate > 0:
                target_ns = aggregate_target_ns(
                    formal_start_ns, global_slot, aggregate_rate
                )
                if message_count == 0 and target_ns >= formal_end_ns:
                    break
                wait_until_ns(target_ns, spin_threshold_us)
            elif message_count == 0 and time.perf_counter_ns() >= formal_end_ns:
                break

            send_timestamp_ns = time.perf_counter_ns()
            data.sequence_number(sequence)
            data.send_timestamp_ns(send_timestamp_ns)
            return_code = writer.write(data)
            if return_code == fastdds.RETCODE_OK:
                raw_writer.append(
                    SendRecord(
                        endpoint_id,
                        sequence,
                        send_timestamp_ns,
                        len(payload),
                        checksum,
                    )
                )
            else:
                sent_failure_count += 1
            sequence += 1

        raw_count = raw_writer.snapshot_count()
        send_done_ns = time.perf_counter_ns()
        status.update(
            state="send_done",
            send_done_ns=send_done_ns,
            sent_success_count=raw_count,
            send_failure_count=sent_failure_count,
        )
        if (
            condition["qos"]["reliability"] == "reliable"
            and condition["qos"].get("wait_for_acknowledgments", True)
        ):
            ack_code = publisher.wait_for_acknowledgments(
                _duration(fastdds, int(float(condition["drain_seconds"]) * 1000))
            )
            ack_result = str(ack_code)

        # Keep the writer and its transport alive through the common drain
        # boundary. This is essential for asynchronous/best-effort profiles,
        # where destroying the participant immediately could discard queued
        # samples and turn teardown into an artificial loss source.
        _wait_json(run_dir / "stop.json", deadline_ns)

        raw_count = raw_writer.close()
        cleanup_error = _cleanup(factory, participant)
        participant = None
        cpu_seconds = _process_cpu_seconds()
        result = {
            "role": "publisher",
            "endpoint_id": endpoint_id,
            "pid": os.getpid(),
            "status": "completed",
            "raw_file": raw_name,
            "raw_record_count": raw_count,
            "sent_success_count": raw_count,
            "send_failure_count": sent_failure_count,
            "warmup_success_count": warmup_success_count,
            "acknowledgment_result": ack_result,
            "cleanup_error": cleanup_error,
            "process_cpu_time_seconds": cpu_seconds,
            "process_wall_time_seconds": (
                time.perf_counter_ns() - PROCESS_WALL_START_NS
            )
            / 1_000_000_000.0,
            "completed_time": utc_now_iso(),
            "error": None,
        }
        atomic_write_json(result_path, result)
        status.update(state="finished")
        return 0
    except Exception as exc:
        try:
            raw_count = raw_writer.close()
        except Exception:
            raw_count = 0
        cleanup_error = _cleanup(factory, participant)
        error = f"{type(exc).__name__}: {exc}"
        atomic_write_json(
            result_path,
            {
                "role": "publisher",
                "endpoint_id": endpoint_id,
                "pid": os.getpid(),
                "status": "failed",
                "raw_file": raw_name,
                "raw_record_count": raw_count,
                "sent_success_count": raw_count,
                "send_failure_count": sent_failure_count,
                "cleanup_error": cleanup_error,
                "process_cpu_time_seconds": _process_cpu_seconds(),
                "error": error,
                "traceback": traceback.format_exc(),
            },
        )
        status.update(state="error", error=error)
        return 2


def run_subscriber(args: argparse.Namespace, condition: dict[str, Any]) -> int:
    endpoint_id = int(args.endpoint_id)
    run_dir = Path(args.run_dir)
    status = StatusTracker(
        run_dir / "status" / f"subscriber_{endpoint_id}.json",
        "subscriber",
        endpoint_id,
    )
    raw_name = f"subscriber_{endpoint_id}.receive.bin"
    raw_writer = ReceiveRecordWriter(run_dir / raw_name)
    result_path = run_dir / f"subscriber_{endpoint_id}.result.json"
    factory = participant = None
    cleanup_error: str | None = None
    snapshot_record_count: int | None = None
    snapshot_ns: int | None = None

    try:
        import BenchmarkMessage
        import fastdds

        factory, participant, topic, _topic_data_type, _type_support = (
            _make_participant_and_topic(
                fastdds,
                BenchmarkMessage,
                int(args.domain_id),
                args.profile_name,
                args.topic_name,
                status,
            )
        )

        matched = threading.Event()
        expected_matches = int(condition["publisher_count"])
        expected_size = int(condition["payload_size_bytes"])
        expected_checksum = int(condition["payload_checksum"])

        class ReaderListener(fastdds.DataReaderListener):
            def __init__(self) -> None:
                super().__init__()
                self._callback_lock = threading.Lock()
                self._active = True
                self.read_error_count = 0
                self.decode_error_count = 0
                self.non_data_sample_count = 0
                self.first_callback_error: str | None = None

            def on_subscription_matched(self, reader: Any, info: Any) -> None:
                count = int(info.current_count)
                status.update_match(count, expected_matches)
                if count >= expected_matches:
                    matched.set()

            def on_data_available(self, reader: Any) -> None:
                with self._callback_lock:
                    if not self._active:
                        return
                    while True:
                        data = BenchmarkMessage.BenchmarkMessage()
                        info = fastdds.SampleInfo()
                        code = reader.take_next_sample(data, info)
                        if code == fastdds.RETCODE_NO_DATA:
                            return
                        if code != fastdds.RETCODE_OK:
                            self.read_error_count += 1
                            return
                        receive_timestamp_ns = time.perf_counter_ns()
                        if not info.valid_data:
                            self.non_data_sample_count += 1
                            continue
                        try:
                            phase = int(data.phase())
                            if phase != 1:
                                continue
                            publisher_id = int(data.publisher_id())
                            sequence_number = int(data.sequence_number())
                            send_timestamp_ns = int(data.send_timestamp_ns())
                            declared_length = int(data.payload_length())
                            declared_checksum = int(data.checksum())
                            flags = FLAG_DDS_VALID
                            try:
                                payload = read_payload_text(data)
                                payload_bytes = payload.encode("ascii")
                                actual_length = len(payload_bytes)
                                actual_checksum = crc32_text(payload)
                                flags |= FLAG_DECODE_OK
                                if (
                                    actual_length == declared_length
                                    and actual_length == expected_size
                                ):
                                    flags |= FLAG_LENGTH_OK
                                if (
                                    actual_checksum == declared_checksum
                                    and actual_checksum == expected_checksum
                                ):
                                    flags |= FLAG_CHECKSUM_OK
                            except (UnicodeError, ValueError, TypeError):
                                self.decode_error_count += 1
                                actual_length = 0
                                actual_checksum = 0
                            raw_writer.append(
                                ReceiveRecord(
                                    publisher_id,
                                    sequence_number,
                                    send_timestamp_ns,
                                    receive_timestamp_ns,
                                    declared_length,
                                    declared_checksum,
                                    actual_length,
                                    actual_checksum,
                                    flags,
                                )
                            )
                        except Exception as exc:
                            self.read_error_count += 1
                            if self.first_callback_error is None:
                                self.first_callback_error = (
                                    f"{type(exc).__name__}: {exc}"
                                )

            def deactivate(self) -> None:
                with self._callback_lock:
                    self._active = False

        subscriber = participant.create_subscriber(fastdds.SUBSCRIBER_QOS_DEFAULT)
        if subscriber is None:
            raise RuntimeError("create_subscriber returned None")
        reader_qos = fastdds.DataReaderQos()
        subscriber.get_default_datareader_qos(reader_qos)
        _configure_endpoint_qos(fastdds, reader_qos, condition["qos"])
        listener = ReaderListener()
        reader = subscriber.create_datareader(topic, reader_qos, listener)
        if reader is None:
            raise RuntimeError("create_datareader returned None")
        status.update(state="ready", ready_ns=time.perf_counter_ns())

        if not matched.wait(timeout=float(condition["discovery_timeout_seconds"])):
            raise TimeoutError(
                f"Subscriber {endpoint_id} did not match {expected_matches} publisher(s)"
            )

        deadline_ns = time.perf_counter_ns() + int(
            float(condition["endpoint_timeout_seconds"]) * 1_000_000_000
        )
        while time.perf_counter_ns() < deadline_ns:
            if snapshot_record_count is None and (run_dir / "send_complete.json").exists():
                snapshot_record_count = raw_writer.snapshot_count(flush=True)
                snapshot_ns = time.perf_counter_ns()
                status.update(
                    state="draining",
                    snapshot_record_count=snapshot_record_count,
                    snapshot_ns=snapshot_ns,
                )
            if (run_dir / "stop.json").exists():
                break
            time.sleep(0.005)
        else:
            raise TimeoutError("Subscriber timed out waiting for stop signal")

        if snapshot_record_count is None:
            snapshot_record_count = raw_writer.snapshot_count(flush=True)
            snapshot_ns = time.perf_counter_ns()
        listener.deactivate()
        cleanup_error = _cleanup(factory, participant)
        participant = None
        raw_count = raw_writer.close()
        cpu_seconds = _process_cpu_seconds()
        result = {
            "role": "subscriber",
            "endpoint_id": endpoint_id,
            "pid": os.getpid(),
            "status": "completed",
            "raw_file": raw_name,
            "raw_record_count": raw_count,
            "snapshot_record_count": snapshot_record_count,
            "snapshot_ns": snapshot_ns,
            "read_error_count": listener.read_error_count,
            "decode_error_count": listener.decode_error_count,
            "non_data_sample_count": listener.non_data_sample_count,
            "first_callback_error": listener.first_callback_error,
            "cleanup_error": cleanup_error,
            "process_cpu_time_seconds": cpu_seconds,
            "process_wall_time_seconds": (
                time.perf_counter_ns() - PROCESS_WALL_START_NS
            )
            / 1_000_000_000.0,
            "completed_time": utc_now_iso(),
            "error": None,
        }
        atomic_write_json(result_path, result)
        status.update(state="finished")
        return 0
    except Exception as exc:
        try:
            raw_count = raw_writer.close()
        except Exception:
            raw_count = 0
        cleanup_error = _cleanup(factory, participant)
        error = f"{type(exc).__name__}: {exc}"
        atomic_write_json(
            result_path,
            {
                "role": "subscriber",
                "endpoint_id": endpoint_id,
                "pid": os.getpid(),
                "status": "failed",
                "raw_file": raw_name,
                "raw_record_count": raw_count,
                "snapshot_record_count": snapshot_record_count or 0,
                "cleanup_error": cleanup_error,
                "process_cpu_time_seconds": _process_cpu_seconds(),
                "error": error,
                "traceback": traceback.format_exc(),
            },
        )
        status.update(state="error", error=error)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal Fast DDS endpoint")
    parser.add_argument("--role", choices=("publisher", "subscriber"), required=True)
    parser.add_argument("--endpoint-id", type=int, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--condition-file", type=Path, required=True)
    parser.add_argument("--topic-name", required=True)
    parser.add_argument("--domain-id", type=int, required=True)
    parser.add_argument("--profile-name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    condition = read_json(args.condition_file)
    if args.role == "publisher":
        return run_publisher(args, condition)
    return run_subscriber(args, condition)


if __name__ == "__main__":
    raise SystemExit(main())
