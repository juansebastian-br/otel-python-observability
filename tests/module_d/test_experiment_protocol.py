"""Exercise the real worker/controller file protocol without HTTP or Kubernetes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "module-d"))
from experiment import runner, session, worker


class ProtocolTests(unittest.TestCase):
    def test_short_protocol_preserves_the_formal_contract_and_all_three_barriers(self):
        identifier = "d-latency-" + "b" * 32
        value = {"experiment_id": identifier, "mode": "latency",
            "target_url": "http://module-d-payments.observability-chaos-d.svc:8001",
            "path": "/payments", "method": "POST", "body": {"order_id": "00000000-0000-0000-0000-000000000001", "amount": 100, "currency": "COP"}}
        config = worker.validate_config(value, {"POD_NAMESPACE": runner.NAMESPACE, "DEPLOYMENT_ENVIRONMENT": "sandbox"})
        self.assertEqual((config.baseline_seconds, config.chaos_seconds, config.recovery_seconds, config.rate, config.max_workers),
                         (900, 180, 300, 5, 20))

        # Replace only HTTP/time execution in this test. The production config,
        # evidence writers, baseline gates and phase handshakes remain unchanged.
        def observed_fixture_run(config, output, *, stop_event, phase_hook):
            def emit(row):
                output.write(json.dumps({"experiment_id": identifier, "timestamp": runner.utc_now(), **row}) + "\n")
            emit({"record_type": "run_start", "planned_arrivals": 6900})
            count = 0
            for phase, n, duration in (("baseline", 4500, 20), ("chaos", 900, 220), ("recovery", 1500, 20)):
                phase_hook(phase)
                emit({"record_type": "phase_start", "phase": phase})
                for _ in range(n):
                    emit({"record_type": "request", "request_id": str(count), "phase": phase,
                        "attempted": True, "status": 200, "outcome": "http_success", "success": True,
                        "latency_ms": duration})
                    count += 1
                emit({"record_type": "phase_end", "phase": phase})
            end = {"record_type": "run_end", "completed": True, "planned_arrivals": 6900,
                   "attempted": count, "arrival_missed": 0, "successful": count}
            emit(end)
            return end

        with tempfile.TemporaryDirectory() as temp:
            evidence, local = Path(temp) / "remote", Path(temp) / "local"
            evidence.mkdir(); local.mkdir()
            results = []
            thread = threading.Thread(target=lambda: results.append(worker.run_worker(config, evidence,
                run=observed_fixture_run, collection_timeout=10)), daemon=True)
            controller = object.__new__(session.InClusterWorker)
            controller.experiment_id, controller.out = identifier, local
            controller.evidence_copied = False
            def local_exec(script):
                result = subprocess.run([sys.executable, "-c", script.replace("/evidence", evidence.as_posix())],
                                        capture_output=True, text=True, timeout=10, check=True)
                return result.stdout
            controller.call = local_exec
            stream, phases = SimpleNamespace(records=[]), []
            def transition(phase):
                if phase == "chaos":
                    session.baseline_check(stream.records)
                phases.append(phase)
                return {"test_fixture": True}
            thread.start()
            try:
                end = controller.run(transition, stream)
                controller.signal(collected=True)
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [0])
                self.assertTrue(end["completed"])
                self.assertEqual(phases, ["baseline", "chaos", "recovery"])
                self.assertEqual(sum(r["record_type"] == "request" for r in stream.records), 6900)
                self.assertTrue(controller.evidence_copied)
            finally:
                worker.atomic_json(evidence / "stop", {"experiment_id": identifier})
                worker.atomic_json(evidence / "collected", {"experiment_id": identifier})
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
