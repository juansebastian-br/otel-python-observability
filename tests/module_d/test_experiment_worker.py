import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "module-d"))
from experiment import worker

ENV = {"POD_NAMESPACE": "observability-chaos-d", "DEPLOYMENT_ENVIRONMENT": "sandbox"}


def document(mode="latency"):
    role, port, path, method = (("payments", 8001, "/payments", "POST") if mode == "latency"
                              else ("data", 8002, "/data/health", "GET"))
    return {"experiment_id": "d-" + mode + "-" + "a" * 32, "mode": mode,
        "target_url": f"http://module-d-{role}.observability-chaos-d.svc:{port}",
        "path": path, "method": method, "body": {"amount": 100} if mode == "latency" else {}}


def request(identifier, index=0, latency=50, success=True, phase="baseline"):
    return {"record_type": "request", "experiment_id": identifier, "phase": phase,
        "request_id": str(index), "success": success,
        "outcome": "http_success" if success else "http_server_error", "latency_ms": latency}


class FakeClock:
    def __init__(self):
        self.value = 0
    def now(self):
        return self.value
    def wait(self, seconds):
        self.value += seconds


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)

    def test_config_binds_exact_role_route_port_and_fixed_load(self):
        for mode in ("latency", "error"):
            value = document(mode)
            config = worker.validate_config(value, ENV)
            self.assertEqual((config.rate, config.max_workers), (5, 20))
            self.assertEqual((config.baseline_seconds, config.chaos_seconds, config.recovery_seconds), (900, 180, 300))
            value["target_url"] = value["target_url"].replace(".svc:", ".svc.cluster.local:")
            worker.validate_config(value, ENV)
        for change in ({"target_url": "http://127.0.0.1:8001"}, {"path": "/health"},
                       {"method": "GET"}, {"target_url": document("error")["target_url"]},
                       {"target_url": document()["target_url"] + "/"}, {"rate": 100},
                       {"body": []}, {"body": {"amount": float("nan")}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                worker.validate_config({**document(), **change}, ENV)

    def test_namespace_and_environment_are_mandatory(self):
        for env in ({}, {**ENV, "POD_NAMESPACE": "observability-lab"},
                    {**ENV, "DEPLOYMENT_ENVIRONMENT": "production"}):
            with self.assertRaises(ValueError):
                worker.validate_config(document(), env)

    def test_json_rejects_duplicate_nonfinite_and_oversized_input(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                worker.strict_json(raw)
        path = self.out / "input.json"
        path.write_text(" " * 65537)
        with self.assertRaises(ValueError):
            worker.read_json(path)

    def test_release_requires_exact_experiment_phase_action_and_no_extra_fields(self):
        identifier = document()["experiment_id"]
        path = self.out / "release-baseline.json"
        expected = {"experiment_id": identifier, "phase": "baseline", "action": "continue"}
        worker.atomic_json(path, expected)
        worker.wait_release(self.out, identifier, "baseline", threading.Event())
        for value in ({**expected, "experiment_id": "other"}, {**expected, "phase": "chaos"},
                      {**expected, "action": "enable"}, {**expected, "extra": True}):
            worker.atomic_json(path, value)
            with self.assertRaises(worker.runner.PhaseTransitionError):
                worker.wait_release(self.out, identifier, "baseline", threading.Event())

    def test_release_timeout_and_stop_abort_before_loading(self):
        clock = FakeClock()
        with self.assertRaises(worker.runner.PhaseTransitionError):
            worker.wait_release(self.out, "one", "baseline", threading.Event(),
                timeout=.4, monotonic=clock.now, wait=clock.wait)
        self.assertEqual(clock.value, .4)
        (self.out / "stop").touch()
        stop = threading.Event()
        with self.assertRaises(worker.runner.PhaseTransitionError):
            worker.wait_release(self.out, "one", "baseline", stop)
        self.assertTrue(stop.is_set())

    def test_baseline_gate_rejects_error_miss_missing_sample_and_latency_boundary(self):
        rows = [request("one", i) for i in range(4500)]
        self.assertEqual(worker.require_healthy_baseline(rows)["N"], 4500)
        invalid = [rows[:-1], rows + [{"record_type": "arrival_missed", "phase": "baseline"}],
            [request("one", 0, success=False)] + rows[1:],
            [request("one", i, latency=200) for i in range(4500)]]
        for records in invalid:
            with self.assertRaises(worker.runner.PhaseTransitionError):
                worker.require_healthy_baseline(records)

    def test_progress_is_atomic_aggregate_after_100_requests(self):
        identifier = document()["experiment_id"]
        output = io.StringIO()
        stream = worker.EvidenceStream(output, self.out, identifier)
        stream.write(json.dumps({"record_type": "phase_start", "experiment_id": identifier, "phase": "baseline"}) + "\n")
        for index in range(100):
            stream.write(json.dumps(request(identifier, index, success=index != 0)) + "\n")
        state = worker.read_json(self.out / "phase-state.json")
        self.assertEqual((state["state"], state["phase"], state["N"], state["F"]), ("running", "baseline", 100, 1))
        self.assertEqual(state["p99_client_duration_ms"], 50)
        self.assertNotIn("body", state)
        self.assertNotIn("headers", state)
        self.assertEqual(len(output.getvalue().splitlines()), 101)
        self.assertEqual(list(self.out.glob("*.tmp")), [])

    def test_collection_ack_must_match_and_wait_is_bounded(self):
        worker.atomic_json(self.out / "collected", {"experiment_id": "other"})
        clock = FakeClock()
        self.assertFalse(worker.wait_for_collection(self.out, "one", timeout=.4,
            monotonic=clock.now, sleep=clock.wait))
        worker.atomic_json(self.out / "collected", {"experiment_id": "one"})
        self.assertTrue(worker.wait_for_collection(self.out, "one", timeout=1))

    def test_existing_evidence_is_preserved(self):
        path = self.out / "events.jsonl"
        path.write_text("original")
        with self.assertRaises(ValueError):
            worker.run_worker(worker.validate_config(document(), ENV), self.out, collection_timeout=0)
        self.assertEqual(path.read_text(), "original")

    def test_failed_run_finishes_and_preserves_actual_run_end_before_collection(self):
        config = worker.validate_config(document(), ENV)
        worker.atomic_json(self.out / "release-baseline.json", {"experiment_id": config.experiment_id,
            "phase": "baseline", "action": "continue"})
        def fail_run(cfg, stream, stop_event, phase_hook):
            stream.write(json.dumps({"record_type": "run_start", "experiment_id": cfg.experiment_id,
                "planned_arrivals": 6900, "timestamp": worker.runner.utc_now()}) + "\n")
            phase_hook("baseline")
            stream.write(json.dumps({"record_type": "phase_start", "experiment_id": cfg.experiment_id,
                "phase": "baseline"}) + "\n")
            stream.write(json.dumps(request(cfg.experiment_id)) + "\n")
            raise ValueError("never include raw exception content")
        def verify_hold(out, identifier, timeout):
            self.assertEqual(timeout, 120)
            state = worker.read_json(out / "phase-state.json")
            self.assertEqual((state["state"], state["phase"]), ("finished", "complete"))
            self.assertFalse(state["summary"]["completed"])
            self.assertEqual(state["summary"]["error_type"], "ValueError")
            rows = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
            self.assertEqual(rows[-1]["record_type"], "run_end")
            self.assertEqual(rows[-1]["attempted"], 1)
            self.assertEqual(rows[-1]["unissued_after_stop"], 6899)
            self.assertNotIn("never include", (out / "worker-summary.json").read_text())
            return True
        with patch.object(worker, "wait_for_collection", side_effect=verify_hold), redirect_stdout(io.StringIO()):
            code = worker.run_worker(config, self.out, run=fail_run)
        self.assertEqual(code, 2)

    def test_chaos_barrier_never_published_for_bad_baseline(self):
        config = worker.validate_config(document(), ENV)
        def rejected(cfg, stream, stop_event, phase_hook):
            stream.write(json.dumps({"record_type": "run_start", "experiment_id": cfg.experiment_id,
                "planned_arrivals": 6900}) + "\n")
            phase_hook("chaos")
            self.fail("invalid baseline advanced")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(worker.run_worker(config, self.out, run=rejected, collection_timeout=0), 2)
        state = worker.read_json(self.out / "phase-state.json")
        self.assertEqual(state["state"], "finished")
        self.assertEqual(state["summary"]["error_type"], "PhaseTransitionError")


if __name__ == "__main__":
    unittest.main()
