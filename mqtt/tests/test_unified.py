import csv
import json
import math
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mqtt_adapter import normalize
from mqtt_metrics import describe, latency_metrics, percentile, throughput, resource_window_metrics
from mqtt_results import analyze
from mqtt_wire import HEADER, pack, unpack


class UnifiedTests(unittest.TestCase):
    def test_document_formulas(self):
        self.assertEqual(percentile([0,10],.95),9.5)
        self.assertEqual(percentile([0,10],.99),9.9)
        self.assertEqual(describe([0,10])['std'],5)
        self.assertEqual(describe([0,10])['variance'],25)
        self.assertIsNone(describe([])['mean'])
        self.assertEqual(throughput(1000,1024,1),8.192)
        self.assertIsNone(throughput(1,1024,0))

    def test_jitter_is_per_link_in_sequence_order(self):
        metrics=latency_metrics([[(2,9),(0,1),(1,3)],[(1,100),(0,90)]])
        self.assertEqual(metrics['jitter_ms'],6)
        self.assertEqual(metrics['jitter_sample_count'],3)
        self.assertIsNone(latency_metrics([[(0,5)]])['jitter_ms'])
        self.assertIsNone(latency_metrics([])['latency_std_ms'])

    def test_payload_and_corruption(self):
        run=uuid.uuid4().bytes
        for size in [0,1024,65536,1048576,4194304]:
            data=pack(run,0,3,123456,b'x'*size)
            self.assertEqual(len(data),HEADER.size+size)
            self.assertEqual(unpack(data,run,size,1)[1],'valid')
        data=pack(run,0,3,123456,b'abcd')
        self.assertEqual(unpack(data[:-1],run,4,1)[1],'length_error')
        self.assertEqual(unpack(data[:-1]+b'z',run,4,1)[1],'checksum_error')
        self.assertEqual(unpack(b'bad',run,4,1)[1],'unparseable')
        self.assertEqual(unpack(data,uuid.uuid4().bytes,4,1)[1],'foreign_run')

    def test_exact_delivery_accounting_and_recovery_separation(self):
        cfg=normalize(dict(publisher_count=2,subscriber_count=2,message_count=3,payload_size_bytes=1024))
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            for pub in range(2):
                with open(p/f'publisher-{pub}.csv','w',newline='') as f:
                    csv.writer(f).writerows([[seq,1_000_000_000+seq*1_000_000,1_000_000_100+seq*1_000_000,1,1,0] for seq in range(3)])
            rows=[]
            # Independent sequence spaces; seq1 missing, seq2 then seq0 out of order; seq0 duplicated.
            for pub in range(2):
                for seq,lat in [(2,9),(0,1),(0,1)]:
                    stamp=1_000_000_000+seq*1_000_000
                    rows.append([pub,seq,stamp,stamp+lat*1_000_000,1,'valid',1073,0])
                rows.append([pub,1,1_001_000_000,2_000_000_000,1,'valid',1073,1])
                rows.append([pub,0,2_100_000_000,2_101_000_000,2,'valid',1073,1])
            rows.append([0,9,0,1_001_000_000,1,'checksum_error',1073,0])
            rows.append([-1,-1,0,1_001_000_000,-1,'unparseable',3,0])
            for sub in range(2):
                with open(p/f'subscriber-{sub}.csv','w',newline='') as f:
                    csv.writer(f).writerows(rows)
            r=analyze(p,cfg,1_000_000_000,1_500_000_000)
            self.assertEqual(r['messages_sent'],6)
            self.assertEqual(r['expected_deliveries'],12)
            self.assertEqual(r['unique_deliveries'],8)
            self.assertEqual(r['final_unique_deliveries'],12)
            self.assertEqual(r['duplicate_count'],4)
            self.assertEqual(r['out_of_order_count'],8)
            self.assertEqual(r['corrupted_count'],2)
            self.assertEqual(r['unparseable_count'],2)
            self.assertEqual(r['late_native_deliveries'],4)
            self.assertEqual(r['packet_loss'],1/3)
            self.assertEqual(r['final_packet_loss'],0)
            self.assertEqual(r['latency_ms'],5)
            self.assertEqual(r['latency_std_ms'],4)
            self.assertEqual(r['jitter_ms'],8)
            self.assertEqual(len(r['delivery_matrix']),4)

    def test_configuration(self):
        self.assertEqual(normalize({'message_size_bytes':1024})['payload_size_bytes'],1024)
        for config in [dict(message_size_bytes=10,payload_size_bytes=11),dict(network_loss_rate=1.1),
                       dict(publisher_count=0),dict(publish_rate_hz=float('nan')),dict(qos_profile='invalid')]:
            with self.assertRaises(ValueError):
                normalize(config)

    def test_cpu_windows_weight_actual_sampling_intervals(self):
        samples=[dict(timestamp_ns=1_000_000_000,cpu_percent=0,memory_mb=10),
                 dict(timestamp_ns=10_000_000_000,cpu_percent=100,memory_mb=20)]
        result=resource_window_metrics(samples,0,0,11_000_000_000)
        self.assertEqual(result['cpu_percent'],90)
        self.assertEqual(result['resource_sample_coverage_seconds'],10)
        self.assertEqual(result['memory_mb'],20)
        partial=resource_window_metrics(samples,0,500_000_000,5_000_000_000)
        self.assertAlmostEqual(partial['cpu_percent'],400/4.5)
        self.assertEqual(partial['resource_sample_coverage_seconds'],4.5)


if __name__=='__main__':
    unittest.main()
