"""Check ownership, secret preservation and sandbox pod security without a cluster."""
import argparse
import base64
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_probe", ROOT / "module-c/security/deploy_probe.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeployProbeTests(unittest.TestCase):
    def setUp(self):
        self.registry = "us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final"
        self.args = argparse.Namespace(image=self.registry + "/module-c-auth-probe:v1", registry=self.registry,
            project="crafty-dynamics-239114", namespace="observability-lab", context="explicit-sandbox-context",
            cluster=None, location=None, otlp_endpoint="otel-collector:4317")

    def test_registry_requires_exact_prefix_and_probe_image(self):
        deployment.validate_image(self.args.image, self.registry, self.args.project)
        for image in (self.registry + "-other/module-c-auth-probe:v1",
                      self.registry + "/orders:v1", self.registry + "/module-c-auth-probe"):
            with self.assertRaises(ValueError):
                deployment.validate_image(image, self.registry, self.args.project)

    def test_unmanaged_collision_stops_before_all_mutations(self):
        commands = []
        def execute(command, body=None):
            commands.append(command)
            if "get" in command and "deployment" in command:
                return json.dumps({"kind": "Deployment", "metadata": {"labels": {}}})
            return ""
        with patch.object(deployment, "execute", side_effect=execute), self.assertRaises(ValueError):
            deployment.deploy(self.args)
        self.assertFalse(any(verb in command for command in commands for verb in ("create", "replace")))

    def test_existing_key_preserved_without_secret_mutation(self):
        writes = []
        key = "preserved-key-at-least-24-bytes"
        secret = {"kind": "Secret", "metadata": {"labels": deployment.LABELS},
                  "data": {"API_KEY": base64.b64encode(key.encode()).decode()}}
        def execute(command, body=None):
            if "get" in command and "secret" in command:
                return json.dumps(secret)
            if body:
                writes.append(body)
            return ""
        with patch.object(deployment, "execute", side_effect=execute):
            result = deployment.deploy(self.args)
        self.assertEqual(result["secret"], "preserved")
        self.assertEqual([obj["kind"] for obj in writes], ["Deployment", "Service"])
        self.assertNotIn(key, json.dumps(result))

    def test_new_key_only_goes_to_secret_stdin(self):
        writes = []
        def execute(command, body=None):
            if body:
                writes.append((command, body))
            return ""
        with patch.object(deployment, "execute", side_effect=execute):
            result = deployment.deploy(self.args)
        secret = writes[0][1]
        key = secret["stringData"]["API_KEY"]
        self.assertGreaterEqual(len(key), 24)
        self.assertEqual(secret["kind"], "Secret")
        self.assertNotIn(key, json.dumps(result))
        self.assertTrue(all(key not in str(command) for command, _ in writes))

    def test_pod_is_nonroot_tokenless_and_sidecar_disabled(self):
        resource, service = deployment.manifests(self.args.namespace, self.args.image, self.args.otlp_endpoint)
        template = resource["spec"]["template"]
        self.assertEqual(template["metadata"]["annotations"]["sidecar.istio.io/inject"], "false")
        pod = template["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["resources"]["requests"], {"cpu": "100m", "memory": "128Mi"})
        self.assertEqual(service["spec"]["type"], "ClusterIP")

    def test_replace_retains_race_checks_and_service_cluster_ip(self):
        _, desired = deployment.manifests(self.args.namespace, self.args.image, self.args.otlp_endpoint)
        existing = copy.deepcopy(desired)
        existing["metadata"].update(resourceVersion="42", uid="unique-object-identity")
        existing["spec"].update(clusterIP="10.0.0.10", clusterIPs=["10.0.0.10"])
        result = deployment.replacement(desired, existing)
        self.assertEqual(result["metadata"]["resourceVersion"], "42")
        self.assertEqual(result["metadata"]["uid"], "unique-object-identity")
        self.assertEqual(result["spec"]["clusterIP"], "10.0.0.10")


if __name__ == "__main__":
    unittest.main()
