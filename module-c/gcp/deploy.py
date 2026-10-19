#!/usr/bin/env python3
"""Module C GCP configuration. Read-only plan by default; explicit apply only.

Uses Python's standard library and the signed-in gcloud identity. Never stores tokens.
Cloud Shell: python3 module-c/gcp/deploy.py plan --project crafty-dynamics-239114
"""
import argparse
import copy
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ACTIVE_CVES = "custom.googleapis.com/security/active_cves"
SNAPSHOT = "custom.googleapis.com/security/cve_snapshot_timestamp"
OWNER = "otel-module-c"


def gcloud(*args):
    result = subprocess.run(["gcloud", *args, "--format=json", "--quiet"], text=True,
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def api(method, url, body=None, missing_ok=False):
    token = subprocess.run(["gcloud", "auth", "print-access-token"], text=True,
                           capture_output=True, check=True).stdout.strip()
    request = urllib.request.Request(url, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            # Retry only idempotent operations. A POST may have succeeded before timeout.
            if method in ("GET", "PUT", "PATCH") and exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url}: HTTP {exc.code}: {exc.read().decode()}") from exc


def list_all(url, key):
    items = []
    while True:
        response = api("GET", url)
        items.extend(response.get(key, []))
        token = response.get("nextPageToken")
        if not token:
            return items
        url = url.split("?", 1)[0] + "?pageToken=" + urllib.parse.quote(token)


def q(value):
    return json.dumps(str(value))


def discover(args):
    project = gcloud("projects", "describe", args.project)
    clusters = gcloud("container", "clusters", "list", "--project", args.project)
    candidates = [c for c in clusters if (not args.cluster or c["name"] == args.cluster)
                  and (not args.location or c.get("location", c.get("zone")) == args.location)]
    if len(candidates) != 1:
        choices = [(c["name"], c.get("location", c.get("zone"))) for c in clusters]
        raise RuntimeError(f"Specify --cluster and --location; expected one cluster, found {len(candidates)}. Available: {choices}")
    selected = candidates[0]
    location = selected.get("location", selected.get("zone"))
    cluster = gcloud("container", "clusters", "describe", selected["name"],
                     "--location", location, "--project", args.project)
    subnet_ref = cluster.get("subnetwork") or cluster.get("networkConfig", {}).get("subnetwork")
    region = re.sub(r"-[a-z]$", "", location)
    if not subnet_ref:
        raise RuntimeError("Cluster has no discoverable subnetwork; legacy networks are unsupported.")
    if "/projects/" in subnet_ref:
        host = subnet_ref.split("/projects/")[-1].split("/")[0]
    elif subnet_ref.startswith("projects/"):
        host = subnet_ref.split("/")[1]
    else:
        host = args.project
    if host != args.project:
        raise RuntimeError("Shared VPC host differs from this project. Stop: do not modify a shared host subnet.")
    subnet = gcloud("compute", "networks", "subnets", "describe", subnet_ref.split("/")[-1],
                    "--region", region, "--project", args.project)
    network = subnet["network"].split("/")[-1]
    network_subnets = gcloud("compute", "networks", "subnets", "list", "--project", args.project)
    cidrs = []
    for entry in network_subnets:
        if entry["network"].split("/")[-1] == network:
            cidrs.append(entry["ipCidrRange"])
            cidrs.extend(r["ipCidrRange"] for r in entry.get("secondaryIpRanges", []))
    return {"project": args.project, "project_number": project["projectNumber"],
            "parent": project.get("parent"), "cluster": cluster["name"], "location": location,
            "region": region, "namespace": args.namespace, "network": network,
            "subnet": subnet["name"], "subnet_id": str(subnet["id"]),
            "network_cidrs": sorted(set(cidrs)), "subnet_before": subnet,
            "autopilot": cluster.get("autopilot", {}).get("enabled", False),
            "intranode_visibility": cluster.get("networkConfig", {}).get("enableIntraNodeVisibility", False),
            "logging": cluster.get("loggingConfig", {}), "channels": args.notification_channel,
            "unexpected_port": args.unexpected_port,
            "flow_records_threshold": args.flow_records_threshold,
            "auth_failures_threshold": args.auth_failures_threshold}


def flow_resource(config):
    """Monitoring resource is explicit when using a non-legacy log backend."""
    return config.get("flow_metric_resource", "gce_subnetwork")


def flow_log_filter(config):
    if config.get("flow_backend", "compute") == "network-management":
        return ('log_id("networkmanagement.googleapis.com/vpc_flows") AND resource.type="vpc_flow_logs_config" '
                f'AND resource.labels.name={q(config["flow_config_name"])}')
    return (f'log_id("compute.googleapis.com/vpc_flows") AND resource.type="gce_subnetwork" '
            f'AND resource.labels.subnetwork_id={q(config["subnet_id"])}')


def verify_flow_backend(config, out):
    """Read-only gate: migration may reuse only an owned, enabled exact-subnet config."""
    if config.get("flow_backend", "compute") != "network-management":
        return None
    from flow_diagnostic import assert_owned, configuration
    name = f"projects/{config['project']}/locations/global/vpcFlowLogsConfigs/{config['flow_config_name']}"
    existing = api("GET", "https://networkmanagement.googleapis.com/v1/" + name)
    receipt_paths = [out / "flow-diagnostic-owned.json", out / "network-diagnostic-operation.json"]
    if config.get("flow_creation_receipt"):
        receipt_paths.append(pathlib.Path(config["flow_creation_receipt"]))
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in receipt_paths if path.exists()]
    assert_owned(existing, configuration(config), receipts)
    if existing.get("state") != "ENABLED":
        raise ValueError("Network Management flow configuration is not ENABLED; no changes applied")
    if existing.get("metadata") != "INCLUDE_ALL_METADATA" or existing.get("filterExpr", "true") != "true":
        raise ValueError("Network Management flow configuration may hide pod metadata/flows; inspect before migration")
    return existing


def counter(name, description, log_filter, labels=None):
    metric = {"name": "module_c/" + name, "description": description, "filter": log_filter,
              "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64", "unit": "1"}}
    if labels:
        metric["metricDescriptor"]["labels"] = [{"key": key, "valueType": "STRING"} for key in labels]
        metric["labelExtractors"] = {key: f"EXTRACT({field})" for key, field in labels.items()}
    return metric


def metric_filter(name, resource):
    kind = name if name.startswith("custom.") else "logging.googleapis.com/user/module_c/" + name
    return f'metric.type = {q(kind)} AND resource.type = {q(resource)}'


def aggregation(aligner="ALIGN_SUM", groups=None):
    return {"alignmentPeriod": "60s", "perSeriesAligner": aligner,
            "crossSeriesReducer": "REDUCE_SUM", "groupByFields": groups or []}


def threshold_policy(key, title, name, resource, threshold, runbook, channels, duration="0s"):
    return {"displayName": title, "combiner": "OR", "enabled": True,
            "userLabels": {"managed_by": OWNER, "module_c_rule": key},
            "notificationChannels": channels,
            "documentation": {"mimeType": "text/markdown", "content": runbook},
            "alertStrategy": {"autoClose": "3600s"},
            "conditions": [{"displayName": title, "conditionThreshold": {
                "filter": metric_filter(name, resource), "comparison": "COMPARISON_GT",
                "thresholdValue": threshold, "duration": duration,
                "aggregations": [aggregation()], "trigger": {"count": 1}}}]}


def chart(title, name, resource, groups=None, aligner="ALIGN_SUM"):
    return {"title": title, "xyChart": {"chartOptions": {"mode": "COLOR"},
        "dataSets": [{"plotType": "LINE", "timeSeriesQuery": {"timeSeriesFilter": {
            "filter": metric_filter(name, resource), "aggregation": aggregation(aligner, groups)}}}],
        "yAxis": {"label": "Cantidad", "scale": "LINEAR"}}}


def build(config):
    project, cluster, namespace = (config[k] for k in ("project", "cluster", "namespace"))
    scope = f"{OWNER} {project}/{cluster}/{namespace}. "
    source_cluster = f'jsonPayload.src_gke_details.cluster.cluster_name={q(cluster)}'
    dest_cluster = f'jsonPayload.dest_gke_details.cluster.cluster_name={q(cluster)}'
    src_ns = f'jsonPayload.src_gke_details.pod.pod_namespace={q(namespace)}'
    dst_ns = f'jsonPayload.dest_gke_details.pod.pod_namespace={q(namespace)}'
    # Scope by the actual subnet ID. Node SNAT can remove pod metadata from external flows.
    flow = flow_log_filter(config)
    network_resource = flow_resource(config)
    in_src = "(" + " OR ".join(f'ip_in_net(jsonPayload.connection.src_ip,{q(c)})' for c in config["network_cidrs"]) + ")"
    in_dst = "(" + " OR ".join(f'ip_in_net(jsonPayload.connection.dest_ip,{q(c)})' for c in config["network_cidrs"]) + ")"
    # Count one reporter per direction to avoid SRC+DEST duplication. Separate boundary directions.
    ew = flow + f' AND jsonPayload.reporter="SRC" AND {in_src} AND {in_dst}'
    ns_egress = flow + f' AND jsonPayload.reporter="SRC" AND {in_src} AND NOT {in_dst}'
    ns_ingress = flow + f' AND jsonPayload.reporter="DEST" AND NOT {in_src} AND {in_dst}'
    # GKE Service DNAT can hide the destination pod/port on the source reporter.
    # Live canary records preserve both pod identities and logical port at DEST.
    # Use exactly that reporter for interservice rules; general E-W remains SRC.
    interservice = (flow + f' AND jsonPayload.reporter="DEST" AND {in_src} AND {in_dst}'
                    + f" AND {source_cluster} AND {dest_cluster} AND {src_ns} AND {dst_ns}")
    suspicious = interservice + f' AND jsonPayload.connection.protocol=6 AND jsonPayload.connection.dest_port={config["unexpected_port"]}'
    auth = (f'resource.type="k8s_container" AND resource.labels.project_id={q(project)} '
            f'AND resource.labels.cluster_name={q(cluster)} AND resource.labels.namespace_name={q(namespace)} '
            'AND log_id("stdout") AND jsonPayload.event="authentication_failed"')
    metrics = [counter("auth_failures", scope + "Failed authentication attempts (no credentials).", auth,
                       {"service_name": "jsonPayload.service_name"}),
        counter("ew_flow_records", scope + "Sampled E-W flow records within configured VPC CIDRs (not bytes).", ew),
        counter("ns_egress_flow_records", scope + "Sampled N-S egress flow records crossing configured VPC CIDRs.", ns_egress),
        counter("ns_ingress_flow_records", scope + "Sampled N-S ingress flow records crossing configured VPC CIDRs.", ns_ingress),
        counter("interservice_flow_records", scope + "Destination-observed sampled flows between pods in this lab namespace.", interservice),
        counter("interservice_unexpected_port", scope + "Destination-observed TCP traffic to the lab's explicitly unexpected port.", suspicious)]
    descriptors = [
        {"type": ACTIVE_CVES, "displayName": "Active CVEs - successful full scan", "metricKind": "GAUGE",
         "valueType": "INT64", "unit": "1", "labels": [{"key": "severity", "valueType": "STRING"}],
         "description": "Current distinct active CVEs per severity in the declared image inventory; publish all severities including zero."},
        {"type": SNAPSHOT, "displayName": "CVE full snapshot timestamp", "metricKind": "GAUGE", "valueType": "INT64",
         "unit": "s", "description": "Unix timestamp of the latest successful complete CVE inventory scan; failures must not refresh it."}]
    logs_url = f"https://console.cloud.google.com/logs/query?project={project}"
    policies = [threshold_policy("auth", "Modulo C - autenticaciones fallidas", "auth_failures", "k8s_container",
          config["auth_failures_threshold"], "Revisar eventos authentication_failed en " + logs_url +
          ". Usar trace_id y service_name del JSON para correlacionar. No copiar credenciales. Umbral: intentos por ventana de 60 s.", config["channels"]),
        threshold_policy("unexpected_port", "Modulo C - puerto inesperado entre servicios", "interservice_unexpected_port", network_resource, 0,
          f"Puerto TCP {config['unexpected_port']} observado desde DEST entre pods de {cluster}/{namespace}. Revisar origen/destino/horario en {logs_url}. "
          "Comparar con despliegues y autorizaciones; detener solo el generador de prueba. Este contador no significa firewall DENY.", config["channels"]),
        threshold_policy("traffic_spike", "Modulo C - pico de registros entre servicios", "interservice_flow_records", network_resource,
          config["flow_records_threshold"], "Umbral de laboratorio ajustable: registros muestreados por 60 s. Comparar con 15 min de baseline; "
          "verificar sampling, replicas y cambios de carga antes de escalar. Investigar pares IP/puerto en " + logs_url, config["channels"], "60s")]
    snapshot_selector = '{' + q(SNAPSHOT) + ', monitored_resource="global", project_id=' + q(project) + '}'
    snapshot_age = 'time() - max(last_over_time(' + snapshot_selector + '[2h]))'
    policies.append({"displayName": "Modulo C - inventario CVE sin actualizar (60 min)", "combiner": "OR", "enabled": True,
        "userLabels": {"managed_by": OWNER, "module_c_rule": "cve_missing"}, "notificationChannels": config["channels"],
        "documentation": {"mimeType": "text/markdown", "content": "No existe snapshot reciente o su fecha real supera una hora. Revisar escaner, inventario y permisos. Cero CVEs no equivale a ausencia de datos. Esta alerta tambien detecta que nunca se haya publicado el primer snapshot."},
        "conditions": [{"displayName": "Snapshot ausente o con mas de 3600 segundos", "conditionPrometheusQueryLanguage": {
            "query": '(' + snapshot_age + ' > 3600) or absent_over_time(' + snapshot_selector + '[1h])',
            "duration": "60s", "evaluationInterval": "60s"}}]})
    widgets = [
        {"title": "Cobertura y lectura", "text": {"format": "MARKDOWN", "content":
            f"**{project} / {cluster} / {namespace}**. Red: **{config['network']}**, subred: **{config['subnet']}**. "
            f"Fuente exclusiva: **{config.get('flow_backend', 'compute')}**. "
            "N-S cruza los CIDR de esta VPC; E-W permanece dentro. Los graficos de red muestran registros muestreados por minuto, "
            "no paquetes, bytes ni requests. Tramos peering/on-prem no enumerados se clasifican como externos. "
            "La metadata de pods puede desaparecer por SNAT. CVEs: inventario completo de las imagenes de aplicacion declaradas en "
            "orders-service, payments-service y data-service, por digest real. Excluye sidecars, nodos y complementos del cluster. "
            "Sin muestras significa desconocido, no cero; consultar la edad real del snapshot."}},
        chart("Intentos fallidos / minuto por servicio", "auth_failures", "k8s_container", ["metric.label.service_name"]),
        chart("E-W: registros observados / minuto", "ew_flow_records", network_resource),
        chart("N-S saliente: registros observados / minuto", "ns_egress_flow_records", network_resource),
        chart("N-S entrante: registros observados / minuto", "ns_ingress_flow_records", network_resource),
        chart("Puerto inesperado entre servicios / minuto", "interservice_unexpected_port", network_resource),
        chart("CVEs activos por severidad (snapshot)", ACTIVE_CVES, "global", ["metric.label.severity"], "ALIGN_MAX"),
        {"title": "Edad real del snapshot CVE (segundos)", "xyChart": {"dataSets": [{"plotType": "LINE",
            "timeSeriesQuery": {"prometheusQuery": snapshot_age}}], "yAxis": {"label": "Segundos", "scale": "LINEAR"}}}]
    dashboard = {"displayName": "Modulo C - Golden Signals de Seguridad", "labels": {OWNER: ""},
                 "gridLayout": {"columns": "2", "widgets": widgets}}
    return {"config": config, "metrics": metrics, "descriptors": descriptors, "policies": policies, "dashboard": dashboard}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply(plan, out):
    c = plan["config"]
    project = c["project"]
    backend_config = verify_flow_backend(c, out)
    if backend_config:
        save(out / "network-management-selected.json", backend_config)
    # Fail before mutation if an existing metric belongs to a different lab scope.
    logging_url = f"https://logging.googleapis.com/v2/projects/{project}/metrics"
    expected = f"{OWNER} {project}/{c['cluster']}/{c['namespace']}. "
    for metric in plan["metrics"]:
        url = logging_url + "/" + urllib.parse.quote(metric["name"], safe="")
        current = api("GET", url, missing_ok=True)
        if current and not current.get("description", "").startswith(expected):
            raise RuntimeError(f"Metric {metric['name']} already exists with another owner/scope. No changes applied.")
    required_services = {"compute.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
                         "securitycenter.googleapis.com"}
    enabled_services = gcloud("services", "list", "--enabled", "--project", project) or []
    enabled_names = {entry.get("config", {}).get("name") for entry in enabled_services}
    missing_services = sorted(required_services - enabled_names)
    if missing_services:
        gcloud("services", "enable", *missing_services, "--project", project)
    original = out / "subnet-before.json"
    if not original.exists():
        save(original, c["subnet_before"])
    # Preserve a previously enabled log configuration. Do not overwrite sampling or filters silently.
    current_log = c["subnet_before"].get("logConfig", {})
    if c.get("flow_backend", "compute") == "network-management":
        print("Using the verified Network Management configuration; existing Compute subnet settings preserved.")
    elif not current_log.get("enable"):
        gcloud("compute", "networks", "subnets", "update", c["subnet"], "--region", c["region"],
               "--project", project, "--enable-flow-logs", "--logging-aggregation-interval", "interval-5-sec",
               "--logging-flow-sampling", "1.0", "--logging-metadata", "include-all")
    else:
        print("Flow Logs already enabled: existing sampling/metadata/filter preserved. See plan warnings.")
    monitoring_url = f"https://monitoring.googleapis.com/v3/projects/{project}"
    for descriptor in plan["descriptors"]:
        # Monitoring metric types form the remaining resource path. Encoding '/' here
        # passes an invalid literal %2F name to Monitoring (unlike Logging's metricId).
        existing = api("GET", monitoring_url + "/metricDescriptors/" + urllib.parse.quote(descriptor["type"], safe="/"), missing_ok=True)
        if existing is None:
            api("POST", monitoring_url + "/metricDescriptors", descriptor)
        else:
            for field in ("metricKind", "valueType", "unit"):
                if existing.get(field) != descriptor[field]:
                    raise RuntimeError(f"Incompatible existing descriptor {descriptor['type']}: {field}")
            if {x['key'] for x in existing.get('labels', [])} != {x['key'] for x in descriptor.get('labels', [])}:
                raise RuntimeError(f"Incompatible labels for {descriptor['type']}")
    for metric in plan["metrics"]:
        url = logging_url + "/" + urllib.parse.quote(metric["name"], safe="")
        exists = api("GET", url, missing_ok=True)
        api("PUT" if exists else "POST", url if exists else logging_url, metric)
    policies = list_all(monitoring_url + "/alertPolicies", "alertPolicies")
    for desired in plan["policies"]:
        matches = [p for p in policies if p.get("userLabels", {}) == desired["userLabels"]]
        if len(matches) > 1:
            raise RuntimeError("Duplicate managed alert policies: resolve before reapplying.")
        body = copy.deepcopy(desired)
        if matches:
            body["name"] = matches[0]["name"]
            api("PATCH", "https://monitoring.googleapis.com/v3/" + body["name"], body)
        else:
            api("POST", monitoring_url + "/alertPolicies", body)
    dashboard_url = f"https://monitoring.googleapis.com/v1/projects/{project}/dashboards"
    dashboards = list_all(dashboard_url, "dashboards")
    existing = [d for d in dashboards if d.get("displayName") == plan["dashboard"]["displayName"]]
    if len(existing) > 1:
        raise RuntimeError("Multiple dashboards share the managed display name; resolve before reapplying.")
    dashboard = copy.deepcopy(plan["dashboard"])
    if existing:
        full = api("GET", "https://monitoring.googleapis.com/v1/" + existing[0]["name"])
        if OWNER not in full.get("labels", {}):
            raise RuntimeError("Dashboard with same title has a different owner; it will not be overwritten.")
        dashboard.update({"name": full["name"], "etag": full["etag"]})
        result = api("PATCH", "https://monitoring.googleapis.com/v1/" + full["name"], dashboard)
    else:
        result = api("POST", dashboard_url, dashboard)
    save(out / "applied.json", {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "dashboard": result["name"],
        "project": project, "scc_activated": False,
        "note": "API enabled only; verify/activate SCC Standard separately. No evidence of telemetry is implied."})
    print("Applied Monitoring/Logging resources. SCC requires the documented Standard activation check.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply", "evidence"])
    parser.add_argument("--project", default="crafty-dynamics-239114")
    parser.add_argument("--cluster")
    parser.add_argument("--location")
    parser.add_argument("--namespace", default="observability-lab")
    parser.add_argument("--notification-channel", action="append", default=[])
    parser.add_argument("--unexpected-port", type=int, default=4444)
    parser.add_argument("--flow-records-threshold", type=int, default=120)
    parser.add_argument("--auth-failures-threshold", type=int, default=4)
    parser.add_argument("--flow-backend", choices=("compute", "network-management"), default="compute")
    parser.add_argument("--flow-config-name", default="module-c-subnet-diagnostic")
    parser.add_argument("--flow-metric-resource", help="Verified Monitoring resource type for Network Management log metrics")
    parser.add_argument("--flow-creation-receipt", type=pathlib.Path, help="Saved successful creation receipt when API omits ownership labels")
    parser.add_argument("--sandbox-subnet", help="Exact discovered subnet name, mandatory for apply")
    parser.add_argument("--out", type=pathlib.Path, default=HERE / ".state")
    args = parser.parse_args(argv)
    if not 1 <= args.unexpected_port <= 65535 or args.flow_records_threshold < 1 or args.auth_failures_threshold < 0:
        parser.error("Invalid port or threshold.")
    if args.action == "evidence":
        plan_path = args.out / "plan.json"
        if not plan_path.exists():
            parser.error("evidence requires an existing plan.json; run plan/apply first")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        config = plan["config"]
        if args.project != config["project"]:
            parser.error("evidence project differs from the persisted plan")
        # Evidence is read-only and must preserve the approved channel configuration.
    else:
        config = discover(args)
        if args.flow_backend == "network-management":
            if not args.flow_metric_resource or not re.fullmatch(r"[a-z][a-z0-9_]*", args.flow_metric_resource):
                parser.error("Network Management requires --flow-metric-resource with the verified Monitoring resource type")
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.flow_config_name):
                parser.error("Invalid --flow-config-name")
        config.update({"flow_backend": args.flow_backend, "flow_config_name": args.flow_config_name,
            "flow_metric_resource": args.flow_metric_resource if args.flow_backend == "network-management" else "gce_subnetwork"})
        if args.flow_creation_receipt:
            config["flow_creation_receipt"] = str(args.flow_creation_receipt.resolve())
        plan = build(config)
        save(args.out / "plan.json", plan)
    print(json.dumps({key: config[key] for key in ("project", "parent", "cluster", "location", "network", "subnet", "autopilot", "intranode_visibility")}, indent=2))
    if not config["intranode_visibility"] and not config["autopilot"]:
        print("WARNING: no confirmed intranode visibility; same-node traffic may be absent. No cluster change is made.")
    logging = config["subnet_before"].get("logConfig", {})
    if logging.get("enable") and (logging.get("metadata") != "INCLUDE_ALL_METADATA" or logging.get("filterExpr", "true") != "true"):
        print("WARNING: existing Flow Logs metadata/filter may hide pod flows. Inspect logConfig in plan.json; it will be preserved.")
    if not config["channels"]:
        print("WARNING: incidents will be visible in Monitoring only; no notification channel configured.")
    if args.action == "apply":
        if args.sandbox_subnet != config["subnet"]:
            parser.error("apply requires --sandbox-subnet with the exact discovered dedicated sandbox subnet name.")
        apply(plan, args.out)
    elif args.action == "evidence":
        for metric in plan["metrics"]:
            entries = gcloud("logging", "read", metric["filter"], "--project", args.project, "--freshness", "1h", "--limit", "100")
            save(args.out / (metric["name"].split("/")[-1] + "-evidence.json"), entries)
        print("Saved raw evidence. Empty arrays mean no observed events, not a passed validation.")
    else:
        print(f"Read-only plan saved to {args.out / 'plan.json'}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
