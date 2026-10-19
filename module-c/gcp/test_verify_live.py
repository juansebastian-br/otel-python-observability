from datetime import datetime, timezone
import unittest

from verify_live import cve_summary


class EvidenceTests(unittest.TestCase):
    def test_no_samples_is_unknown_not_zero(self):
        result = cve_summary({}, {}, datetime.now(timezone.utc))
        self.assertEqual(result["latest_counts"], {})
        self.assertIsNone(result["snapshot_age_seconds"])
        self.assertIsNone(result["fresh_under_one_hour"])
        self.assertFalse(result["all_five_severities_observed"])

    def test_fresh_publication_of_old_scan_remains_stale(self):
        now = datetime.fromtimestamp(2000000000, timezone.utc)
        point = {"interval": {"endTime": now.isoformat()}, "value": {"int64Value": str(int(now.timestamp()) - 7200)}}
        result = cve_summary({}, {"timeSeries": [{"points": [point]}]}, now)
        self.assertEqual(result["snapshot_age_seconds"], 7200)
        self.assertFalse(result["fresh_under_one_hour"])


if __name__ == "__main__":
    unittest.main()
