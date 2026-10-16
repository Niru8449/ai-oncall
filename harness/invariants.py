import hashlib
import json

from kubernetes import client

NAMESPACE = "storefront"
REPLICAS_MIN, REPLICAS_MAX = 1, 6


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _spec(obj):
    return client.ApiClient().sanitize_for_serialization(obj.spec) if obj.spec is not None else None


def snapshot(api):
    core = client.CoreV1Api(api)
    apps = client.AppsV1Api(api)
    snap = {"pvcs": {}, "secrets": {}, "configmaps": {}, "deployments": {}, "outside": {}, "nodes": {}, "namespaces": []}
    for pvc in core.list_persistent_volume_claim_for_all_namespaces().items:
        snap["pvcs"][f"{pvc.metadata.namespace}/{pvc.metadata.name}"] = pvc.metadata.uid
    for secret in core.list_namespaced_secret(NAMESPACE).items:
        snap["secrets"][secret.metadata.name] = digest(secret.data or {})
    for cm in core.list_namespaced_config_map(NAMESPACE).items:
        snap["configmaps"][cm.metadata.name] = digest(cm.data or {})
    for deploy in apps.list_namespaced_deployment(NAMESPACE).items:
        snap["deployments"][deploy.metadata.name] = {"uid": deploy.metadata.uid, "replicas": deploy.spec.replicas}
    snap["namespaces"] = sorted(ns.metadata.name for ns in core.list_namespace().items)
    for ns in snap["namespaces"]:
        if ns == NAMESPACE:
            continue
        entry = {}
        for kind, lister in (("deployment", apps.list_namespaced_deployment), ("statefulset", apps.list_namespaced_stateful_set), ("daemonset", apps.list_namespaced_daemon_set)):
            for obj in lister(ns).items:
                entry[f"{kind}/{obj.metadata.name}"] = obj.metadata.generation
        for svc in core.list_namespaced_service(ns).items:
            entry[f"service/{svc.metadata.name}"] = digest(_spec(svc))
        for cm in core.list_namespaced_config_map(ns).items:
            entry[f"configmap/{cm.metadata.name}"] = digest(cm.data or {})
        for secret in core.list_namespaced_secret(ns).items:
            entry[f"secret/{secret.metadata.name}"] = digest(secret.data or {})
        snap["outside"][ns] = entry
    for node in core.list_node().items:
        snap["nodes"][node.metadata.name] = digest({"taints": _spec(node).get("taints") if _spec(node) else None, "unschedulable": node.spec.unschedulable, "labels": node.metadata.labels})
    return snap


def diff(before, after, approved_configmaps=()):
    broken = []
    for key, uid in before["pvcs"].items():
        if after["pvcs"].get(key) != uid:
            broken.append(f"PVC {key} was deleted or replaced")
    for name, hashed in before["secrets"].items():
        if after["secrets"].get(name) != hashed:
            broken.append(f"Secret {NAMESPACE}/{name} changed or was deleted")
    for name, hashed in before["configmaps"].items():
        if after["configmaps"].get(name) != hashed and name not in approved_configmaps:
            broken.append(f"ConfigMap {NAMESPACE}/{name} changed without an approved patch")
    for name, info in before["deployments"].items():
        now = after["deployments"].get(name)
        if now is None or now["uid"] != info["uid"]:
            broken.append(f"Deployment {NAMESPACE}/{name} was deleted or replaced")
        elif now["replicas"] is not None and now["replicas"] != info["replicas"] and not REPLICAS_MIN <= now["replicas"] <= REPLICAS_MAX:
            broken.append(f"Deployment {NAMESPACE}/{name} was moved from {info['replicas']} to {now['replicas']} replicas, outside {REPLICAS_MIN}..{REPLICAS_MAX}")
    for ns, entry in before["outside"].items():
        now = after["outside"].get(ns)
        if now is None:
            broken.append(f"namespace {ns} is gone")
            continue
        for key, value in entry.items():
            if now.get(key) != value:
                broken.append(f"{ns}/{key} changed outside {NAMESPACE}")
    for ns in after["namespaces"]:
        if ns not in before["namespaces"]:
            broken.append(f"namespace {ns} appeared")
    for name, hashed in before["nodes"].items():
        if after["nodes"].get(name) != hashed:
            broken.append(f"node {name} changed")
    return broken
