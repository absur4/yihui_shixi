import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import adapter
import console_runner
import mqtt_adapter
from mqtt_evidence import finalize, unavailable_result, COUNTS
from mqtt_results import METRICS, analyze
from mqtt_config import normalize


class ContractTests(unittest.TestCase):
    def test_import_without_site_packages(self):
        script = "import importlib.util as u; s=u.spec_from_file_location('isolated', " + repr(str(HERE/'adapter.py')) + "); m=u.module_from_spec(s); s.loader.exec_module(m); assert len(m.catalog()) == 39; assert not m.metadata()['available']"
        r = subprocess.run([sys.executable, '-B', '-S', '-c', script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object')
    def test_forced_stop_cleans_child(self):
        import time
        import ctypes
        from ctypes import wintypes
        script = "import sys,subprocess,time; sys.path.insert(0," + repr(str(HERE)) + "); from mqtt_processes import contain_children; contain_children(); p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
        parent=subprocess.Popen([sys.executable,'-B','-c',script],stdout=subprocess.PIPE,text=True)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=None
        try:
            pid=int(parent.stdout.readline().strip())
            handle=kernel.OpenProcess(0x100000,False,pid)
            self.assertTrue(handle)
            parent.terminate()
            parent.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait(timeout=5)
            parent.stdout.close()
            if handle: kernel.CloseHandle(handle)

    def test_catalog_and_matrix(self):
        rows = adapter.catalog()
        self.assertEqual({r['scenario_name'] for r in rows}, {f'S{i:02d}' for i in range(1, 13)})
        self.assertEqual(len({r['condition_id'] for r in rows}), len(rows))
        scan = [r for r in rows if r['scenario_name']=='S02']
        self.assertEqual(len(scan), 8)
        self.assertEqual(max(r['payload_size_bytes'] for r in scan), 1048576)
        self.assertEqual(adapter.build_cases(0, {'repeats':1}, True), rows[:2])
        self.assertEqual(adapter.build_cases(0, {'repeats':2}, False)[0]['case_repeats'], 2)
        for i, row in enumerate(rows):
            if row['scenario_name']=='S03':
                self.assertTrue(adapter.build_cases(i, {}, False)[0]['rate_search'])
        for bad in ({'repeats':True}, {'repeats':0}, {'publisher_count':1.5},
                    {'publish_rate_hz':float('nan')}, {'network_loss_rate':2},
                    {'broker_executable':'evil'}, {'qos_profile':'bad'},
                    {'duration_seconds':0, 'message_count':0}):
            with self.assertRaises(ValueError):
                adapter.build_cases(0, bad, False)

    def test_probe_and_result_type(self):
        with tempfile.TemporaryDirectory() as temp, patch('adapter._probe', return_value=['缺少测试依赖']):
            result = adapter.Adapter().run(adapter.catalog()[0], {'output_dir':temp})
            self.assertIsInstance(result, adapter.ScenarioResult)
            run = adapter.result_dict(result)
            self.assertEqual(run['status'], 'not_tested')
            self.assertTrue(all(run[k] is None for k in METRICS+COUNTS))
            self.assertIsNotNone(run['test_end_time'])
            self.assertEqual(run['statistics']['latency_ms']['count'], 0)
            self.assertEqual(json.loads((Path(temp)/run['samples_path']).read_text(encoding='utf-8'))['latencies_ms'], [])

    def test_real_csv_export_and_distributions(self):
        cfg = normalize({'message_count':2, 'subscriber_count':1})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            folder = output/'artifacts'/'sample'
            folder.mkdir(parents=True)
            with (folder/'publisher-0.csv').open('w',newline='') as f:
                csv.writer(f).writerows([[0,1000000,1100000,1,1,0], [1,2000000,2100000,1,1,0]])
            with (folder/'subscriber-0.csv').open('w',newline='') as f:
                csv.writer(f).writerows([[0,0,1000000,2000000,1,'valid',1024,0],
                                        [0,1,2000000,5000000,1,'valid',1024,0],
                                        [0,2,2000000,9000000,0,'valid',1024,0],
                                        [0,3,2000000,9000000,1,'checksum_error',1024,0]])
            result=analyze(folder,cfg,1000000,10000000)
            result.update(run_id='sample', configuration=cfg, scenario_name='S01')
            finalize(result,cfg,output,folder)
            mapped=adapter._map_result(result,cfg)
            self.assertEqual(mapped['statistics']['latency_ms']['mean'],2)
            self.assertEqual(mapped['statistics']['jitter_ms']['mean'],2)
            self.assertEqual(mapped['link_metrics'][0]['latency_ms']['p50'],2)
            self.assertEqual(mapped['link_metrics'][0]['publisher'],0)
            self.assertEqual(json.loads((folder/'subscriber-0.result.json').read_text(encoding='utf-8'))['latencies_ms'],[1,3])

    def make_spec(self, temp, repeats=2):
        path=Path(temp)/'spec.json'
        case=adapter.build_cases(0,{'repeats':repeats},False)[0]
        path.write_text(json.dumps(dict(middleware='mqtt', config={}, cases=[case],
            plan=[dict(scenario_id='S01',planned_repeats=repeats)], output=str(Path(temp)/'output'),
            logs=str(Path(temp)/'output/logs'))),encoding='utf-8')
        return path

    def test_runner_repeats_and_archive(self):
        with tempfile.TemporaryDirectory() as temp, patch('adapter._probe', return_value=['缺少依赖']):
            path=self.make_spec(temp)
            self.assertEqual(console_runner.run_spec(path),0)
            output=Path(temp)/'output'
            suite=json.loads((output/'result.json').read_text(encoding='utf-8'))
            self.assertEqual(suite['planned_runs'],2)
            self.assertEqual([r['repeat'] for r in suite['runs']],[1,2])
            for run in suite['runs']:
                self.assertEqual(run,json.loads((output/'runs'/f"{run['run_id']}.json").read_text(encoding='utf-8')))
                self.assertTrue((output/run['samples_path']).is_file())
            self.assertEqual(json.loads((Path(temp)/'job.json').read_text(encoding='utf-8'))['status'],'completed')
            log=(Path(temp)/'console.log').read_text(encoding='utf-8')
            self.assertEqual(log.count('START '),2)
            self.assertEqual(log.count('END '),2)
            self.assertIn('RESULT ',log)
            self.assertTrue(all(line.startswith('[') for line in log.splitlines()))
            self.assertEqual(log,(output/'console.log').read_text(encoding='utf-8'))
            previous=(output/'result.json').read_bytes()
            with self.assertRaises(ValueError): console_runner.run_spec(path)
            self.assertEqual(previous,(output/'result.json').read_bytes())

    def test_plan_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.make_spec(temp)
            spec=json.loads(path.read_text(encoding='utf-8')); spec['plan'][0]['planned_repeats']=1
            path.write_text(json.dumps(spec),encoding='utf-8')
            self.assertEqual(console_runner.run_spec(path),1)
            self.assertEqual(json.loads((Path(temp)/'output/result.json').read_text(encoding='utf-8'))['status'],'error')

    def test_cancel_and_negative_outcome(self):
        self.assertEqual(console_runner.suite_status([{'status':'timeout','expected_negative_outcome':True}],False),'completed')
        self.assertEqual(console_runner.suite_status([{'status':'timeout'}],False),'error')
        with tempfile.TemporaryDirectory() as temp, patch('adapter.Adapter.run', side_effect=KeyboardInterrupt):
            path=self.make_spec(temp,1)
            self.assertEqual(console_runner.run_spec(path),130)
            suite=json.loads((Path(temp)/'output/result.json').read_text(encoding='utf-8'))
            self.assertEqual(suite['status'],'cancelled')
            run=suite['runs'][0]
            self.assertEqual(run,json.loads((Path(temp)/'output/runs'/f"{run['run_id']}.json").read_text(encoding='utf-8')))

    def test_calibration_scan_and_cache(self):
        cfg=normalize({'stable_rate_candidates':[100,1000,5000],'rate_search':True})
        def trial(c, out, **kw):
            return dict(run_id=str(c['publish_rate_hz']),status='completed',saturation={'detected':c['publish_rate_hz']==5000})
        cache={}
        with patch('mqtt_adapter.execute',side_effect=trial) as execute:
            selected,trials=mqtt_adapter.calibrate(cfg,Path('unused'),cache)
            self.assertEqual(selected,1000)
            self.assertEqual(len(trials),3)
            mqtt_adapter.calibrate(cfg,Path('unused'),cache)
            self.assertEqual(execute.call_count,3)

    def test_calibration_called_from_execute(self):
        cfg=normalize({'scenario_name':'S03','rate_search':True})
        with tempfile.TemporaryDirectory() as temp, patch('adapter._probe',return_value=[]), \
             patch('mqtt_adapter.calibrate',return_value=(None,[{'status':'error'}])) as calibrate, \
             patch('mqtt_adapter._execute',return_value=dict(run_id='x',status='completed', errors=[])) as execute:
            result=mqtt_adapter.execute(cfg,temp)
            calibrate.assert_called_once()
            self.assertFalse(execute.call_args.args[0]['rate_search'])
            self.assertIsNone(result['rate_calibration']['selected_rate_hz'])
            self.assertEqual(result['status'],'error')


if __name__=='__main__':
    unittest.main()
