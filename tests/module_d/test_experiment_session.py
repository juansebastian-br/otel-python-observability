from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "module-d"))
from experiment import session


def resource_fixture():
    name = "module-d-payments"
    meta = {"name": name, "namespace": session.NS, "uid": "deployment-uid", "generation": 2, "labels": dict(session.LABELS)}
    deployment = {"metadata": meta, "spec": {"replicas": 1, "template": {
        "metadata": {"labels": dict(session.LABELS)}, "spec": {"containers": [{"name": name, "ports": [{"containerPort": 8001}]}]}}},
        "status": {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 2}}
    rs = [{"metadata": {"namespace": session.NS, "uid": "rs-uid", "ownerReferences": [{"kind": "Deployment", "uid": "deployment-uid"}]}}]
    pods = [{"metadata": {"name": "module-d-payments-ready", "uid": "pod-uid", "namespace": session.NS,
        "labels": dict(session.LABELS), "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-uid"}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                   "containerStatuses": [{"name": name, "ready": True, "restartCount": 0}]}}]
    return deployment, rs, pods


def request(index=0, latency=100, outcome="http_success"):
    return {"record_type": "request", "phase": "baseline", "request_id": str(index),
            "outcome": outcome, "latency_ms": latency}


def fault(**changes):
    value = {"event": "chaos.applied", "experiment_id": "test-one", "service_name": "module-d-payments",
             "namespace": session.NS, "mode": "latency", "route": "/payments", "method": "POST",
             "observed_unix_ns": 1767225600123456789, "trace_id": "1" * 32}
    value.update(changes)
    return value


def alert(**changes):
    labels = {"namespace": session.NS, "alertname": "ModuleDLatency", "service": "module-d-payments",
              "route": "/payments", "method": "POST"}
    labels.update(changes)
    return {"labels": labels, "state": "firing", "activeAt": "2026-01-01T00:00:00Z", "status": "firing"}


class SessionTests(unittest.TestCase):
    def test_owned_ready_pod_requires_uid_chain_and_no_rollout(self):
        fixture = resource_fixture()
        selected = session.select_pod(*fixture)
        self.assertEqual(selected["uid"], "pod-uid")
        for mutation in ("labels", "replicas", "owner", "restart-layout", "unready"):
            deployment, rs, pods = copy.deepcopy(fixture)
            if mutation == "labels":
                pods[0]["metadata"]["labels"]["managed-by"] = "other"
            elif mutation == "replicas":
                deployment["status"]["replicas"] = 2
            elif mutation == "owner":
                rs[0]["metadata"]["ownerReferences"][0]["uid"] = "another-deployment"
            elif mutation == "restart-layout":
                deployment["spec"]["template"]["spec"]["containers"].append({"name": "other"})
            else:
                pods[0]["status"]["conditions"][0]["status"] = "False"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                session.select_pod(deployment, rs, pods)

    def test_control_is_atomic_exact_ttl_and_off_has_no_fault_fields(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(session, "CONTROL", str(Path(temp) / "control.json")):
            for enabled, mode in ((True, "latency"), (True, "error"), (False, "latency")):
                with redirect_stdout(io.StringIO()):
                    exec(compile(session.control_script(enabled, "unit-safe", mode), "control", "exec"), {})
                value = json.loads(Path(session.CONTROL).read_text())
                if enabled:
                    self.assertEqual(value["expires_at"] - value["issued_at"], 180)
                    self.assertEqual(set(value), {"enabled", "experiment_id", "mode", "delay_ms", "error_rate", "issued_at", "expires_at"})
                    self.assertEqual(value["delay_ms"], 200 if mode == "latency" else 0)
                    self.assertEqual(value["error_rate"], 0 if mode == "latency" else .1)
                else:
                    self.assertEqual(value, {"enabled": False})
                self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_control_rejects_changed_pod_before_exec(self):
        selected = session.select_pod(*resource_fixture())
        client = object.__new__(session.Kubernetes)
        client.pod = Mock(return_value={**selected, "restart_count": 1})
        client.call = Mock()
        with self.assertRaises(ValueError):
            client.control(selected, True, "unit-safe", "latency")
        client.call.assert_not_called()
        with self.assertRaises(ValueError):
            client.control(selected, True, "unit-safe", "error")

    def test_context_and_project_must_both_match(self):
        for outputs in (("another-project\n",), ("right-project\n", "gke_wrong_us-central1_otel-lab\n")):
            with patch.object(session, "command", side_effect=outputs), self.assertRaises(ValueError):
                session.Kubernetes("right-project", "us-central1", "otel-lab")

    def test_baseline_requires_all_4500_healthy_and_strict_client_p99(self):
        rows = [request(i) for i in range(4500)]
        self.assertEqual(session.baseline_check(rows)["N"], 4500)
        for invalid in (rows[:-1], rows + [{"record_type": "arrival_missed", "phase": "baseline"}],
                        [request(i, 200) for i in range(4500)], [request(0, 100, "timeout")] + rows[1:]):
            with self.assertRaises(session.runner.PhaseTransitionError):
                session.baseline_check(invalid)

    def test_target_health_requires_exact_two_roles_and_up(self):
        value = {"data": {"activeTargets": [{"health": "up", "labels": {"job": "module-d-apps", "sandbox_role": r}} for r in ("payments", "data")]}}
        session.verify_targets(value)
        value["data"]["activeTargets"][0]["health"] = "down"
        with self.assertRaises(ValueError):
            session.verify_targets(value)

    def test_first_fault_uses_actual_nanoseconds_and_exact_route(self):
        events = [fault(observed_unix_ns=1767225601123456789), fault(), fault(route="/wrong", observed_unix_ns=1)]
        first = session.first_chaos(events, "test-one", "module-d-payments", "latency")
        self.assertEqual(session.ns_timestamp(first["observed_unix_ns"]), "2026-01-01T00:00:00.123456789Z")
        with self.assertRaises(ValueError):
            session.first_chaos([fault(trace_id=None)], "test-one", "module-d-payments", "latency")
        self.assertIsNone(session.first_chaos(events, "different-experiment", "module-d-payments", "latency"))

    def test_firing_observation_and_webhook_receipt_are_distinct_from_pending_start(self):
        first = fault()
        observation = {"ended_at": "2026-01-01T00:05:00Z", "first_observed_firing": [
            {"first_observed_firing_at": "2026-01-01T00:00:35Z", "alert": alert()},
            {"first_observed_firing_at": "2026-01-01T00:00:10Z", "alert": alert(route="/other")}]}
        notifications = {"events": [{"received_at": "2026-01-01T00:00:37Z", "alerts": [alert()]},
            {"received_at": "2026-01-01T00:04:00Z", "alerts": [{**alert(), "status": "resolved"}]}]}
        result = session.detection_evidence(first, observation, notifications, "latency")
        self.assertAlmostEqual(result["detection"]["seconds"], 34.876544)
        self.assertEqual(result["detection"]["evidence_source"], "prometheus_firing_observed")
        self.assertAlmostEqual(result["notification"]["delay_seconds"], 36.876544)
        self.assertEqual(result["resolution"]["status"], "observed")

    def test_missing_fault_or_alert_never_becomes_zero_mttd(self):
        missing = session.detection_evidence(None, {}, {}, "latency")
        self.assertIsNone(missing["detection"]["seconds"])
        no_alert = session.detection_evidence(fault(), {"ended_at": "2026-01-01T00:05:00Z"}, {}, "latency")
        self.assertIs(no_alert["detection"]["target_met"], False)
        self.assertIsNone(no_alert["detection"]["seconds"])

    def test_process_cleanup_kills_after_graceful_timeout(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("test", 5), 0]
        session.stop_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_existing_output_is_never_modified_on_rejected_session(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(session, "REPO", Path(temp)):
            out = Path(temp) / ".state" / "keep"
            out.mkdir(parents=True)
            (out / "session.json").write_text("original evidence")
            with patch.object(session, "Kubernetes") as kubernetes, redirect_stdout(io.StringIO()):
                code = session.main(["--project", "right-project", "--mode", "latency", "--out", str(out)])
            self.assertEqual(code, 2)
            kubernetes.assert_not_called()
            self.assertEqual((out / "session.json").read_text(), "original evidence")
            self.assertEqual([p.name for p in out.iterdir()], ["session.json"])
            self.assertFalse((out.parent / "module-d-session.lock").exists())

    def test_preflight_failure_rolls_both_apps_off_and_closes_owned_tunnels(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(session, "REPO", Path(temp)):
            out = Path(temp) / ".state" / "new"
            client = Mock()
            client.pod.side_effect = lambda name: {"name": name, "deployment": name}
            client.control.return_value = {"enabled": False}
            forwards = Mock()
            with patch.object(session, "Kubernetes", return_value=client), patch.object(session, "Forwards", return_value=forwards), \
                 patch.object(session, "wait_preflight", side_effect=ValueError("unhealthy")), redirect_stdout(io.StringIO()):
                code = session.main(["--project", "right-project", "--mode", "error", "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertEqual(client.control.call_count, 4)
            self.assertTrue(all(call.args[1] is False for call in client.control.call_args_list))
            forwards.close.assert_called_once()
            self.assertTrue(json.loads((out / "session.json").read_text())["rollback_confirmed"])


if __name__ == "__main__":
    unittest.main()
