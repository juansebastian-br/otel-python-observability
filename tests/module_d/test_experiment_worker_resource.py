import copy
import importlib.util
from pathlib import Path
import unittest

FILE = Path(__file__).resolve().parents[2] / "module-d/experiment/worker_resource.py"
spec = importlib.util.spec_from_file_location("module_d_worker_resource", FILE)
resource = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resource)
IDENTIFIER = "d-latency-" + "a" * 32
IMAGE = "us-central1-docker.pkg.dev/test/apps/payments@sha256:" + "b" * 64
SOURCES = {name: "value = 1\n" for name in resource.SOURCE_NAMES}


class WorkerResourceTests(unittest.TestCase):
    def test_job_is_bounded_internal_and_has_no_credentials_or_rbac(self):
        manifest = resource.build_worker_manifest(IDENTIFIER, IMAGE, SOURCES, {"experiment_id": IDENTIFIER})
        self.assertEqual([item["kind"] for item in manifest["items"]], ["ConfigMap", "ConfigMap", "Job"])
        job = manifest["items"][-1]
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 2400)
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 3600)
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["containers"][0]["image"], IMAGE)
        self.assertEqual(pod["containers"][0]["args"], ["--config", "/config/worker.json", "--out", "/evidence"])
        self.assertEqual(pod["containers"][0]["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(pod["containers"][0]["resources"]["limits"]["cpu"], "1")
        self.assertFalse(any("secret" in volume or "hostPath" in volume for volume in pod["volumes"]))

    def test_rejects_unsafe_identity_mutable_image_and_package_drift(self):
        cases = [("../shared", IMAGE, SOURCES, {}),
            (IDENTIFIER, "python:latest", SOURCES, {"experiment_id": IDENTIFIER}),
            (IDENTIFIER, IMAGE, {**SOURCES, "../escape.py": "pass"}, {"experiment_id": IDENTIFIER}),
            (IDENTIFIER, IMAGE, SOURCES, {"experiment_id": "other"}),
            (IDENTIFIER, IMAGE, SOURCES, {"experiment_id": IDENTIFIER, "namespace": "observability-lab"})]
        for case in cases:
            with self.assertRaises(ValueError):
                resource.build_worker_manifest(*case)

    def test_ready_worker_requires_job_uid_ready_container_and_exact_digest(self):
        job = resource.build_worker_manifest(IDENTIFIER, IMAGE, SOURCES, {"experiment_id": IDENTIFIER})["items"][-1]
        job["metadata"]["uid"] = "job-uid"
        pod = {"metadata": {"name": IDENTIFIER + "-abcde", "namespace": resource.NAMESPACE, "uid": "pod-uid",
            "labels": resource.labels(IDENTIFIER), "ownerReferences": [{"kind": "Job", "uid": "job-uid"}]},
            "spec": job["spec"]["template"]["spec"], "status": {"phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": resource.CONTAINER, "ready": True, "imageID": "docker-pullable://" + IMAGE}]}}
        self.assertEqual(resource.validate_ready_worker(job, [pod])["pod_uid"], "pod-uid")
        for change in ("owner", "digest", "ready", "labels"):
            bad = copy.deepcopy(pod)
            if change == "owner":
                bad["metadata"]["ownerReferences"][0]["uid"] = "foreign"
            elif change == "digest":
                bad["status"]["containerStatuses"][0]["imageID"] = IMAGE[:-64] + "c" * 64
            elif change == "ready":
                bad["status"]["conditions"][0]["status"] = "False"
            else:
                bad["metadata"]["labels"]["managed-by"] = "foreign"
            with self.assertRaises(ValueError):
                resource.validate_ready_worker(job, [bad])


if __name__ == "__main__":
    unittest.main()
