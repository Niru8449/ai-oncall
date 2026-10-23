import json
import re
import subprocess
from pathlib import Path

from kubernetes import client

NAMESPACE = "storefront"
SANDBOXES = Path.home() / ".oncall" / "sandboxes"
MAX_CHARS = 6000
LOG_LINES = 200
EVENT_LINES = 60
PROM_PROXY = "/api/v1/namespaces/monitoring/services/http:mon-kube-prometheus-stack-prometheus:9090/proxy"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
FORBIDDEN_METRIC_RE = re.compile(r"\bprobe_[a-z_]*")
EMPTY_RESULT_HINT = (
    "(empty result: no series matched this query)\n"
    "App metrics that exist: api_requests_total{path=\"/orders\"|\"/healthz\"|\"/ready\",status}, api_request_seconds_bucket{path,le}, "
    "queue_depth, worker_jobs_processed_total, worker_errors_total. Also kube_* (kube-state-metrics), node_*, up, ALERTS. "
    "There is no http_requests_total."
)
KINDS = {
    "pods": "pods", "pod": "pods",
    "deployments": "deployments.apps", "deployment": "deployments.apps", "deploy": "deployments.apps",
    "replicasets": "replicasets.apps", "replicaset": "replicasets.apps", "rs": "replicasets.apps",
    "statefulsets": "statefulsets.apps", "statefulset": "statefulsets.apps", "sts": "statefulsets.apps",
    "services": "services", "service": "services", "svc": "services",
    "endpoints": "endpoints", "configmaps": "configmaps", "configmap": "configmaps", "cm": "configmaps",
    "hpa": "horizontalpodautoscalers.autoscaling", "horizontalpodautoscalers": "horizontalpodautoscalers.autoscaling",
    "pdb": "poddisruptionbudgets.policy", "poddisruptionbudgets": "poddisruptionbudgets.policy",
    "jobs": "jobs.batch", "job": "jobs.batch", "cronjobs": "cronjobs.batch", "cronjob": "cronjobs.batch",
    "pvc": "persistentvolumeclaims", "persistentvolumeclaims": "persistentvolumeclaims",
    "networkpolicies": "networkpolicies.networking.k8s.io", "networkpolicy": "networkpolicies.networking.k8s.io", "netpol": "networkpolicies.networking.k8s.io",
    "nodes": "nodes", "node": "nodes",
}
CLUSTER_SCOPED = {"nodes"}


class ToolError(Exception):
    pass


class ReadTools:
    def __init__(self, sandbox, api_client):
        self.sandbox = sandbox
        self.api = api_client
        self.kubeconfig = SANDBOXES / sandbox / "agent.kubeconfig"
        self.context = f"kind-{sandbox}-agent"

    def _kubectl(self, *args):
        argv = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.context, *args]
        result = subprocess.run(argv, text=True, capture_output=True, timeout=60)
        out = result.stdout
        if result.returncode != 0:
            out = (out + "\n" + result.stderr).strip()
        return out.strip() or "(no output)"

    @staticmethod
    def _resource(kind):
        try:
            return KINDS[kind.lower()]
        except KeyError:
            raise ToolError(f"unknown kind {kind!r}; use one of {sorted(set(KINDS))}")

    @staticmethod
    def _name(name):
        if not NAME_RE.match(name or ""):
            raise ToolError(f"invalid name {name!r}")
        return name

    def _scope(self, resource):
        return [] if resource in CLUSTER_SCOPED else ["-n", NAMESPACE]

    def get(self, kind, name=None):
        resource = self._resource(kind)
        args = ["get", resource, *self._scope(resource), "-o", "wide"]
        if name:
            args.append(self._name(name))
        return wrap(f"kubectl get {resource}" + (f" {name}" if name else ""), self._kubectl(*args))

    def describe(self, kind, name):
        resource = self._resource(kind)
        return wrap(f"kubectl describe {resource} {name}", self._kubectl("describe", resource, *self._scope(resource), self._name(name)))

    def logs(self, pod, container=None, previous=False):
        args = ["logs", "-n", NAMESPACE, self._name(pod), f"--tail={LOG_LINES}"]
        if container:
            args += ["-c", self._name(container)]
        if previous:
            args.append("--previous")
        return wrap(f"logs/{pod}" + (f"/{container}" if container else "") + (" (previous)" if previous else ""), self._kubectl(*args))

    def events(self):
        out = self._kubectl("get", "events", "-n", NAMESPACE, "--sort-by=.lastTimestamp")
        lines = out.splitlines()
        if len(lines) > EVENT_LINES + 1:
            lines = [lines[0]] + lines[-EVENT_LINES:]
        return wrap("kubectl get events", "\n".join(lines))

    def rollout_history(self, deployment):
        return wrap(f"kubectl rollout history deployment/{deployment}", self._kubectl("rollout", "history", "-n", NAMESPACE, f"deployment/{self._name(deployment)}"))

    def promql(self, query):
        if FORBIDDEN_METRIC_RE.search(query):
            raise ToolError("probe_* metrics belong to the scorer and are not available to the agent")
        response = self.api.call_api(
            PROM_PROXY + "/api/v1/query", "GET",
            query_params=[("query", query)], auth_settings=["BearerToken"],
            _preload_content=False, _request_timeout=15,
        )
        body = json.loads(response.data)
        if body.get("status") != "success":
            raise ToolError(f"prometheus: {body.get('error') or body}")
        data = body.get("data") or {}
        result = data.get("result")
        kind = data.get("resultType")
        if kind in ("scalar", "string"):
            value = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else result
            return wrap(f"promql {query}", f"{kind}: {value}")
        rows = []
        for item in result or []:
            if not isinstance(item, dict):
                rows.append(str(item))
                continue
            labels = ",".join(f"{k}={v}" for k, v in sorted((item.get("metric") or {}).items()))
            if "values" in item:
                points = item["values"] or []
                first, last = (points[0][1] if points else None), (points[-1][1] if points else None)
                rows.append(f"{{{labels}}} {len(points)} samples, first {first}, last {last}")
            else:
                value = (item.get("value") or [None, None])[1]
                rows.append(f"{{{labels}}} {value}")
        return wrap(f"promql {query}", "\n".join(rows) or EMPTY_RESULT_HINT)


def trim(text, limit=MAX_CHARS):
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return text[:head] + f"\n[... trimmed {len(text) - head - tail} chars ...]\n" + text[-tail:]


def wrap(source, text):
    return f'<untrusted source="{source}">\n{trim(text)}\n</untrusted>'


TOOL_SPECS = [
    {"type": "function", "function": {"name": "k8s_get", "description": "List objects of one kind in the storefront namespace (or nodes), like kubectl get -o wide. Optional name for one object.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}}, "required": ["kind"]}}},
    {"type": "function", "function": {"name": "k8s_describe", "description": "kubectl describe for one object: spec, status, conditions and recent events.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string"}, "name": {"type": "string"}}, "required": ["kind", "name"]}}},
    {"type": "function", "function": {"name": "k8s_logs", "description": "Last 200 log lines of a pod. previous=true reads the crashed previous container.",
        "parameters": {"type": "object", "properties": {"pod": {"type": "string"}, "container": {"type": "string"}, "previous": {"type": "boolean"}}, "required": ["pod"]}}},
    {"type": "function", "function": {"name": "k8s_events", "description": "Recent events in the storefront namespace, oldest first.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "rollout_history", "description": "Revision history of a Deployment in storefront.",
        "parameters": {"type": "object", "properties": {"deployment": {"type": "string"}}, "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "promql", "description": "Run one instant PromQL query against the cluster's Prometheus (app, kube-state and node metrics).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]


HANDLERS = {
    "k8s_get": lambda t, a: t.get(a["kind"], a.get("name")),
    "k8s_describe": lambda t, a: t.describe(a["kind"], a["name"]),
    "k8s_logs": lambda t, a: t.logs(a["pod"], a.get("container"), bool(a.get("previous"))),
    "k8s_events": lambda t, a: t.events(),
    "rollout_history": lambda t, a: t.rollout_history(a["deployment"]),
    "promql": lambda t, a: t.promql(a["query"]),
}


def call(tools, name, arguments):
    if name not in HANDLERS:
        raise ToolError(f"unknown tool {name!r}")
    try:
        return HANDLERS[name](tools, arguments or {})
    except KeyError as exc:
        raise ToolError(f"{name}: missing argument {exc}")
