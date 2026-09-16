import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mqtt_cd.common import config_hash, read_json, safe_member, validate_case
from mqtt_cd.controller import NodeClient, collect_prior_results, run_suite, validate_config


def configuration(cooldown=20):
    return validate_config({"nodes": {"A": {"url": "http://a:8765", "mqtt_host": "a"},
                                      "B": {"url": "http://b:8765", "mqtt_host": "b"}},
                            "cooldown_s": cooldown})


def fingerprint(config):
    return config_hash(dict({key: config.get(key) for key in
                            ("nodes", "broker_port", "clock", "environment", "cooldown_s")},
                            planning_seed=20260916))


class ControllerContractTests(unittest.TestCase):
    def test_numeric_and_path_validation(self):
        for update in ({"rate_hz":True},{"duration_s":float("nan")},{"payload_bytes":0},
                       {"qos":1},{"subscribers":5},{"duration_s":0}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_case(update)
        for value in ("../outside", "a/../b", "/absolute", "C:/escape", "a\\..\\b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_member(value)

    def test_config_does_not_accept_credential_url_or_unsafe_host(self):
        cfg = {"nodes":{"A":{"url":"http://localhost:8765","mqtt_host":"127.0.0.1"},
                        "B":{"url":"http://localhost:8766","mqtt_host":"127.0.0.1"}}}
        self.assertFalse(validate_config(cfg)["clock"]["model_validated"])
        cfg["nodes"]["A"]["url"] = "http://secret:password@localhost"
        with self.assertRaises(ValueError):
            validate_config(cfg)

    def test_archive_traversal_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,"w") as archive:
            archive.writestr("../outside.txt","bad")
        client = NodeClient("A",{"url":"http://localhost:8765"})
        client._open = lambda *args, **kwargs: io.BytesIO(buffer.getvalue())
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)/"nodes"/"A"
            with self.assertRaises(ValueError):
                client.artifacts("run",target)
            self.assertFalse((target.parent/"outside.txt").exists())


class SuiteTests(unittest.TestCase):
    def fake_run(self, config, case, output, allow_local):
        return {"run_id": str(len(self.executed)), "spec": dict(case),
                "execution_status": "completed", "capacity_verdict": "indeterminate",
                "local_check": allow_local}

    def setUp(self):
        self.executed = []

    def recording_run(self, *args):
        self.executed.append(args[1])
        return self.fake_run(*args)

    def test_cd4_honors_direction_repeat_and_exploratory_overrides(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("mqtt_cd.controller.health", return_value=({}, {})), \
                patch("mqtt_cd.controller.run_case", side_effect=self.recording_run), \
                patch("mqtt_cd.controller.time.sleep"), patch("mqtt_cd.report.write_report"):
            suite = run_suite(configuration(), [], temporary, cd4=True,
                              selected_directions=["A_to_B"], generated_repeats=1,
                              generated_overrides={"duration_s": 10, "warmup_s": .2, "drain_s": 1},
                              manual_rates={direction: {"CD4-L": 100, "CD4-H": 200}
                                            for direction in ("A_to_B", "B_to_A")})
            self.assertEqual(suite["status"], "completed")
            self.assertEqual(len(self.executed), 2)
            for case in self.executed:
                self.assertEqual(case["direction"], "A_to_B")
                self.assertEqual(case["duration_s"], 10)
                self.assertEqual(case["warmup_s"], .2)
                self.assertEqual(case["repeat"], 1)
                self.assertEqual(case["measurement_kind"], "exploratory")
            self.assertTrue((Path(temporary)/"experiment_manifest.json").is_file())

    def test_followup_preserves_transitive_evidence_and_summary(self):
        cfg = configuration()
        prior = {"run_id": "prior", "spec": {"comparison_fingerprint": fingerprint(cfg)}}
        with tempfile.TemporaryDirectory() as temporary, \
                patch("mqtt_cd.controller.health", return_value=({}, {})), \
                patch("mqtt_cd.controller.run_case", side_effect=self.recording_run), \
                patch("mqtt_cd.planner.summarize_scans", side_effect=lambda rows: [r["run_id"] for r in rows]), \
                patch("mqtt_cd.report.write_report"):
            run_suite(cfg, [validate_case({})], temporary, prior_results=[prior])
            saved = read_json(Path(temporary)/"result.json")
            self.assertEqual(saved["summary"], ["prior", "1"])
            self.assertEqual([r["run_id"] for r in collect_prior_results(saved)], ["prior", "1"])
            self.assertEqual(saved["reference_runs"], [prior])

    def test_different_configuration_rejects_prior_evidence_before_connecting(self):
        prior = {"run_id": "prior", "spec": {"comparison_fingerprint": fingerprint(configuration())}}
        with patch("mqtt_cd.controller.health") as request, self.assertRaises(ValueError):
            run_suite(configuration(0), [], "unused", prior_results=[prior])
        request.assert_not_called()

    def test_nonstandard_cooldown_marks_runs_exploratory(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("mqtt_cd.controller.health", return_value=({}, {})), \
                patch("mqtt_cd.controller.run_case", side_effect=self.recording_run), \
                patch("mqtt_cd.report.write_report"):
            run_suite(configuration(0), [validate_case({})], temporary)
            self.assertEqual(self.executed[0]["measurement_kind"], "exploratory")

    def test_repeated_reference_id_is_deduplicated_but_conflict_rejected(self):
        old = {"run_id": "same", "capacity_verdict": "fail"}
        self.assertEqual(collect_prior_results({"reference_runs": [old], "runs": [old]}), [old])
        with self.assertRaises(ValueError):
            collect_prior_results({"reference_runs": [old], "runs": [dict(old, capacity_verdict="pass")]})

    def test_different_planning_seed_rejects_prior_evidence(self):
        cfg = configuration()
        prior = {"run_id": "prior", "spec": {"comparison_fingerprint": fingerprint(cfg)}}
        with patch("mqtt_cd.controller.health") as request, self.assertRaises(ValueError):
            run_suite(cfg, [], "unused", prior_results=[prior], planning_seed=7)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
