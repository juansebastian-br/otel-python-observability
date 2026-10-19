import copy
import importlib.util
import json
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[2] / "module-d/gcp"
spec = importlib.util.spec_from_file_location("module_d_stop", BASE / "stop.py")
stop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stop)


def inventory():
    def meta(name):
        return {"name": name, "namespace": stop.NS, "uid": name + "-uid", "labels": dict(stop.deploy.LABELS)}
    ns = meta(stop.NS)
    deployments = [{"metadata": meta(name), "spec": {"replicas": 1}} for name in sorted(stop.EXPECTED)]
    sets, pods = [], []
    for name in stop.APPLICATIONS:
        rs = meta(name + "-rs")
        rs["ownerReferences"] = [{"kind": "Deployment", "uid": name + "-uid"}]
        sets.append({"metadata": rs})
        pod = meta(name + "-pod")
        pod["ownerReferences"] = [{"kind": "ReplicaSet", "uid": rs["uid"]}]
        pods.append({"metadata": pod, "spec": {"containers": [{"name": name}]},
                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}})
    return ns, deployments, sets, pods, []


class StopTests(unittest.TestCase):
    def test_preflight_rejects_foreign_resources_partial_inventory_and_wrong_pod_owner(self):
        good = inventory()
        self.assertEqual(len(stop.check_inventory(*good)), 2)
        cases = []
        bad = copy.deepcopy(good); bad[1][0]["metadata"]["labels"] = {}; cases.append(bad)
        bad = copy.deepcopy(good); bad[1].pop(); cases.append(bad)
        bad = copy.deepcopy(good); bad[3][0]["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"; cases.append(bad)
        bad = copy.deepcopy(good); bad[4].append(("Secret", {"name": "foreign", "namespace": stop.NS, "labels": {}})); cases.append(bad)
        bad = copy.deepcopy(good); bad[0]["name"] = "observability-lab"; cases.append(bad)
        for bad in cases:
            with self.assertRaises(ValueError):
                stop.check_inventory(*bad)

    def test_controls_are_disabled_before_scaling_apps_then_remaining_deployments(self):
        source = inventory()
        controls = stop.check_inventory(*source)
        class FakeClient:
            def __init__(self):
                self.calls = []
            def get(self, kind, name=None, namespace=None, metadata_only=False):
                if name:
                    items = source[3] if kind == "pod" else source[1]
                    return next(item["metadata"] for item in items if item["metadata"]["name"] == name)
                return {"items": [{**item, "spec": {"replicas": 0}} for item in source[1]]}
            def call(self, args):
                self.calls.append(args)
                return json.dumps({"chaos_control_disabled": True}) if args[0] == "exec" else "scaled"
        client = FakeClient()
        receipt = {"deployments_before": [{"name": d["metadata"]["name"], "uid": d["metadata"]["uid"]} for d in source[1]], "actions": []}
        stop.execute(client, controls, receipt, lambda: None)
        self.assertEqual([c[0] for c in client.calls], ["exec", "exec"] + ["scale"] * 8)
        self.assertEqual([c[2] for c in client.calls if c[0] == "scale"], list(stop.APPLICATIONS + stop.OTHERS))
        self.assertFalse(any("delete" in c or "observability-lab" in c for c in client.calls))
        self.assertEqual(receipt["status"], "SCALED_ZERO_POD_TERMINATION_NOT_YET_VERIFIED")

    def test_not_ready_pods_are_not_executed_and_uid_change_stops_before_write(self):
        source = inventory()
        source[3][0]["status"]["conditions"][0]["status"] = "False"
        controls = stop.check_inventory(*source)
        self.assertEqual(len(controls), 1)
        class ChangedIdentity:
            def get(self, *args, **kwargs):
                return {"uid": "new-pod-uid", "labels": dict(stop.deploy.LABELS)}
            def call(self, *args, **kwargs):
                raise AssertionError("No mutation allowed after identity mismatch")
        receipt = {"actions": [], "deployments_before": []}
        with self.assertRaises(ValueError):
            stop.execute(ChangedIdentity(), controls, receipt, lambda: None)
        self.assertEqual(receipt["status"], "STOP_STARTED_NOT_COMPLETED")

    def test_allows_only_terminal_owned_worker_jobs_and_rejects_active_worker_before_mutation(self):
        source = inventory()
        identifier = "d-error-" + "a" * 32
        labels = {**stop.deploy.LABELS, "module-d-component": "load-generator", "module-d-experiment": identifier}
        job = {"metadata": {"name": identifier, "namespace": stop.NS, "uid": "worker-job", "labels": labels},
               "status": {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}}
        pod = {"metadata": {"name": identifier + "-abcde", "namespace": stop.NS, "uid": "worker-pod", "labels": labels,
                "ownerReferences": [{"kind": "Job", "uid": "worker-job"}]}, "status": {"phase": "Succeeded"}}
        source[3].append(pod)
        self.assertEqual(len(stop.check_inventory(*source, jobs=[job])), 2)
        for change in ("active", "unowned", "pod_running", "unknown_job"):
            changed_job, changed_source = copy.deepcopy(job), copy.deepcopy(source)
            if change == "active":
                changed_job["status"] = {"active": 1}
            elif change == "unowned":
                changed_job["metadata"]["labels"]["managed-by"] = "foreign"
            elif change == "pod_running":
                changed_source[3][-1]["status"]["phase"] = "Running"
            else:
                changed_job["metadata"]["name"] = "unrelated-job"
            with self.assertRaises(ValueError):
                stop.check_inventory(*changed_source, jobs=[changed_job])


if __name__ == "__main__":
    unittest.main()
