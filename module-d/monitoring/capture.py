"""Observe real local port-forwards. Evidence only; never injects alerts or chaos."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def local_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise argparse.ArgumentTypeError("Use a plain local HTTP port-forward URL without credentials/path/query")
    return value.rstrip("/")


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch(base, path):
    with urllib.request.urlopen(base + path, timeout=4) as response:
        return json.load(response)


def capture(args):
    if not 1 <= args.seconds <= 3600 or not 1 <= args.poll_seconds <= 10:
        raise ValueError("Duration 1..3600 seconds and poll interval 1..10 seconds required")
    args.out.mkdir(parents=True, exist_ok=False)
    start = utc()
    deadline = time.monotonic() + args.seconds
    stop_file = getattr(args, "stop_file", None)
    stop_reason = "duration_completed"
    first_firing, failures = {}, 0
    def save(name, value):
        (args.out / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    save("observation.json", {"started_at": start, "planned_seconds": args.seconds,
        "poll_seconds": args.poll_seconds, "status": "OBSERVING", "mttd_seconds": None,
        "note": "Prometheus activeAt / Alertmanager startsAt may be pending times; first_observed_firing_at and webhook received_at are observed upper bounds"})
    print(json.dumps({"event": "observation_started", "at": start, "out": str(args.out)}), flush=True)
    try:
        for name, path in (("targets-before", "/api/v1/targets"), ("rules-before", "/api/v1/rules")):
            save(name + ".json", fetch(args.prometheus, path))
        with (args.out / "polls.jsonl").open("w", encoding="utf-8") as output:
            while time.monotonic() < deadline:
                if stop_file is not None and stop_file.exists():
                    stop_reason = "stop_file_observed"
                    break
                sample = {"observed_at": utc()}
                for name, base, path in (("prometheus", args.prometheus, "/api/v1/alerts"),
                    ("alertmanager", args.alertmanager, "/api/v2/alerts"), ("receiver", args.receiver, "/events")):
                    try:
                        sample[name] = fetch(base, path)
                        sample[name + "_received_at"] = utc()
                    except Exception as exc:
                        sample[name] = {"error_type": type(exc).__name__}
                        failures += 1
                for alert in sample.get("prometheus", {}).get("data", {}).get("alerts", []):
                    if alert.get("state") == "firing":
                        key = json.dumps(alert["labels"], sort_keys=True) + str(alert.get("activeAt"))
                        if key not in first_firing:
                            first_firing[key] = {"first_observed_firing_at": sample["prometheus_received_at"], "alert": alert}
                            print(json.dumps({"event": "first_observed_firing", **first_firing[key]}), flush=True)
                output.write(json.dumps(sample, separators=(",", ":")) + "\n")
                output.flush()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(args.poll_seconds, remaining))
    except BaseException:
        stop_reason = "capture_failed_or_interrupted"
        raise
    finally:
        end = utc()
        save("observation.json", {"started_at": start, "ended_at": end, "planned_seconds": args.seconds,
            "poll_seconds": args.poll_seconds, "api_errors": failures, "first_observed_firing": list(first_firing.values()),
            "stop_reason": stop_reason,
            "mttd_seconds": None, "note": "Correlate first chaos.applied UTC from server logs with firing observation / webhook receipt. No alerts is not zero MTTD."})
        for name, base, path in (("receiver-final", args.receiver, "/events"), ("rules-final", args.prometheus, "/api/v1/rules")):
            try:
                save(name + ".json", fetch(base, path))
            except Exception as exc:
                save(name + ".json", {"error_type": type(exc).__name__})
        print(json.dumps({"event": "observation_completed", "at": end, "firing_alerts_observed": len(first_firing), "api_errors": failures}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", type=local_url, default="http://127.0.0.1:19090")
    parser.add_argument("--alertmanager", type=local_url, default="http://127.0.0.1:19093")
    parser.add_argument("--receiver", type=local_url, default="http://127.0.0.1:18080")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=2)
    parser.add_argument("--stop-file", type=Path, help="Finish gracefully when this local file appears; still saves final evidence")
    capture(parser.parse_args())


if __name__ == "__main__":
    main()
