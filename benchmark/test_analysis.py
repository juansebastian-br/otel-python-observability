import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from analyze import analyze

class AnalysisTests(unittest.TestCase):
    def fixture(self, root, baseline_cpu=10, failed=False):
        (root/'metadata.json').write_text(json.dumps({'vus':50,'duration_seconds':300}))
        for mode,cpu,p99 in [('baseline',baseline_cpu,100),('otel',15,120)]:
            folder=root/('01-'+mode); folder.mkdir()
            metrics={k:{'values':v} for k,v in {
                'http_req_duration':{'p(99)':p99,'p(95)':90},
                'http_reqs':{'count':1000,'rate':3.33},
                'http_req_failed':{'rate':0},'checks':{'rate':1},
            }.items()}
            (folder/'summary.json').write_text(json.dumps({'metrics':metrics}))
            (folder/'run.json').write_text(json.dumps({'mode':mode,'k6_exit_code':99 if failed else 0,'sampling_errors':[]}))
            with (folder/'resources.csv').open('w',newline='') as f:
                w=csv.writer(f); w.writerow(['timestamp_utc','service','cpu_percent','memory_mib'])
                # Two non-simultaneous per-service maxima must NOT be added for peak memory.
                for i,(a,b) in enumerate([(90,10),(10,90),(50,50)]):
                    scale=1 if mode=='baseline' else 1.25
                    w.writerow([i,'orders-service',cpu,a*scale]); w.writerow([i,'payments-service',cpu,b*scale])
                    w.writerow([i,'otel-collector',1,50])
                    for service in ('postgres','loki','jaeger','prometheus','grafana'):
                        w.writerow([i,service,0,0])

    def result(self,root):
        with contextlib.redirect_stdout(io.StringIO()): return analyze(root)

    def test_relative_cpu_and_simultaneous_memory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); self.fixture(root); r=self.result(root)
            self.assertEqual(r['delta']['p99_additional_ms'],20)
            self.assertEqual(r['delta']['cpu_overhead_percent'],50)
            self.assertEqual(r['delta']['cpu_percentage_points'],10)
            self.assertEqual(r['delta']['memory_additional_mib'],25)
            self.assertEqual(r['aggregate']['baseline']['memory_mib_peak'],100)

    def test_zero_baseline_cpu_is_undefined(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); self.fixture(root,baseline_cpu=0)
            self.assertIsNone(self.result(root)['delta']['cpu_overhead_percent'])

    def test_failed_load_is_not_valid(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); self.fixture(root,failed=True)
            self.assertFalse(self.result(root)['valid'])

if __name__=='__main__': unittest.main()
