"""Opt-in authentication for the isolated Module C sandbox.

This guard is deliberately small: it demonstrates actual rejected credentials
and correlated logs. It is not a production identity provider.
"""
import hmac
import json
import os
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from opentelemetry import trace


def build_api_key_guard():
    enabled_value = os.getenv("SECURITY_AUTH_ENABLED", "false").lower()
    if enabled_value not in {"true", "false"}:
        raise RuntimeError("SECURITY_AUTH_ENABLED must be true or false")
    enabled = enabled_value == "true"
    expected = os.getenv("SECURITY_API_KEY", "")
    environment = os.getenv("DEPLOYMENT_ENVIRONMENT", "local").lower()
    if enabled and environment not in {"sandbox", "local", "test"}:
        raise RuntimeError("Module C API-key authentication requires a sandbox environment")
    if enabled and len(expected.encode("utf-8")) < 24:
        raise RuntimeError("SECURITY_API_KEY must contain at least 24 bytes when authentication is enabled")
    service_name = os.getenv("OTEL_SERVICE_NAME", "orders-service")

    async def require_api_key(request: Request):
        if not enabled:
            return
        supplied = request.headers.getlist("x-api-key")
        valid = len(supplied) == 1 and hmac.compare_digest(
            supplied[0].encode("utf-8"), expected.encode("utf-8")
        )
        if valid:
            return
        context = trace.get_current_span().get_span_context()
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "WARNING",
            "event": "authentication_failed",
            "service_name": service_name,
            "trace_id": f"{context.trace_id:032x}" if context.is_valid else None,
            "span_id": f"{context.span_id:016x}" if context.is_valid else None,
            "reason": "missing_credential" if not supplied else "invalid_credential",
        }
        # Exactly one stdout JSON event per rejection. Never serialize headers,
        # credentials, customer identifiers, request bodies, or IP addresses.
        print(json.dumps(event, separators=(",", ":")), flush=True)
        raise HTTPException(status_code=401, detail="Authentication required")

    return require_api_key
