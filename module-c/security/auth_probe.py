"""Send a bounded, non-destructive rejected-credential probe to a sandbox."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_target(url, allowed_host=None):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use an http(s) sandbox URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials, query strings and fragments are not allowed")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and host != (allowed_host or "").lower():
        raise ValueError("Remote probes require --allow-host with the exact authorized sandbox hostname")
    if parsed.path.rstrip("/") not in {"", "/orders"}:
        raise ValueError("This probe only supports the sandbox /orders route")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/orders", "", "", ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--allow-host", help="Exact sandbox host you are authorized to test")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.count <= 20 or not 0.5 <= args.interval <= 10:
        parser.error("Use 1..20 requests and a 0.5..10 second interval")
    try:
        target = validate_target(args.url, args.allow_host)
    except ValueError as exc:
        parser.error(str(exc))
    opener = urllib.request.build_opener(NoRedirects())
    trace_ids = []
    started_at = datetime.now(timezone.utc).isoformat()
    started_clock = time.perf_counter()
    for index in range(args.count):
        trace_id, span_id = secrets.token_hex(16), secrets.token_hex(8)
        trace_ids.append(trace_id)
        request = urllib.request.Request(
            target, data=b"{}", method="POST", headers={
                "Content-Type": "application/json",
                "X-API-Key": "module-c-deliberately-invalid",
                "traceparent": f"00-{trace_id}-{span_id}-01",
            },
        )
        sent_at = datetime.now(timezone.utc).isoformat()
        try:
            with opener.open(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            raise SystemExit(f"Probe stopped: sandbox connection failed ({type(exc.reason).__name__})")
        if status != 401:
            raise SystemExit(f"Probe stopped: expected HTTP 401, received {status}; verify sandbox authentication")
        print(json.dumps({"probe": index + 1, "status": status, "trace_id": trace_id,
                          "sent_at": sent_at, "completed_at": datetime.now(timezone.utc).isoformat()}), flush=True)
        if index + 1 < args.count:
            time.sleep(args.interval)
    print(json.dumps({"result": "PASS", "rejected_requests": args.count, "trace_ids": trace_ids,
                      "started_at": started_at, "completed_at": datetime.now(timezone.utc).isoformat(),
                      "duration_seconds": round(time.perf_counter() - started_clock, 3)}), flush=True)


if __name__ == "__main__":
    main()
