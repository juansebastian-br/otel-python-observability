import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("module_d_gcp_deploy", REPO / "module-d/gcp/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
IMAGE = "us-central1-docker.pkg.dev/test/apps/payments@sha256:" + "a" * 64
DATA_SOURCE = '''engines = {name: db_engine(prefix) for name, prefix in (("gcp", "GCP_DB"), ("aws", "AWS_DB"))}
engines = {k: v for k, v in engines.items() if v is not None}
if set(engines) != {"gcp", "aws"}:
    raise HTTPException(status_code=503, detail={"error": "Both GCP and AWS connections must be configured", "configured": list(engines)})
span.set_attribute("cloud.provider", "gcp" if name == "gcp" else "aws")
'''


def sources():
    return {"payments": {"main.py": "app = object()\n", "otel.py": "value = 1\n"},
            "data": {"main.py": deploy.adapt_data_source(DATA_SOURCE), "otel.py": "value = 2\n"}}


def source_objects():
    deployment = {"metadata": {"name": "payments-service", "namespace": deploy.SOURCE_NS, "uid": "deployment-uid"},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "payments", "image": "payments:v1"}, {"name": "istio-proxy", "image": "proxy:v1"}]}}},
        "status": {"readyReplicas": 1}}
    rs = {"metadata": {"uid": "rs-uid", "ownerReferences": [{"kind": "Deployment", "uid": "deployment-uid"}]}}
    pod = {"metadata": {"name": "payments-pod", "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-uid"}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "payments", "image": "payments:v1", "imageID": "docker-pullable://" + IMAGE, "ready": True}]}}
    return deployment, [rs], [pod]


class DeployTests(unittest.TestCase):
    def test_reviewed_data_adaptation_is_exact_and_fails_closed(self):
        result = deploy.adapt_data_source(DATA_SOURCE)
        self.assertNotIn("AWS_DB", result)
        self.assertIn('if set(engines) != {"gcp"}:', result)
        self.assertIn('span.set_attribute("cloud.provider", "gcp")', result)
        for changed in (DATA_SOURCE.replace('"AWS_DB"', '"AWS_DATABASE"'), DATA_SOURCE * 2,
                        DATA_SOURCE + '\nother = "AWS_DB"\n'):
            with self.assertRaises(ValueError):
                deploy.adapt_data_source(changed)

    def test_source_requires_ownership_readiness_and_one_digest(self):
        objects = source_objects()
        self.assertEqual(deploy.choose_source(*objects)["image"], IMAGE)
        cases = []
        changed = copy.deepcopy(objects)
        changed[1][0]["metadata"]["ownerReferences"][0]["uid"] = "unrelated-deployment"
        cases.append(changed)
        changed = copy.deepcopy(objects)
        changed[2][0]["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "a" * 64
        cases.append(changed)
        changed = copy.deepcopy(objects)
        changed[2][0]["status"]["conditions"][0]["status"] = "False"
        cases.append(changed)
        changed = copy.deepcopy(objects)
        changed[0]["spec"]["replicas"] = 2
        changed[0]["status"]["readyReplicas"] = 2
        second = copy.deepcopy(changed[2][0])
        second["status"]["containerStatuses"][0]["imageID"] = IMAGE[:-64] + "b" * 64
        changed[2].append(second)
        cases.append(changed)
        for case in cases:
            with self.assertRaises(ValueError):
                deploy.choose_source(*case)

    def test_manifest_has_isolated_real_source_and_no_secret_values(self):
        manifest, monitoring = deploy.build({"payments": IMAGE, "data": IMAGE}, sources())
        deploy.validate_manifest(manifest)
        objects = {(x["kind"], x["metadata"]["name"]): x for x in manifest["items"]}
        self.assertNotIn("Secret", {x["kind"] for x in manifest["items"]})
        self.assertTrue(all(x["metadata"]["namespace"] == deploy.NS for x in manifest["items"]))
        for role, port in (("payments", 8001), ("data", 8002)):
            pod = objects["Deployment", "module-d-" + role]["spec"]["template"]["spec"]
            container = pod["containers"][0]
            env = {x["name"]: x for x in container["env"]}
            self.assertEqual(container["image"], IMAGE)
            self.assertIn("module_d_runtime.entrypoint:app", container["args"])
            self.assertEqual(container["workingDir"], "/opt/module-d")
            self.assertEqual(env["PYTHONPATH"]["value"], "/opt/module-d:/app")
            self.assertEqual(env["GCP_DB_HOST"]["value"], "module-d-postgres")
            self.assertEqual(env["GCP_DB_DATABASE"]["valueFrom"]["secretKeyRef"]["key"], "database")
            self.assertEqual(env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"], "module-d-db")
            self.assertEqual(container["readinessProbe"]["tcpSocket"]["port"], port)
            self.assertEqual(pod["volumes"][-1]["name"], "chaos")
            self.assertRegex(objects["Deployment", "module-d-" + role]["spec"]["template"]["metadata"]["annotations"]["module-d/config-sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(monitoring["prometheus"]["scrape_configs"][0]["metrics_path"], "/__module_d/metrics")
        postgres = objects["Deployment", "module-d-postgres"]["spec"]["template"]["spec"]
        self.assertEqual(postgres["securityContext"]["fsGroup"], 70)
        self.assertIn("emptyDir", postgres["volumes"][0])
        self.assertIn("CREATE TABLE IF NOT EXISTS payments", objects["ConfigMap", "module-d-db-init"]["data"]["init.sql"])

    def test_network_policy_has_no_shared_namespace_or_metadata_http_egress(self):
        manifest, _ = deploy.build({"payments": IMAGE, "data": IMAGE}, sources())
        policies = {x["metadata"]["name"]: x["spec"] for x in manifest["items"] if x["kind"] == "NetworkPolicy"}
        self.assertEqual(set(policies["module-d-default-deny"]["policyTypes"]), {"Ingress", "Egress"})
        allow = policies["module-d-internal-and-dns"]
        self.assertEqual(allow["egress"][0]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], deploy.NS)
        self.assertEqual(allow["egress"][1]["ports"], [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}])
        self.assertNotIn(deploy.SOURCE_NS, json.dumps(allow))
        self.assertNotIn("0.0.0.0/0", json.dumps(allow))

    def test_validation_rejects_cross_namespace_privilege_and_load_balancers(self):
        manifest, _ = deploy.build({"payments": IMAGE, "data": IMAGE}, sources())
        for mutation in ("namespace", "privilege", "token", "lb", "owner"):
            bad = copy.deepcopy(manifest)
            if mutation == "namespace":
                bad["items"][0]["metadata"]["namespace"] = deploy.SOURCE_NS
            elif mutation == "owner":
                bad["items"][0]["metadata"]["labels"] = {}
            elif mutation == "lb":
                next(x for x in bad["items"] if x["kind"] == "Service")["spec"]["type"] = "LoadBalancer"
            else:
                pod = next(x for x in bad["items"] if x["kind"] == "Deployment")["spec"]["template"]["spec"]
                if mutation == "token":
                    pod["automountServiceAccountToken"] = True
                else:
                    pod["containers"][0]["securityContext"]["capabilities"]["add"] = ["NET_ADMIN"]
            with self.assertRaises(ValueError):
                deploy.validate_manifest(bad)

    def test_preflight_refuses_adopting_resources_and_reuses_owned_secret(self):
        class FakeClient:
            def __init__(self, labels):
                self.labels = labels
            def get(self, kind, name=None, namespace=None, metadata_only=False):
                self.assert_metadata_only = metadata_only
                return {"labels": self.labels}
        manifest = {"items": [{"kind": "ConfigMap", "metadata": {"name": "module-d-runtime"}}]}
        self.assertTrue(deploy.preflight_resources(FakeClient(deploy.LABELS), manifest))
        with self.assertRaises(ValueError):
            deploy.preflight_resources(FakeClient({"managed-by": "somebody-else"}), manifest)

    def test_secret_is_random_and_command_errors_do_not_leak_payload(self):
        first, second = deploy.new_secret(), deploy.new_secret()
        deploy.validate_manifest(first)
        self.assertNotEqual(first["stringData"]["password"], second["stringData"]["password"])
        self.assertIn("module-d-postgres:5432/module_d", first["stringData"]["url"])
        completed = subprocess.CompletedProcess(["kubectl"], 1, "", "Secret contains SENSITIVE-PASSWORD")
        with patch.object(deploy.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as error:
                deploy.run(["kubectl", "apply", "-f", "-"], payload="SENSITIVE-PASSWORD")
        self.assertNotIn("SENSITIVE", str(error.exception))

    def test_context_validation_never_silently_switches_projects(self):
        with patch.object(deploy, "run", side_effect=["wrong-project\n"]):
            with self.assertRaises(ValueError):
                deploy.Client("expected-project")
        with patch.object(deploy, "run", side_effect=["expected-project\n", "gke_expected-project_other_other\n"]):
            with self.assertRaises(ValueError):
                deploy.Client("expected-project")
        with patch.object(deploy, "run", side_effect=["expected-project\n", "gke_expected-project_us-central1_otel-lab\n"]):
            self.assertEqual(deploy.Client("expected-project").context, "gke_expected-project_us-central1_otel-lab")


if __name__ == "__main__":
    unittest.main()
