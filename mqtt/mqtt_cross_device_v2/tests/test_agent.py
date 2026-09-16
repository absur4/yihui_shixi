from http.server import ThreadingHTTPServer
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
import zipfile

from mqtt_cd.agent import AgentError, NodeService, Run, handler_class, mosquitto_password_hash, run_stage, runtime_origin
from mqtt_cd.common import DEFAULT_CASE, write_json
from mqtt_cd.processes import spawn as real_spawn

SECRET = "unit-test-token-at-least-24-characters"


class NodeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NodeService("A", self.temp.name)
        self.environment = patch.dict(os.environ, MQTT_CD_TOKEN=SECRET)
        self.environment.start()

    def tearDown(self):
        self.service.shutdown()
        self.environment.stop()
        self.temp.cleanup()

    def make_run(self, state="ready"):
        identifier = str(uuid.uuid4())
        folder = Path(self.temp.name) / identifier
        folder.mkdir()
        spec = dict(run_id=identifier, role="receiver", node_id="A", case=dict(DEFAULT_CASE),
                    broker_host="127.0.0.1", broker_port=18883)
        run = Run(identifier, folder, spec, state=state)
        self.service.current = run
        return run

    def test_paths_cannot_escape_data_directory(self):
        for identifier in ("../outside", "..\\outside", "/tmp/file", "A" * 36, ""):
            with self.subTest(identifier=identifier), self.assertRaises(AgentError):
                self.service.status(identifier)

    def test_only_ready_run_can_arm_and_timestamp_has_warmup_lead(self):
        run = self.make_run()
        with self.assertRaises(AgentError):
            self.service.arm(run.run_id, dict(start_ns=time.perf_counter_ns()))
        start = time.perf_counter_ns() + 10_000_000_000
        result = self.service.arm(run.run_id, dict(start_ns=start))
        self.assertEqual(result["state"], "armed")
        self.assertEqual(json.loads((run.folder / "arm.json").read_text())["start_ns"], start)
        with self.assertRaises(AgentError):
            self.service.arm(run.run_id, dict(start_ns=start))
        run.done.set()

    def test_archive_excludes_private_broker_files(self):
        run = self.make_run("completed")
        private = run.folder / ".private"
        private.mkdir()
        (private / "passwords").write_text(SECRET)
        write_json(run.folder / "spec.json", run.spec)
        with self.assertRaises(AgentError):
            self.service.artifact(run.run_id)
        run.done.set()
        path = self.service.artifact(run.run_id)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), ["spec.json"])
            self.assertNotIn(SECRET.encode(), archive.read("spec.json"))

    def test_cleanup_failure_cannot_publish_mutable_artifacts(self):
        run = self.make_run("error")
        run.done.set()
        run.processes.append(("publisher", SimpleNamespace(poll=lambda: None)))
        self.assertFalse(self.service.status(run.run_id)["archived"])
        with self.assertRaises(AgentError):
            self.service.artifact(run.run_id)

    def test_prepare_rejects_sender_without_local_broker(self):
        with self.assertRaises(AgentError) as result:
            self.service.prepare(dict(run_id=str(uuid.uuid4()), role="sender", case={},
                                      broker_host="127.0.0.1", broker_port=18883))
        self.assertEqual(result.exception.status, 503)

    def test_active_run_blocks_second_prepare(self):
        run = self.make_run()
        with patch("mqtt_cd.agent.dependencies", return_value={"psutil": {"available": True}}):
            with self.assertRaises(AgentError) as result:
                self.service.prepare(dict(run_id=str(uuid.uuid4()), role="receiver", case={},
                                          broker_host="127.0.0.1", broker_port=18883))
        self.assertEqual(result.exception.status, 409)
        run.done.set()

    def test_credentials_are_not_saved_in_specs(self):
        with self.assertRaises(AgentError):
            self.service.prepare(dict(run_id=str(uuid.uuid4()), role="receiver", case={"password": SECRET},
                                      broker_host="127.0.0.1", broker_port=18883))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_authenticated_status_renews_lease(self):
        run = self.make_run()
        run.last_contact = time.monotonic() - 50
        self.service.touch()
        self.assertLess(time.monotonic() - run.last_contact, .1)
        run.done.set()

    def test_password_hash_has_mosquitto_format_and_random_salt(self):
        first = mosquitto_password_hash(SECRET)
        second = mosquitto_password_hash(SECRET)
        self.assertNotEqual(first, second)
        self.assertNotIn(SECRET, first)
        prefix, algorithm, iterations, salt, digest = first.split("$")
        self.assertEqual((prefix, algorithm, iterations), ("", "7", "1000"))
        salt_bytes = base64.b64decode(salt, validate=True)
        self.assertEqual(len(salt_bytes), 12)
        decoded = base64.b64decode(digest, validate=True)
        self.assertEqual(len(decoded), 64)
        self.assertEqual(decoded, hashlib.pbkdf2_hmac("sha512", SECRET.encode(), salt_bytes, 1000))
        self.assertNotEqual(decoded, hashlib.pbkdf2_hmac("sha512", b"wrong-password", salt_bytes, 1000))

    def test_expired_controller_lease_reaps_owned_child_before_archive(self):
        run = self.make_run("preparing")
        run.last_contact = time.monotonic() - 100

        def inert_worker(*args, **kwargs):
            return real_spawn([sys.executable, "-c", "import time; time.sleep(30)"],
                              stdout=kwargs["stdout"], stderr=kwargs["stderr"])

        with patch("mqtt_cd.agent.spawn", side_effect=inert_worker):
            self.service._execute(run)
        self.assertEqual(run.state, "cancelled")
        self.assertTrue(run.done.is_set())
        self.assertTrue((run.folder / "cancel").exists())
        self.assertIn("Controller lease expired", run.errors)
        self.assertTrue(run.workers)
        self.assertTrue(all(process.poll() is not None for _, _, process in run.workers))
        final = json.loads((run.folder / "agent_status.json").read_text())
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["failure_origin"], "controller")
        self.assertEqual(final["stage"], "preparing")
        self.assertTrue(final["archived"])
        self.assertTrue(self.service.artifact(run.run_id).is_file())

    def test_phase_boundaries_distinguish_preparation_from_dut_failure(self):
        run = self.make_run()
        run.arm_start = 10_000_000_000
        for timestamp, stage in ((2_000_000_000, "armed"), (3_000_000_000, "warmup"),
                                 (8_000_000_000, "settle"), (10_000_000_000, "formal"),
                                 (30_000_000_000, "drain"), (32_000_000_000, "diagnostic")):
            self.assertEqual(run_stage(run, timestamp), stage)
        self.assertEqual(runtime_origin("formal"), "dut")
        self.assertEqual(runtime_origin("warmup"), "configuration")
        run.done.set()


class HttpAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NodeService("B", self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.service, SECRET))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.shutdown()
        self.temp.cleanup()

    def request(self, path, method="GET", authorization=SECRET, raw=None):
        headers = {} if authorization is None else {"Authorization": "Bearer " + authorization}
        request = Request(self.base + path, data=raw, headers=headers, method=method)
        return urlopen(request, timeout=2)

    def test_every_endpoint_requires_token(self):
        for path, method in (("/health", "GET"), ("/clock", "POST"), ("/prepare", "POST"),
                             ("/artifact/" + str(uuid.uuid4()), "GET")):
            for supplied in (None, "incorrect"):
                with self.subTest(path=path, supplied=supplied), self.assertRaises(HTTPError) as exc:
                    self.request(path, method, supplied)
                self.assertEqual(exc.exception.code, 401)

    def test_health_and_clock_contract(self):
        with self.request("/health") as response:
            health = json.load(response)
        self.assertEqual(health["node_id"], "B")
        self.assertTrue(health["hostname"])
        with self.request("/clock", "POST", raw=b"{}") as response:
            sample = json.load(response)
        self.assertLessEqual(sample["receive_ns"], sample["send_ns"])
        self.assertEqual(sample["node_id"], "B")

    def test_bad_json_and_non_object_body_rejected(self):
        for raw in (b"not-json", b"[]"):
            with self.assertRaises(HTTPError) as result:
                self.request("/prepare", "POST", raw=raw)
            self.assertEqual(result.exception.code, 400)

    def test_bad_uuid_rejected_before_filesystem_lookup(self):
        with self.assertRaises(HTTPError) as result:
            self.request("/artifact/not-a-uuid")
        self.assertEqual(result.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
