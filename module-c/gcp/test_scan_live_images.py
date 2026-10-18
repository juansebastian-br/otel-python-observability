import copy
import unittest

from scan_live_images import derive_inventory, immutable_reference


def fixture():
    digest = "registry.example/app@sha256:" + "a" * 64
    deployment = {"metadata": {"name": "orders-service", "uid": "dep-uid", "generation": 3},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "image": "registry.example/app:v1"}]}}},
        "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
    replica_set = {"metadata": {"name": "owned-rs", "uid": "rs-uid", "ownerReferences": [
        {"kind": "Deployment", "uid": "dep-uid", "controller": True}]}}
    pod = {"metadata": {"name": "owned-pod", "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-uid", "controller": True}]},
        "spec": {"containers": [{"name": "app", "image": "registry.example/app:v1"}, {"name": "istio-proxy", "image": "sidecar:v1"}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "app", "ready": True, "state": {"running": {}}, "imageID": "docker-pullable://" + digest},
                                  {"name": "istio-proxy", "ready": True, "state": {"running": {}}, "imageID": "ignored-sidecar"}]}}
    return [deployment], [replica_set], [pod]


class LiveInventoryTests(unittest.TestCase):
    def test_resolves_actual_owned_container_excluding_injected_sidecar(self):
        inventory = derive_inventory(*fixture(), names=("orders-service",))
        self.assertEqual(len(inventory[0]["containers"]), 1)
        self.assertEqual(inventory[0]["containers"][0]["immutable_image"], "registry.example/app@sha256:" + "a" * 64)

    def test_rejects_partial_rollout_and_missing_business_service(self):
        deployments, replicas, pods = fixture()
        with self.assertRaisesRegex(ValueError, "Missing business"):
            derive_inventory(deployments, replicas, pods)
        deployments[0]["status"]["readyReplicas"] = 0
        with self.assertRaisesRegex(ValueError, "not stable"):
            derive_inventory(deployments, replicas, pods, names=("orders-service",))

    def test_never_guesses_registry_digest_from_runtime_config_hash(self):
        with self.assertRaisesRegex(ValueError, "refusing to guess"):
            immutable_reference("containerd://sha256:" + "a" * 64, "registry.example/app:v1")

    def test_rejects_unowned_or_unready_pod(self):
        deployments, replicas, pods = fixture()
        pods[0]["metadata"]["ownerReferences"][0]["uid"] = "unrelated-rs"
        with self.assertRaisesRegex(ValueError, "owned pods"):
            derive_inventory(deployments, replicas, pods, names=("orders-service",))
        deployments, replicas, pods = fixture()
        pods[0]["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(ValueError, "not Ready"):
            derive_inventory(deployments, replicas, pods, names=("orders-service",))

    def test_rejects_multiple_digests_even_for_same_mutable_tag(self):
        deployments, replicas, pods = fixture()
        deployments[0]["spec"]["replicas"] = 2
        for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"):
            deployments[0]["status"][field] = 2
        second = copy.deepcopy(pods[0])
        second["status"]["containerStatuses"][0]["imageID"] = "registry.example/app@sha256:" + "b" * 64
        with self.assertRaisesRegex(ValueError, "different digests"):
            derive_inventory(deployments, replicas, pods + [second], names=("orders-service",))


if __name__ == "__main__":
    unittest.main()
