import unittest

from flow_diagnostic import assert_owned, configuration


class DiagnosticTests(unittest.TestCase):
    def test_scope_is_only_discovered_subnet_and_tcp_enum_is_explicit(self):
        body = configuration({"project": "unit-test-project", "region": "us-central1", "subnet": "unit-test-subnet"})
        self.assertEqual(body["subnet"], "projects/unit-test-project/regions/us-central1/subnetworks/unit-test-subnet")
        self.assertNotIn("network", body)
        self.assertEqual(body["connectionLogging"], "CONNECTION_LOGGING_ENABLED")
        assert_owned(body, body)

    def test_refuses_foreign_owner_and_different_subnet(self):
        body = configuration({"project": "unit-test-project", "region": "us-central1", "subnet": "unit-test-subnet"})
        with self.assertRaisesRegex(ValueError, "no matching owner"):
            assert_owned(dict(body, labels={}), body)
        with self.assertRaisesRegex(ValueError, "different subnet"):
            assert_owned(dict(body, subnet="projects/other/regions/us-central1/subnetworks/shared"), body)

    def test_label_omission_accepts_exact_successful_creation_receipt(self):
        desired = configuration({"project": "unit-test-project", "region": "us-central1", "subnet": "unit-test-subnet"})
        existing = dict(desired, labels={}, name="projects/unit-test-project/locations/global/vpcFlowLogsConfigs/module-c-subnet-diagnostic",
                        createTime="2026-10-18T19:32:26.916286462Z", description="Created by this test")
        receipt = {"done": True, "response": dict(existing)}
        assert_owned(existing, desired, [receipt])
        assert_owned(existing, desired, [dict(existing)])

    def test_receipt_refuses_same_name_new_creation_or_wrong_description(self):
        desired = configuration({"project": "unit-test-project", "region": "us-central1", "subnet": "unit-test-subnet"})
        existing = dict(desired, labels={}, name="projects/unit-test-project/locations/global/vpcFlowLogsConfigs/module-c-subnet-diagnostic",
                        createTime="2026-10-18T19:32:26.916286462Z", description="Created by this test")
        for mismatch in ({"createTime": "2026-10-18T19:40:00Z"}, {"description": "Another actor"}, {"subnet": "another"}):
            with self.assertRaisesRegex(ValueError, "no matching owner"):
                assert_owned(existing, desired, [dict(existing, **mismatch)])
        with self.assertRaisesRegex(ValueError, "no matching owner"):
            assert_owned(existing, desired, [{"done": False, "response": dict(existing)}])


if __name__ == "__main__":
    unittest.main()
