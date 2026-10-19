from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "module-d"))
from experiment import analyze


def request(index, outcome="http_success", latency=20, phase="chaos"):
    status = {"http_success": 200, "http_server_error": 503, "http_client_error": 401}.get(outcome)
    return {"record_type": "request", "experiment_id": "test", "request_id": f"req-{index}",
            "phase": phase, "scheduled_at": "2026-10-19T02:00:00Z", "started_at": "2026-10-19T02:00:00Z",
            "finished_at": "2026-10-19T02:00:01Z", "attempted": True, "status": status,
            "outcome": outcome, "success": outcome == "http_success", "latency_ms": latency}


def evidence(rows, complete=True):
    requests = [r for r in rows if r["record_type"] == "request"]
    missed = len(rows) - len(requests)
    return [{"record_type": "run_start", "experiment_id": "test", "planned_arrivals": len(rows)}] + rows + [
        {"record_type": "run_end", "experiment_id": "test", "completed": complete,
         "attempted": len(requests), "arrival_missed": missed,
         "successful": sum(r["success"] for r in requests)}]


def result(rows, **kwargs):
    return analyze.analyze_records(evidence(rows), experiment_id="test", latency_threshold_ms=200, **kwargs)


class AnalysisTests(unittest.TestCase):
    def test_request_budget_matches_100_failures_in_1000_requests(self):
        rows = [request(i, "http_server_error" if i < 100 else "http_success") for i in range(1000)]
        summary = result(rows)["whole_experiment"]
        self.assertEqual((summary["N"], summary["F"]), (1000, 100))
        self.assertEqual(summary["availability"], 0.9)
        self.assertAlmostEqual(summary["availability_budget"]["allowed_bad_requests"], 5)
        self.assertAlmostEqual(summary["availability_budget"]["consumed_percent"], 2000)
        self.assertEqual(summary["availability_budget"]["remaining_bad_requests"], -95)

    def test_empty_windows_have_unknown_ratios_and_no_false_success(self):
        summary = result([])["whole_experiment"]
        self.assertEqual(summary["N"], 0)
        for key in ("availability", "error_rate", "p99_client_duration_ms", "availability_slo_met", "latency_slo_met"):
            self.assertIsNone(summary[key])
        self.assertIsNone(summary["availability_budget"]["consumed_percent"])
        self.assertFalse(summary["measurement_valid"])

    def test_timeouts_are_failure_and_cannot_be_fast_success(self):
        summary = result([request(1, "timeout", latency=50)])["whole_experiment"]
        self.assertEqual(summary["F"], 1)
        self.assertEqual(summary["latency_bad_requests"], 1)
        self.assertEqual(summary["censored_duration_requests"], 1)
        self.assertIsNone(summary["p99_http_response_duration_ms"])
        self.assertFalse(summary["availability_slo_met"])

    def test_missed_arrivals_invalidate_comparison_without_inventing_http_requests(self):
        missed = {"record_type": "arrival_missed", "experiment_id": "test", "request_id": "skip",
                  "phase": "chaos", "scheduled_at": "2026-10-19T02:00:00Z", "attempted": False}
        summary = result([request(1), missed])["whole_experiment"]
        self.assertEqual((summary["N"], summary["F"], summary["arrival_missed"]), (1, 0, 1))
        self.assertEqual(summary["offered_success_ratio"], 0.5)
        self.assertFalse(summary["measurement_valid"])
        self.assertIsNone(summary["availability_slo_met"])

    def test_p99_is_empirical_and_phases_are_not_merged_for_phase_results(self):
        rows = [request(i, latency=i + 1, phase="baseline") for i in range(100)] + [request(100, latency=400)]
        summary = result(rows)
        self.assertEqual(summary["phases"]["baseline"]["p99_client_duration_ms"], 99)
        self.assertEqual(summary["phases"]["chaos"]["p99_client_duration_ms"], 400)
        self.assertEqual(summary["whole_experiment"]["N"], 101)

    def test_latency_boundary_is_inclusive_and_combined_failures_do_not_double_count(self):
        summary = result([request(1, latency=200), request(2, "http_server_error", latency=201)])["whole_experiment"]
        self.assertEqual(summary["latency_bad_requests"], 1)
        self.assertEqual(summary["combined_bad_requests_without_double_count"], 1)

    def test_unexpected_401_is_failure_and_invalid_experiment_not_availability_success(self):
        summary = result([request(1, "http_client_error")])["whole_experiment"]
        self.assertEqual(summary["F"], 1)
        self.assertFalse(summary["measurement_valid"])

    def test_missing_end_marks_partial_evidence_not_complete(self):
        records = evidence([request(1)])[:-1]
        summary = analyze.analyze_records(records, experiment_id="test", latency_threshold_ms=200)
        self.assertFalse(summary["run_completed"])
        self.assertIsNone(summary["whole_experiment"]["availability_slo_met"])

    def test_duplicate_id_and_false_status_success_are_rejected(self):
        with self.assertRaises(ValueError):
            result([request(1), request(1)])
        row = request(1)
        row["status"] = 503
        with self.assertRaises(ValueError):
            result([row])

    def test_no_incident_timing_is_inferred_from_load_phases(self):
        self.assertEqual(result([request(1)])["detection"]["status"], "not_measured")

    def test_detection_uses_observed_firing_and_strict_less_than_120_seconds(self):
        kwargs = dict(first_chaos_applied_at="2026-10-19T02:00:00Z",
                      evidence_source="prometheus_firing_observed")
        self.assertTrue(analyze.analyze_detection(alert_firing_at="2026-10-19T02:01:59.999Z", **kwargs)["target_met"])
        self.assertFalse(analyze.analyze_detection(alert_firing_at="2026-10-19T02:02:00Z", **kwargs)["target_met"])
        with self.assertRaises(ValueError):
            analyze.analyze_detection(alert_firing_at="2026-10-19T01:59:00Z", **kwargs)
        with self.assertRaises(ValueError):
            analyze.analyze_detection(first_chaos_applied_at="2026-10-19T02:00:00Z", alert_firing_at="2026-10-19T02:01:00Z")

    def test_no_detection_and_insufficient_observation_remain_distinct(self):
        kwargs = dict(first_chaos_applied_at="2026-10-19T02:00:00Z")
        short = analyze.analyze_detection(observed_until="2026-10-19T02:00:30Z", **kwargs)
        long = analyze.analyze_detection(observed_until="2026-10-19T02:03:00Z", **kwargs)
        self.assertIsNone(short["target_met"])
        self.assertFalse(long["target_met"])
        self.assertIsNone(long["seconds"])


if __name__ == "__main__":
    unittest.main()
