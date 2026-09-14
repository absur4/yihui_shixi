import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mqtt_ui import Console


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.app = Console(Path('D:/mqtt_win64_v1.0/mqtt_win64_v1.0'))

    def test_custom_condition_is_separate_and_uses_local_broker(self):
        c, repeats = self.app.config(dict(template_index=0, configuration=dict(message_count=20, repeats=1)))
        self.assertEqual(c['benchmark_profile'], 'custom')
        self.assertEqual(c['host'], '127.0.0.1')
        self.assertEqual(repeats, 1)
        self.assertFalse(c['rate_search'])

    def test_matrix_preserves_standard_fields(self):
        c, repeats = self.app.config(dict(template_index=0, matrix=True, configuration=dict(message_count=20)))
        self.assertEqual(len(c['scenarios']), 2)
        self.assertEqual(repeats, 10)
        self.assertEqual(c['scenarios'][0]['message_count'], 10000)

    def test_rejects_invalid_values_and_executable_injection(self):
        for config in [dict(publish_rate_hz=float('nan')), dict(publisher_count=1.5), dict(repeats=True),
                       dict(qos_profile='bad'), dict(broker_executable='unexpected.exe'),
                       dict(message_count=0, duration_seconds=0), dict(network_loss_rate=2)]:
            with self.assertRaises(ValueError):
                self.app.config(dict(template_index=0, configuration=config))

    def test_history_path_confinement(self):
        for value in ['../../Windows', 'C:/', 'existing/../config.yaml']:
            with self.assertRaises(ValueError):
                self.app.locate(value)


if __name__ == '__main__':
    unittest.main()
