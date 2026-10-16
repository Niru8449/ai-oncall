import json
import math
import time
from dataclasses import asdict, dataclass

PROM_PROXY = "/api/v1/namespaces/monitoring/services/http:mon-kube-prometheus-stack-prometheus:9090/proxy"
SUCCESS_MIN = 0.99
P95_MAX_S = 0.3
STUCK_WINDOW_S = 90
QUEUE_MAX = 100
POLL_S = 5
NS = 'namespace="storefront"'


class PrometheusError(Exception):
    pass


class Prometheus:
    def __init__(self, api_client):
        self.api = api_client

    def _get(self, path, params=None):
        response = self.api.call_api(
            PROM_PROXY + path,
            "GET",
            query_params=list((params or {}).items()),
            auth_settings=["BearerToken"],
            _preload_content=False,
            _request_timeout=15,
        )
        body = json.loads(response.data)
        if body.get("status") != "success":
            raise PrometheusError(f"{path}: {body.get('error') or body}")
        return body["data"]

    def query(self, promql):
        return self._get("/api/v1/query", {"query": promql})["result"]

    def scalar(self, promql):
        result = self.query(promql)
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value

    def alerts(self):
        return self._get("/api/v1/alerts")["alerts"]

    def firing(self):
        return {a["labels"].get("alertname"): a.get("activeAt") for a in self.alerts() if a.get("state") == "firing"}

    def active(self):
        return {a["labels"].get("alertname"): a.get("state") for a in self.alerts() if a.get("state") in ("pending", "firing")}


@dataclass
class Health:
    healthy: bool
    reason: str | None
    success_ratio: float | None = None
    p95_latency_s: float | None = None
    replicas_missing: float | None = None
    restarts: float | None = None
    rollout_stuck: float | None = None
    queue_depth: float | None = None
    queue_growth_per_s: float | None = None
    new_evictions: float | None = None

    def as_dict(self):
        return asdict(self)


def health(prom, window_s=60):
    w = f"{window_s}s"
    success = prom.scalar(
        f'sum(increase(probe_requests_total{{result="ok"}}[{w}])) / sum(increase(probe_requests_total[{w}]))'
    )
    p95 = prom.scalar(f"histogram_quantile(0.95, sum by (le) (rate(probe_latency_seconds_bucket[{w}])))")
    missing = prom.scalar(f"sum(kube_deployment_spec_replicas{{{NS}}} - kube_deployment_status_replicas_ready{{{NS}}})")
    restarts = prom.scalar(f"sum(increase(kube_pod_container_status_restarts_total{{{NS}}}[{w}]))")
    stuck = prom.scalar(
        f"sum(min_over_time((kube_deployment_spec_replicas{{{NS}}}"
        f" - kube_deployment_status_replicas_updated{{{NS}}})[{STUCK_WINDOW_S}s:15s]))"
    )
    queue = prom.scalar(f"max(queue_depth{{{NS}}})")
    growth = prom.scalar(f"deriv(max(queue_depth{{{NS}}})[{w}:15s])")
    evicted = f'kube_pod_status_reason{{{NS},reason="Evicted"}}'
    evictions = prom.scalar(f"(count({evicted} == 1) or vector(0)) - (count({evicted} offset {w} == 1) or vector(0))")
    checks = (
        (success is not None and success >= SUCCESS_MIN, f"probe success {success} < {SUCCESS_MIN}"),
        (p95 is not None and p95 < P95_MAX_S, f"probe p95 {p95}s >= {P95_MAX_S}s"),
        (missing is not None and missing == 0, f"{missing} deployment replicas not ready"),
        (restarts is not None and restarts == 0, f"{restarts} container restarts in {w}"),
        (stuck is not None and stuck == 0, f"a rollout has been stuck for {STUCK_WINDOW_S}s"),
        ((queue or 0) <= QUEUE_MAX or (growth or 0) <= 0, f"job queue at {queue} and growing {growth}/s"),
        ((evictions or 0) <= 0, f"{evictions} pods evicted in the last {w}"),
    )
    reason = next((text for ok, text in checks if not ok), None)
    return Health(reason is None, reason, success, p95, missing, restarts, stuck, queue, growth, evictions)


def wait_for(prom, healthy, timeout_s, window_s=60):
    start = time.monotonic()
    last = None
    while True:
        try:
            last = health(prom, window_s)
            if last.healthy == healthy:
                return last, round(time.monotonic() - start)
        except Exception as exc:
            last = Health(False, f"health query failed: {exc}")
        if time.monotonic() - start >= timeout_s:
            return last, None
        time.sleep(POLL_S)


def label_match(expected, diagnosis):
    return bool(diagnosis) and diagnosis.get("root_cause") == expected
