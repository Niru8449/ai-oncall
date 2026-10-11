import json
import math
import time
from dataclasses import dataclass

PROM_PROXY = "/api/v1/namespaces/monitoring/services/http:mon-kube-prometheus-stack-prometheus:9090/proxy"
SUCCESS_MIN = 0.99
P95_MAX_S = 0.3
POLL_S = 5


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
        return {a["labels"].get("alertname") for a in self.alerts() if a.get("state") == "firing"}


@dataclass
class Health:
    success_ratio: float | None
    p95_latency_s: float | None
    replicas_missing: float | None
    restarts: float | None
    healthy: bool
    reason: str | None

    def as_dict(self):
        return {
            "success_ratio": self.success_ratio,
            "p95_latency_s": self.p95_latency_s,
            "replicas_missing": self.replicas_missing,
            "restarts": self.restarts,
            "healthy": self.healthy,
            "reason": self.reason,
        }


def health(prom, window_s=60):
    w = f"{window_s}s"
    success = prom.scalar(
        f'sum(increase(probe_requests_total{{result="ok"}}[{w}])) / sum(increase(probe_requests_total[{w}]))'
    )
    p95 = prom.scalar(f"histogram_quantile(0.95, sum by (le) (rate(probe_latency_seconds_bucket[{w}])))")
    missing = prom.scalar(
        'sum(kube_deployment_spec_replicas{namespace="storefront"}'
        ' - kube_deployment_status_replicas_ready{namespace="storefront"})'
    )
    restarts = prom.scalar(f'sum(increase(kube_pod_container_status_restarts_total{{namespace="storefront"}}[{w}]))')
    checks = (
        (success is not None and success >= SUCCESS_MIN, f"probe success {success} < {SUCCESS_MIN}"),
        (p95 is not None and p95 < P95_MAX_S, f"probe p95 {p95}s >= {P95_MAX_S}s"),
        (missing is not None and missing == 0, f"{missing} deployment replicas not ready"),
        (restarts is not None and restarts == 0, f"{restarts} container restarts in {w}"),
    )
    reason = next((text for ok, text in checks if not ok), None)
    return Health(success, p95, missing, restarts, reason is None, reason)


def wait_for(prom, healthy, timeout_s, window_s=60):
    start = time.monotonic()
    last = None
    while True:
        try:
            last = health(prom, window_s)
            if last.healthy == healthy:
                return last, round(time.monotonic() - start)
        except Exception as exc:
            last = Health(None, None, None, None, False, f"health query failed: {exc}")
        if time.monotonic() - start >= timeout_s:
            return last, None
        time.sleep(POLL_S)
