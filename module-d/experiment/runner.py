"""Fixed-arrival HTTP load; never activates chaos or modifies Kubernetes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

NAMESPACE = "observability-chaos-d"
MAX_RATE = 5.0
MAX_WORKERS = 20
PHASE_LIMITS = {"baseline": 900.0, "chaos": 180.0, "recovery": 300.0}
TRACE_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
RESERVED_HEADERS = {"host", "content-length", "transfer-encoding", "connection",
                    "traceparent", "x-experiment-id", "x-experiment-request-id"}


class PhaseTransitionError(RuntimeError):
    """The external coordinator rejected or failed a phase transition."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def iso_at(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def origin(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def validate_target(target_url, path, allow_host):
    """Require both a narrow namespace boundary and an explicit exact host."""
    parsed = urlsplit(target_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("target-url must be an explicit HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("userinfo, query strings and fragments are forbidden")
    if parsed.path not in ("", "/"):
        raise ValueError("provide the business path separately with --path")
    host = parsed.hostname.lower()
    if allow_host != host:
        raise ValueError("--allow-host must exactly match the target hostname")
    internal = re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
        + re.escape(NAMESPACE) + r"\.svc(?:\.cluster\.local)?", host
    )
    if host not in ("127.0.0.1", "::1") and not internal:
        raise ValueError("only loopback port-forward or the dedicated Module D namespace is allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid target port") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("an explicit valid target port is required")
    route = urlsplit(path)
    if (not path.startswith("/") or path.startswith("//") or route.scheme or route.netloc
            or route.query or route.fragment or any(ord(c) < 33 or ord(c) == 127 for c in path)):
        raise ValueError("path must be an absolute route without query, fragment or whitespace")
    return Target(parsed.scheme, host, port, path)


def headers_from_environment(specs):
    headers = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError("header-env must have Header-Name=ENVIRONMENT_VARIABLE form")
        name, env_name = spec.split("=", 1)
        if not HEADER_NAME.fullmatch(name) or name.lower() in RESERVED_HEADERS:
            raise ValueError("invalid or reserved header name")
        if name.lower() in {key.lower() for key in headers}:
            raise ValueError("duplicate header names are forbidden")
        value = os.environ.get(env_name)
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("a required header environment variable is absent or invalid")
        # Values are passed only to the HTTP client, never to evidence or errors.
        headers[name] = value
    return headers


@dataclass
class Config:
    target: Target
    experiment_id: str
    method: str
    body: bytes = field(repr=False)
    headers: dict = field(default_factory=dict, repr=False)
    rate: float = 5.0
    max_workers: int = 20
    timeout_seconds: float = 10.0
    baseline_seconds: float = 900.0
    chaos_seconds: float = 180.0
    recovery_seconds: float = 300.0
    max_start_lag_seconds: float = 0.1
    response_byte_limit: int = 1024 * 1024
    connect_address: tuple | None = field(default=None, init=False, repr=False)

    def validate(self):
        # Revalidate even when a caller constructs Target rather than using the CLI.
        validate_target(self.target.origin, self.target.path, self.target.host)
        if not SAFE_ID.fullmatch(self.experiment_id):
            raise ValueError("invalid experiment-id")
        if self.method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise ValueError("unsupported explicit HTTP method")
        if not math.isfinite(self.rate) or not 0 < self.rate <= MAX_RATE:
            raise ValueError("rate must be positive and no greater than 5 requests/s")
        if not 1 <= self.max_workers <= MAX_WORKERS:
            raise ValueError("max-workers must be between 1 and 20")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout must be positive and at most 30 seconds")
        if not math.isfinite(self.max_start_lag_seconds) or not 0 <= self.max_start_lag_seconds <= 1 / self.rate:
            raise ValueError("max-start-lag must fit within one arrival interval")
        if not 1 <= self.response_byte_limit <= 1024 * 1024:
            raise ValueError("response-byte-limit must be between 1 and 1048576")
        total = 0.0
        for phase, limit in PHASE_LIMITS.items():
            value = getattr(self, phase + "_seconds")
            if not math.isfinite(value) or not 0 <= value <= limit:
                raise ValueError(f"{phase} duration must be between 0 and {limit:g} seconds")
            total += value
        if total <= 0:
            raise ValueError("at least one phase must have positive duration")
        if len(self.body) > 65536:
            raise ValueError("request body is limited to 64 KiB")
        try:
            json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("body-file must contain valid JSON, including {} for an empty contract") from exc
        for name, value in self.headers.items():
            if (not HEADER_NAME.fullmatch(name) or name.lower() in RESERVED_HEADERS
                    or not isinstance(value, str) or not value
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("invalid request header")


def resolve_target(config):
    """Bound DNS before scheduling and pin each connection to that resolution."""
    if config.target.host == "127.0.0.1":
        return socket.AF_INET, (config.target.host, config.target.port)
    if config.target.host == "::1":
        return socket.AF_INET6, (config.target.host, config.target.port, 0, 0)
    results = queue.Queue(maxsize=1)

    def resolve():
        try:
            results.put(socket.getaddrinfo(config.target.host, config.target.port, type=socket.SOCK_STREAM))
        except OSError as exc:
            results.put(type(exc).__name__)

    threading.Thread(target=resolve, daemon=True, name="module-d-dns").start()
    try:
        addresses = results.get(timeout=config.timeout_seconds)
    except queue.Empty as exc:
        raise ValueError("sandbox DNS resolution exceeded the configured deadline") from exc
    if not isinstance(addresses, list) or not addresses:
        raise ValueError("sandbox DNS resolution failed")
    family, _, _, _, address = addresses[0]
    return family, address


def phase_plan(config):
    offset, phases = 0.0, []
    for phase in PHASE_LIMITS:
        duration = getattr(config, phase + "_seconds")
        if duration:
            phases.append({"phase": phase, "start": offset, "end": offset + duration})
            offset += duration
    return phases, offset


def missed_record(base, reason, lateness):
    return {**base, "record_type": "arrival_missed", "timestamp": utc_now(),
            "reason": reason, "schedule_lag_ms": round(max(0, lateness) * 1000, 3),
            "attempted": False}


def request_once(config, base, due, phase_end):
    now = time.monotonic()
    if now - due > config.max_start_lag_seconds or now >= phase_end:
        return missed_record(base, "worker_start_late", now - due)
    trace_id, span_id = secrets.token_hex(16), secrets.token_hex(8)
    while int(trace_id, 16) == 0:
        trace_id = secrets.token_hex(16)
    row = {**base, "record_type": "request", "attempted": True, "started_at": utc_now(),
           "sent_trace_id": trace_id, "response_trace_id": None, "trace_header_matches": None,
           "trace_correlation": "not_observed", "status": None, "response_bytes": 0,
           "schedule_lag_ms": round(max(0, now - due) * 1000, 3)}
    headers = {"Content-Type": "application/json", **config.headers,
               "traceparent": f"00-{trace_id}-{span_id}-01",
               "X-Experiment-Id": config.experiment_id,
               "X-Experiment-Request-Id": base["request_id"]}
    cls = http.client.HTTPSConnection if config.target.scheme == "https" else http.client.HTTPConnection
    connection = cls(config.target.host, config.target.port, timeout=config.timeout_seconds)
    if config.connect_address is not None:
        family, address = config.connect_address

        def pinned_connection(_address, timeout, source_address=None):
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                if source_address:
                    sock.bind(source_address)
                sock.connect(address)
                return sock
            except BaseException:
                sock.close()
                raise

        # HTTPConnection keeps the original hostname for Host and HTTPS SNI.
        connection._create_connection = pinned_connection
    expired = threading.Event()
    timer = None
    started = time.monotonic()
    deadline = started + config.timeout_seconds
    try:
        # No proxies and no automatic redirects. Preserve the socket reference even
        # when HTTPConnection detaches it for a Connection: close response.
        connection.connect()
        wire = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        wire.settimeout(remaining)

        def expire():
            expired.set()
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            wire.close()

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request(config.method, config.target.path, body=config.body, headers=headers)
        response = connection.getresponse()
        row["status"] = response.status
        response_id = response.getheader("x-trace-id", "").strip()
        if TRACE_ID.fullmatch(response_id) and int(response_id, 16):
            row["response_trace_id"] = response_id.lower()
            row["trace_header_matches"] = response_id.lower() == trace_id
            row["trace_correlation"] = "response_header_matches" if row["trace_header_matches"] else "response_header_mismatch"
        while True:
            # One extra byte distinguishes a complete bounded response from a
            # truncated response; neither headers nor body are written to evidence.
            chunk = response.read(min(65536, config.response_byte_limit + 1 - row["response_bytes"]))
            row["response_bytes"] += len(chunk)
            if row["response_bytes"] > config.response_byte_limit:
                row["outcome"] = "response_limit_exceeded"
                break
            if not chunk:
                row["outcome"] = ("http_success" if 200 <= response.status < 300 else
                                  "http_redirect" if 300 <= response.status < 400 else
                                  "http_client_error" if 400 <= response.status < 500 else
                                  "http_server_error" if 500 <= response.status < 600 else "invalid_http_status")
                break
        if expired.is_set() or time.monotonic() >= deadline:
            row["outcome"] = "timeout"
    except (TimeoutError, socket.timeout):
        row["outcome"] = "timeout"
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
        row["outcome"] = "timeout" if expired.is_set() or time.monotonic() >= deadline else "transport_error"
        row["error_type"] = type(exc).__name__  # Never serialize exception text.
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()
    row["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    row["finished_at"] = utc_now()
    row["success"] = row["outcome"] == "http_success"
    row["latency_censored"] = row["outcome"] in ("timeout", "transport_error", "response_limit_exceeded")
    return row


def run(config, stream, stop_event=None, phase_hook=None):
    config.validate()
    config.connect_address = resolve_target(config)
    stop_event = stop_event or threading.Event()
    phases, duration = phase_plan(config)
    planned_arrivals = math.ceil(duration * config.rate - 1e-10)
    events = []
    for phase in phases:
        events += [(phase["start"], 1, "phase_start", phase), (phase["end"], 0, "phase_end", phase)]
    for index in range(planned_arrivals):
        offset = index / config.rate
        phase = next(p for p in phases if p["start"] <= offset < p["end"])
        events.append((offset, 2, "arrival", {**phase, "index": index}))
    events.sort(key=lambda event: (event[0], event[1]))
    wall_start, mono_start = datetime.now(timezone.utc), time.monotonic()
    counts = {"attempted": 0, "arrival_missed": 0, "successful": 0}

    def write(row):
        stream.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        if row["record_type"] == "request":
            counts["attempted"] += 1
            counts["successful"] += int(row["success"])
        elif row["record_type"] == "arrival_missed":
            counts["arrival_missed"] += 1

    write({"record_type": "run_start", "schema_version": 1, "timestamp": iso_at(wall_start),
           "experiment_id": config.experiment_id, "target_origin": config.target.origin,
           "path": config.target.path, "method": config.method, "rate_per_second": config.rate,
           "max_workers": config.max_workers, "timeout_seconds": config.timeout_seconds,
           "phases": phases, "planned_arrivals": planned_arrivals,
           "phase_semantics": "planned load windows; chaos activation is external",
           "trace_semantics": "sent client context; response header match does not prove backend trace ingestion"})
    futures, completed, interrupted = set(), False, False
    transition_delay, transition_failure = 0.0, False
    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="module-d-load") as pool:
        try:
            for offset, _, kind, phase in events:
                if stop_event.wait(max(0, mono_start + offset + transition_delay - time.monotonic())):
                    break
                for future in list(futures):
                    if future.done():
                        write(future.result())
                        futures.remove(future)
                if kind != "arrival":
                    if kind == "phase_start" and phase_hook is not None:
                        transition_start = time.monotonic()
                        transition_at = utc_now()
                        for future in as_completed(futures):
                            write(future.result())
                        futures.clear()
                        details = phase_hook(phase["phase"]) or {}
                        elapsed = time.monotonic() - transition_start
                        transition_delay += elapsed
                        write({"record_type": "phase_transition", "timestamp": utc_now(),
                               "started_at": transition_at, "phase": phase["phase"],
                               "experiment_id": config.experiment_id, "duration_seconds": elapsed,
                               "details": details, "arrival_semantics": "no arrivals offered during the explicit transition"})
                    write({"record_type": kind, "timestamp": utc_now(), "phase": phase["phase"],
                           "experiment_id": config.experiment_id,
                           "planned_at": iso_at(wall_start + timedelta(seconds=offset + transition_delay))})
                    continue
                base = {"experiment_id": config.experiment_id, "phase": phase["phase"],
                        "request_id": f"{config.experiment_id}-{phase['index']:06d}",
                        "scheduled_at": iso_at(wall_start + timedelta(seconds=offset + transition_delay))}
                due, now = mono_start + offset + transition_delay, time.monotonic()
                if now - due > config.max_start_lag_seconds:
                    write(missed_record(base, "scheduler_late", now - due))
                elif len(futures) >= config.max_workers:
                    write(missed_record(base, "workers_busy", now - due))
                else:
                    futures.add(pool.submit(request_once, config, base, due, mono_start + phase["end"] + transition_delay))
            else:
                completed = True
        except KeyboardInterrupt:
            interrupted = True
            stop_event.set()
        except PhaseTransitionError:
            transition_failure = True
            stop_event.set()
        finally:
            # Each in-flight request has its own whole-request deadline. No new
            # arrivals are issued while draining, and no chaos control is touched.
            for future in as_completed(futures):
                write(future.result())
    result = {"record_type": "run_end", "timestamp": utc_now(), "experiment_id": config.experiment_id,
              "completed": completed, "interrupted": interrupted, "planned_arrivals": planned_arrivals,
              "phase_transition_failed": transition_failure, "transition_seconds": transition_delay,
              "unissued_after_stop": planned_arrivals - counts["attempted"] - counts["arrival_missed"], **counts}
    write(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--allow-host", required=True)
    parser.add_argument("--method", required=True, choices=("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--header-env", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rate", type=float, default=5)
    parser.add_argument("--max-workers", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--max-start-lag-seconds", type=float, default=0.1)
    for phase, limit in PHASE_LIMITS.items():
        parser.add_argument("--" + phase + "-seconds", type=float, default=limit)
    args = parser.parse_args(argv)
    try:
        body = args.body_file.read_bytes()
        config = Config(validate_target(args.target_url, args.path, args.allow_host), args.experiment_id,
                        args.method, body, headers_from_environment(args.header_env), args.rate,
                        args.max_workers, args.timeout_seconds, args.baseline_seconds,
                        args.chaos_seconds, args.recovery_seconds, args.max_start_lag_seconds)
        config.validate()
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            result = run(config, stream)
        print(json.dumps({"output": str(args.output), **result}, allow_nan=False))
        return 0 if result["completed"] else 130
    except (ValueError, OSError) as exc:
        # File paths and credentials can occur inside raw exceptions.
        parser.exit(2, f"Invalid configuration or output ({type(exc).__name__}); check inputs and use a new output path.\n")


if __name__ == "__main__":
    raise SystemExit(main())
