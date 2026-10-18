"""Offline safety/contract checks; no cloud calls or invented deployment evidence."""
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import deploy
import network_canary

TEST_TMP = pathlib.Path(__file__).resolve().parent / ".state" / "test-tmp"
TEST_TMP.mkdir(parents=True, exist_ok=True)


def config():
    # Explicitly synthetic unit-test topology, never used by deployment discovery.
    return {"project": "unit-test-project", "project_number": "123", "parent": None,
        "cluster": "unit-test-cluster", "location": "us-central1", "region": "us-central1",
        "namespace": "observability", "network": "unit-test-network", "subnet": "unit-test-subnet",
        "subnet_id": "123456", "network_cidrs": ["10.0.0.0/24", "10.4.0.0/16"],
        "subnet_before": {"name": "unit-test-subnet", "logConfig": {"enable": False}},
        "autopilot": True, "intranode_visibility": True, "channels": [],
        "unexpected_port": 4444, "flow_records_threshold": 120, "auth_failures_threshold": 4}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = deploy.build(config())

    def metric(self, name):
        return next(m for m in self.plan["metrics"] if m["name"] == "module_c/" + name)

    def test_reporters_prevent_same_flow_double_count(self):
        self.assertIn('jsonPayload.reporter="SRC"', self.metric("ew_flow_records")["filter"])
        self.assertIn('jsonPayload.reporter="SRC"', self.metric("ns_egress_flow_records")["filter"])
        self.assertIn('jsonPayload.reporter="DEST"', self.metric("ns_ingress_flow_records")["filter"])
        for name in ("ew_flow_records", "ns_ingress_flow_records", "ns_egress_flow_records"):
            self.assertIn('resource.labels.subnetwork_id="123456"', self.metric(name)["filter"])

    def test_anomaly_uses_destination_port_and_both_namespaces(self):
        rule = self.metric("interservice_unexpected_port")["filter"]
        self.assertIn("jsonPayload.connection.dest_port=4444", rule)
        self.assertIn('src_gke_details.pod.pod_namespace="observability"', rule)
        self.assertIn('dest_gke_details.pod.pod_namespace="observability"', rule)
        self.assertNotIn("src_port", rule)
        self.assertNotIn("DENY", rule)

    def test_auth_reads_only_structured_stdout(self):
        rule = self.metric("auth_failures")["filter"]
        self.assertIn('log_id("stdout")', rule)
        self.assertIn('jsonPayload.event="authentication_failed"', rule)
        self.assertNotIn("trace_id", self.metric("auth_failures")["labelExtractors"])

    def test_network_backend_is_exclusive_and_uses_explicit_monitoring_resource(self):
        chosen = config()
        chosen.update({"flow_backend": "network-management", "flow_config_name": "unit-test-flow",
                       "flow_metric_resource": "global"})
        plan = deploy.build(chosen)
        network_metrics = [m for m in plan["metrics"] if m["name"] != "module_c/auth_failures"]
        for metric in network_metrics:
            self.assertIn('log_id("networkmanagement.googleapis.com/vpc_flows")', metric["filter"])
            self.assertIn('resource.labels.name="unit-test-flow"', metric["filter"])
            self.assertNotIn("compute.googleapis.com", metric["filter"])
        for policy in plan["policies"]:
            if policy["userLabels"]["module_c_rule"] in ("unexpected_port", "traffic_spike"):
                self.assertIn('resource.type = "global"', policy["conditions"][0]["conditionThreshold"]["filter"])

    def test_network_backend_gate_refuses_foreign_disabled_or_wrong_subnet(self):
        chosen = config()
        chosen.update({"flow_backend": "network-management", "flow_config_name": "unit-test-flow",
                       "flow_metric_resource": "global"})
        existing = {"state": "ENABLED", "metadata": "INCLUDE_ALL_METADATA", "labels": {"managed_by": deploy.OWNER},
            "subnet": "projects/unit-test-project/regions/us-central1/subnetworks/unit-test-subnet"}
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp:
            for changed in ({"state": "DISABLED"}, {"subnet": "foreign"}, {"labels": {}}, {"metadata": "EXCLUDE_ALL_METADATA"}):
                with patch.object(deploy, "api", return_value=dict(existing, **changed)), patch.object(deploy, "gcloud") as cloud:
                    with self.assertRaises(ValueError):
                        deploy.apply(deploy.build(chosen), pathlib.Path(temp))
                    cloud.assert_not_called()
            with patch.object(deploy, "api", return_value=existing):
                self.assertEqual(deploy.verify_flow_backend(chosen, pathlib.Path(temp)), existing)

    def test_snapshot_gauge_and_age_check(self):
        descriptor = next(d for d in self.plan["descriptors"] if d["type"] == deploy.ACTIVE_CVES)
        self.assertEqual(descriptor["metricKind"], "GAUGE")
        self.assertEqual([l["key"] for l in descriptor["labels"]], ["severity"])
        rule = next(p for p in self.plan["policies"] if p["userLabels"]["module_c_rule"] == "cve_missing")
        query = rule["conditions"][0]["conditionPrometheusQueryLanguage"]["query"]
        self.assertIn("time() -", query)
        self.assertIn("> 3600", query)
        self.assertIn("absent_over_time", query)
        self.assertNotIn("or vector(0)", query)

    def test_plan_json_roundtrip_and_real_metric_references(self):
        plan = json.loads(json.dumps(self.plan))
        all_names = {"logging.googleapis.com/user/" + m["name"] for m in plan["metrics"]}
        all_names.update(d["type"] for d in plan["descriptors"])
        for widget in plan["dashboard"]["gridLayout"]["widgets"]:
            for dataset in widget.get("xyChart", {}).get("dataSets", []):
                query = dataset["timeSeriesQuery"]
                if "timeSeriesFilter" in query:
                    self.assertTrue(any(name in query["timeSeriesFilter"]["filter"] for name in all_names))

    def test_apply_preserves_existing_flow_configuration_and_dashboard_v1(self):
        config_with_logs = config()
        config_with_logs["subnet_before"]["logConfig"] = {"enable": True, "flowSampling": 0.5,
            "metadata": "INCLUDE_ALL_METADATA", "filterExpr": "true"}
        plan = deploy.build(config_with_logs)
        api_calls, gcloud_calls = [], []
        def fake_api(method, url, body=None, missing_ok=False):
            api_calls.append((method, url, body))
            if method == "GET":
                return None if missing_ok else {}
            if "/dashboards" in url:
                return {"name": "projects/unit-test-project/dashboards/123"}
            return {}
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp, patch.object(deploy, "api", fake_api), \
             patch.object(deploy, "gcloud", lambda *args: gcloud_calls.append(args)):
            deploy.apply(plan, pathlib.Path(temp))
            backup = json.loads((pathlib.Path(temp) / "subnet-before.json").read_text())
            self.assertEqual(backup["logConfig"]["flowSampling"], 0.5)
        self.assertFalse(any("subnets" in call and "update" in call for call in gcloud_calls))
        self.assertTrue(any("/v1/" in url and "/dashboards" in url for _, url, _ in api_calls))
        self.assertFalse(any("/v3/" in url and "/dashboards" in url for _, url, _ in api_calls))
        descriptor_get_urls = [url for method, url, _ in api_calls
                               if method == "GET" and "/metricDescriptors/" in url]
        self.assertEqual(set(descriptor_get_urls), {
            "https://monitoring.googleapis.com/v3/projects/unit-test-project/metricDescriptors/custom.googleapis.com/security/active_cves",
            "https://monitoring.googleapis.com/v3/projects/unit-test-project/metricDescriptors/custom.googleapis.com/security/cve_snapshot_timestamp"})
        self.assertTrue(all("%2F" not in url for url in descriptor_get_urls))

    def test_apply_rejects_foreign_metrics_before_mutation(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp, \
             patch.object(deploy, "api", return_value={"description": "owned elsewhere"}), \
             patch.object(deploy, "gcloud") as cloud:
            with self.assertRaisesRegex(RuntimeError, "another owner"):
                deploy.apply(self.plan, pathlib.Path(temp))
            cloud.assert_not_called()

    def test_evidence_does_not_overwrite_saved_channels_or_rediscover(self):
        plan = copy.deepcopy(self.plan)
        plan["config"]["channels"] = ["projects/unit-test-project/notificationChannels/approved"]
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp:
            path = pathlib.Path(temp) / "plan.json"
            deploy.save(path, plan)
            original_bytes = path.read_bytes()
            with patch.object(deploy, "discover") as discover, patch.object(deploy, "gcloud", return_value=[]):
                deploy.main(["evidence", "--project", "unit-test-project", "--out", temp])
                discover.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_canary_is_internal_bounded_and_without_service_token(self):
        deployment, service, job = network_canary.manifest("observability", "unit-test-image", 4444)
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 240)
        for workload in (deployment, job):
            self.assertFalse(workload["spec"]["template"]["spec"]["automountServiceAccountToken"])
            self.assertEqual(workload["spec"]["template"]["metadata"]["annotations"]["sidecar.istio.io/inject"], "false")
        self.assertIn("range(240)", job["spec"]["template"]["spec"]["containers"][0]["command"][-1])
        client_code = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        server_code = deployment["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertIn("BODY=b'C'*65536", server_code)
        self.assertIn("r.read(65537)", client_code)
        self.assertIn("canary_requests_started", client_code)
        self.assertIn("canary_requests_completed", client_code)
        self.assertEqual(network_canary.REQUEST_COUNT * network_canary.RESPONSE_BYTES, 15728640)
        compile(server_code, "canary-server", "exec")
        compile(client_code, "canary-client", "exec")


if __name__ == "__main__":
    unittest.main()
