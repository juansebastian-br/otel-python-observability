"""Analyze observed requests and explicitly supplied incident evidence."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

PHASES = ("baseline", "chaos", "recovery")
HTTP_OUTCOMES = {"http_success": (200, 300), "http_redirect": (300, 400),
                 "http_client_error": (400, 500), "http_server_error": (500, 600)}
OTHER_OUTCOMES = {"timeout", "transport_error", "response_limit_exceeded", "invalid_http_status"}
EVIDENCE_SOURCES = ("cloud_monitoring_incident", "alertmanager_firing_observed", "prometheus_firing_observed")


def parse_timestamp(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("an actual RFC3339 timestamp is required")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc)


def p99_nearest_rank(values):
    if not values:
        return None
    return sorted(values)[math.ceil(0.99 * len(values)) - 1]


def budget(N, bad, target):
    allowed = (1 - target) * N
    return {"target": target, "allowed_bad_requests": round(allowed, 12),
            "observed_bad_requests": bad, "remaining_bad_requests": round(allowed - bad, 12),
            "consumed_percent": round(bad / allowed * 100, 8) if N else None}


def validate_request(row):
    if row.get("attempted") is not True:
        raise ValueError("a request must explicitly be an attempted arrival")
    outcome, status = row.get("outcome"), row.get("status")
    if outcome in HTTP_OUTCOMES:
        low, high = HTTP_OUTCOMES[outcome]
        if isinstance(status, bool) or not isinstance(status, int) or not low <= status < high:
            raise ValueError("HTTP outcome contradicts the observed status")
    elif outcome not in OTHER_OUTCOMES:
        raise ValueError("unknown request outcome")
    if row.get("success") is not (outcome == "http_success"):
        raise ValueError("success contradicts the observed outcome")
    value = row.get("latency_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("each request needs a finite nonnegative client duration")
    start, end = parse_timestamp(row["started_at"]), parse_timestamp(row["finished_at"])
    if end < start:
        raise ValueError("request end precedes its start")


def summarize_window(requests, missed, *, availability_slo, latency_slo, latency_threshold_ms, completed):
    N = len(requests)
    successes = sum(row["outcome"] == "http_success" for row in requests)
    F = N - successes
    censored = {"timeout", "transport_error", "response_limit_exceeded"}
    # SLI: an observed response before T. A failed transport has no known
    # response latency, so it cannot be credited as a fast response.
    slow = [row["latency_ms"] > latency_threshold_ms or row["outcome"] in censored for row in requests]
    latency_bad = sum(slow)
    combined_bad = sum(row["outcome"] != "http_success" or bad for row, bad in zip(requests, slow))
    outcome_counts = Counter(row["outcome"] for row in requests)
    issues = []
    if N == 0:
        issues.append("no_attempted_requests")
    if not completed:
        issues.append("run_not_complete")
    if missed:
        issues.append("arrival_schedule_not_met")
    if any(outcome_counts.get(outcome, 0) for outcome in ("http_client_error", "http_redirect", "invalid_http_status")):
        issues.append("unexpected_http_contract_result")
    if outcome_counts.get("response_limit_exceeded", 0):
        issues.append("response_evidence_truncated")
    valid = not issues
    offered = N + missed
    response_latencies = [row["latency_ms"] for row in requests if row["outcome"] in HTTP_OUTCOMES]
    return {
        "N": N, "F": F, "successful_requests": successes, "arrival_missed": missed,
        "offered_arrivals_observed": offered, "offered_failure_count": F + missed,
        "offered_success_ratio": successes / offered if offered else None,
        "availability": successes / N if N else None,
        "error_rate": F / N if N else None,
        "latency_threshold_ms": latency_threshold_ms, "latency_bad_requests": latency_bad,
        "latency_good_ratio": (N - latency_bad) / N if N else None,
        "p99_client_duration_ms": p99_nearest_rank([row["latency_ms"] for row in requests]),
        "p99_http_response_duration_ms": p99_nearest_rank(response_latencies),
        "censored_duration_requests": sum(outcome_counts.get(k, 0) for k in censored),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "availability_budget": budget(N, F, availability_slo),
        "latency_budget": budget(N, latency_bad, latency_slo),
        "combined_bad_requests_without_double_count": combined_bad,
        "measurement_valid": valid, "measurement_issues": issues,
        "availability_slo_met": (successes / N >= availability_slo) if valid else None,
        "latency_slo_met": ((N - latency_bad) / N >= latency_slo) if valid else None,
    }


def analyze_detection(*, first_chaos_applied_at=None, alert_firing_at=None, observed_until=None,
                      evidence_source=None, target_seconds=120):
    if not math.isfinite(target_seconds) or target_seconds <= 0:
        raise ValueError("detection target must be positive")
    if first_chaos_applied_at is None:
        if alert_firing_at is not None or observed_until is not None:
            raise ValueError("the first actual chaos.applied event is required")
        return {"status": "not_measured", "seconds": None, "target_met": None,
                "reason": "actual chaos.applied and incident firing evidence were not supplied"}
    first = parse_timestamp(first_chaos_applied_at)
    until = parse_timestamp(observed_until) if observed_until else None
    if until is not None and until < first:
        raise ValueError("observation end precedes the first applied fault")
    base = {"first_chaos_applied_at": first_chaos_applied_at, "target_seconds": target_seconds,
            "comparison": "strictly_less_than", "scope": "one experiment; not a multi-incident mean"}
    if alert_firing_at is None:
        elapsed = (until - first).total_seconds() if until is not None else None
        return {**base, "status": "not_detected_within_observation" if until else "observation_incomplete",
                "seconds": None, "observed_until": observed_until, "observed_seconds": elapsed,
                "target_met": False if elapsed is not None and elapsed >= target_seconds else None}
    if evidence_source not in EVIDENCE_SOURCES:
        raise ValueError("identify actual Cloud Monitoring opening or observed Alertmanager firing")
    firing = parse_timestamp(alert_firing_at)
    if firing < first or (until is not None and firing > until):
        raise ValueError("incident firing timestamp contradicts the evidence window")
    elapsed = (firing - first).total_seconds()
    return {**base, "status": "observed", "alert_firing_at": alert_firing_at,
            "evidence_source": evidence_source, "timestamp_provenance": "supplied actual evidence; not inferred from the load phase",
            "seconds": round(elapsed, 6), "target_met": elapsed < target_seconds}


def analyze_records(records, *, experiment_id, latency_threshold_ms, availability_slo=0.995,
                    latency_slo=0.99, detection=None):
    for target in (availability_slo, latency_slo):
        if not math.isfinite(target) or not 0 < target < 1:
            raise ValueError("SLO targets must be fractions strictly between 0 and 1")
    if not math.isfinite(latency_threshold_ms) or latency_threshold_ms <= 0:
        raise ValueError("an agreed positive latency threshold in milliseconds is required")
    starts = [row for row in records if row.get("record_type") == "run_start"]
    ends = [row for row in records if row.get("record_type") == "run_end"]
    if len(starts) != 1 or len(ends) > 1:
        raise ValueError("an evidence file must contain exactly one run_start and at most one run_end")
    if any(row.get("experiment_id") != experiment_id for row in records):
        raise ValueError("mixed or unexpected experiment IDs")
    requests, misses, seen = [], [], set()
    for row in records:
        kind = row.get("record_type")
        if kind in ("request", "arrival_missed"):
            if row.get("phase") not in PHASES or not row.get("request_id") or row["request_id"] in seen:
                raise ValueError("invalid phase or duplicate/missing request ID")
            seen.add(row["request_id"])
            parse_timestamp(row["scheduled_at"])
            if kind == "request":
                validate_request(row)
                requests.append(row)
            else:
                if row.get("attempted") is not False:
                    raise ValueError("a missed arrival must not be reported as an attempted request")
                misses.append(row)
        elif kind not in ("run_start", "run_end", "phase_start", "phase_end", "phase_transition"):
            raise ValueError("unknown evidence record type")
    planned = starts[0].get("planned_arrivals")
    if isinstance(planned, bool) or not isinstance(planned, int) or planned < len(seen):
        raise ValueError("invalid planned arrival count")
    completed = bool(ends and ends[0].get("completed") is True and planned == len(seen))
    if ends:
        end = ends[0]
        if (end.get("attempted") != len(requests) or end.get("arrival_missed") != len(misses)
                or end.get("successful") != sum(row["success"] for row in requests)):
            raise ValueError("run_end totals contradict request evidence")
    options = dict(availability_slo=availability_slo, latency_slo=latency_slo,
                   latency_threshold_ms=latency_threshold_ms, completed=completed)
    return {
        "schema_version": 1, "experiment_id": experiment_id, "run_completed": completed,
        "unissued_after_stop": planned - len(seen),
        "definitions": {
            "N": "attempted requests with observed client outcomes; excludes unissued arrivals",
            "F": "all non-2xx or failed transport outcomes; unexpected 3xx/4xx invalidate comparison",
            "latency_good": "observed non-censored response at or below the agreed threshold (inclusive histogram boundary)",
            "p99": "empirical nearest-rank client durations; censored transport durations identified separately",
            "budget": "request-based budget of this exact window, not a monthly budget",
            "phases": "planned load windows; actual injection must be established from chaos.applied logs",
            "trace_header": "a matching response ID does not prove backend trace ingestion",
        },
        "phases": {phase: summarize_window([r for r in requests if r["phase"] == phase],
                    sum(r["phase"] == phase for r in misses), **options) for phase in PHASES},
        "whole_experiment": summarize_window(requests, len(misses), **options),
        "detection": analyze_detection(**(detection or {})),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--latency-threshold-ms", type=float, required=True)
    parser.add_argument("--availability-slo", type=float, default=0.995)
    parser.add_argument("--latency-slo", type=float, default=0.99)
    parser.add_argument("--first-chaos-applied-at")
    parser.add_argument("--alert-firing-at")
    parser.add_argument("--observed-until")
    parser.add_argument("--evidence-source", choices=EVIDENCE_SOURCES)
    args = parser.parse_args(argv)
    try:
        records = [json.loads(line) for line in args.events.read_text(encoding="utf-8").splitlines() if line.strip()]
        detection = {"first_chaos_applied_at": args.first_chaos_applied_at, "alert_firing_at": args.alert_firing_at,
                     "observed_until": args.observed_until, "evidence_source": args.evidence_source}
        result = analyze_records(records, experiment_id=args.experiment_id,
                                 latency_threshold_ms=args.latency_threshold_ms,
                                 availability_slo=args.availability_slo, latency_slo=args.latency_slo, detection=detection)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"output": str(args.output), "experiment_id": args.experiment_id,
                          "run_completed": result["run_completed"], "detection": result["detection"]["status"]}))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Invalid evidence, parameters or output ({type(exc).__name__}); no result inferred.\n")


if __name__ == "__main__":
    raise SystemExit(main())
