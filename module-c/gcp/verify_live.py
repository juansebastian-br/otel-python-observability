#!/usr/bin/env python3
"""Read-only evidence capture. Configured resources and observed signals stay separate."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import urllib.parse

from deploy import ACTIVE_CVES, SNAPSHOT, OWNER, api, gcloud, list_all, metric_filter, flow_resource, save

HERE = Path(__file__).resolve().parent


def series_query(project, metric, resource, start, end):
    base = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
    parameters = {"filter": metric_filter(metric, resource), "interval.startTime": start,
                  "interval.endTime": end, "view": "FULL", "pageSize": "100"}
    series = []
    while True:
        response = api("GET", base + "?" + urllib.parse.urlencode(parameters))
        series.extend(response.get("timeSeries", []))
        token = response.get("nextPageToken")
        if not token:
            return {"timeSeries": series, "complete": True}
        parameters["pageToken"] = token


def cve_summary(active, freshness, now):
    latest = {}
    for series in active.get("timeSeries", []):
        severity = series.get("metric", {}).get("labels", {}).get("severity")
        points = series.get("points", [])
        if severity and points:
            point = max(points, key=lambda p: p["interval"]["endTime"])
            if severity not in latest or point["interval"]["endTime"] > latest[severity]["point_time"]:
                latest[severity] = {"count": int(point["value"]["int64Value"]), "point_time": point["interval"]["endTime"]}
    snapshots = [point for series in freshness.get("timeSeries", []) for point in series.get("points", [])]
    latest_snapshot = max(snapshots, key=lambda p: p["interval"]["endTime"]) if snapshots else None
    stamp = int(latest_snapshot["value"]["int64Value"]) if latest_snapshot else None
    return {"observed_severities": sorted(latest), "latest_counts": latest,
        "all_five_severities_observed": set(latest) == {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"},
        "snapshot_scan_time_unix": stamp,
        "snapshot_age_seconds": int(now.timestamp()) - stamp if stamp is not None else None,
        "fresh_under_one_hour": (0 <= now.timestamp() - stamp <= 3600) if stamp is not None else None,
        "interpretation": "No samples means unknown, never zero. Counts cover only the declared scanned application-image inventory."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=HERE / ".state" / "plan.json")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    config = plan["config"]
    project = config["project"]
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")
    output = HERE / ".state" / ("live-verification-" + now.strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    summary = {"captured_at": end, "window_start": start, "window_end": end, "project": project,
        "cluster": config["cluster"], "namespace": config["namespace"], "output": str(output),
        "status": "CAPTURED", "log_metrics": {}, "errors": [], "mttd_seconds": None,
        "notification_delivery": "NOT_VERIFIED_BY_THIS_SCRIPT", "scc_activation": "NOT_VERIFIED_BY_THIS_SCRIPT"}
    def capture(name, call):
        try:
            result = call()
            save(output / (name + ".json"), result)
            return result
        except Exception as exc:
            # No auth headers/tokens or full subprocess commands are included in errors.
            summary["errors"].append({"resource": name, "error_type": type(exc).__name__})
            return None
    subnet = capture("subnet-current", lambda: gcloud("compute", "networks", "subnets", "describe", config["subnet"],
        "--region", config["region"], "--project", project))
    summary["flow_logs"] = {"configured_enabled": subnet.get("logConfig", {}).get("enable", False),
        "actual_log_config": subnet.get("logConfig", {})} if subnet is not None else {"configured_enabled": None}
    summary["flow_logs"]["selected_backend"] = config.get("flow_backend", "compute")
    summary["flow_logs"]["monitoring_resource_type"] = flow_resource(config)
    if config.get("flow_backend") == "network-management":
        selected = capture("network-management-current", lambda: api("GET",
            f"https://networkmanagement.googleapis.com/v1/projects/{project}/locations/global/vpcFlowLogsConfigs/{config['flow_config_name']}"))
        summary["flow_logs"]["compute_subnet_enabled"] = summary["flow_logs"]["configured_enabled"]
        summary["flow_logs"]["configured_enabled"] = selected.get("state") == "ENABLED" if selected is not None else None
        summary["flow_logs"]["network_management_configuration"] = selected
    for desired in plan["metrics"]:
        name = desired["name"]
        slug = name.split("/")[-1]
        metric = capture(slug + "-config", lambda name=name: api("GET",
            f"https://logging.googleapis.com/v2/projects/{project}/metrics/" + urllib.parse.quote(name, safe="")))
        logs = capture(slug + "-logs", lambda desired=desired: api("POST", "https://logging.googleapis.com/v2/entries:list", {
            "resourceNames": ["projects/" + project], "filter": desired["filter"] + f' AND timestamp >= "{start}" AND timestamp <= "{end}"',
            "orderBy": "timestamp desc", "pageSize": 100}))
        series = capture(slug + "-series", lambda slug=slug: series_query(project, slug,
            "k8s_container" if slug == "auth_failures" else flow_resource(config), start, end))
        summary["log_metrics"][name] = {"configured": metric is not None,
            "configured_filter_matches_plan": metric.get("filter") == desired["filter"] if metric is not None else None,
            "observed_log_entries": len(logs.get("entries", [])) if logs is not None else None,
            "logs_truncated": bool(logs.get("nextPageToken")) if logs is not None else None,
            "observed_time_series": len(series["timeSeries"]) if series is not None else None,
            "observed_metric_points": sum(len(s.get("points", [])) for s in series["timeSeries"]) if series is not None else None}
    policies = capture("managed-alert-policies", lambda: [p for p in list_all(
        f"https://monitoring.googleapis.com/v3/projects/{project}/alertPolicies", "alertPolicies")
        if p.get("userLabels", {}).get("managed_by") == OWNER])
    summary["alert_policies"] = [{"name": p["name"], "displayName": p.get("displayName"), "enabled": p.get("enabled"),
        "rule": p.get("userLabels", {}).get("module_c_rule"), "notification_channels": p.get("notificationChannels", [])}
        for p in policies] if policies is not None else None
    channel_names = sorted({channel for p in policies or [] for channel in p.get("notificationChannels", [])})
    channels = []
    for index, channel in enumerate(channel_names):
        def safe_channel(channel=channel):
            raw = api("GET", "https://monitoring.googleapis.com/v3/" + channel)
            return {key: raw.get(key) for key in ("name", "displayName", "type", "enabled", "verificationStatus")}
        value = capture(f"channel-{index + 1}-metadata", safe_channel)
        if value:
            channels.append(value)
    summary["channel_metadata"] = channels
    active = capture("cve-active-series", lambda: series_query(project, ACTIVE_CVES, "global", start, end))
    freshness = capture("cve-freshness-series", lambda: series_query(project, SNAPSHOT, "global", start, end))
    summary["cve_inventory"] = cve_summary(active or {}, freshness or {}, now)
    dashboards = capture("managed-dashboards", lambda: [d for d in list_all(
        f"https://monitoring.googleapis.com/v1/projects/{project}/dashboards", "dashboards") if OWNER in d.get("labels", {})])
    summary["dashboards"] = [{"name": d["name"], "displayName": d.get("displayName")} for d in dashboards or []]
    if summary["errors"]:
        summary["status"] = "PARTIAL_CAPTURE"
    save(output / "summary.json", summary)
    save(HERE / ".state" / "verification-latest.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
