"""Internal Alertmanager webhook: preserve observed receipt time, never invent detection."""
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os

NAMESPACE = "observability-chaos-d"
MAX_BODY = 256 * 1024
EVENTS = deque(maxlen=1000)
ALERTS = {"ModuleDLatency", "ModuleDErrorRate", "ModuleDScrapeUnavailable"}


def normalize(payload, received_at):
    if not isinstance(payload, dict) or payload.get("status") not in ("firing", "resolved"):
        raise ValueError("Invalid notification")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= 100:
        raise ValueError("Invalid alert count")
    clean = []
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get("status") not in ("firing", "resolved"):
            raise ValueError("Invalid alert status")
        labels = alert.get("labels", {})
        if not isinstance(labels, dict) or labels.get("namespace") != NAMESPACE or labels.get("alertname") not in ALERTS:
            raise ValueError("Out-of-scope alert")
        clean.append({"status": alert.get("status"), "labels": labels,
            "annotations": alert.get("annotations", {}), "startsAt": alert.get("startsAt"),
            "endsAt": alert.get("endsAt"), "generatorURL": alert.get("generatorURL"),
            "fingerprint": alert.get("fingerprint")})
    return {"event": "module_d_alert_notification", "received_at": received_at,
            "status": payload["status"], "receiver": payload.get("receiver"), "alerts": clean,
            "timing_note": "received_at is observed webhook receipt; startsAt may include pending time and is not firing time"}


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, {"status": "ok"})
        elif self.path == "/events":
            self.respond(200, {"events": list(EVENTS), "capacity": EVENTS.maxlen,
                "note": "In-memory last events; retain stdout logs before deleting/restarting this pod"})
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/alerts":
            self.respond(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid length")
            event = normalize(json.loads(self.rfile.read(length)), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        except (ValueError, TypeError, AttributeError):
            self.respond(400, {"error": "invalid_notification"})
            return
        EVENTS.append(event)
        print(json.dumps(event, separators=(",", ":")), flush=True)
        self.respond(200, {"accepted": True})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if os.environ.get("POD_NAMESPACE") != NAMESPACE:
        raise SystemExit("Receiver requires the isolated Module D namespace")
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
