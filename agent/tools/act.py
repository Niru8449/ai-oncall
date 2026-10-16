import copy
import datetime as dt
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from kubernetes.utils import parse_quantity

NAMESPACE = "storefront"
SANDBOXES = Path.home() / ".oncall" / "sandboxes"
AUTO = {"rollout_restart", "rollback", "scale", "raise_limits", "delete_pod"}
PROPOSE = {"patch_probe", "patch_env", "patch_configmap", "patch_service", "patch_hpa", "patch_pdb", "patch_affinity", "patch_resources"}
CONTROL = {"escalate", "finish"}
REPLICAS_MIN, REPLICAS_MAX = 1, 6
LIMIT_FACTOR_MAX = 2
RAISE_CAPS = {"memory": parse_quantity("1Gi"), "cpu": parse_quantity("1")}
RESOURCE_CAPS = {"memory": parse_quantity("2Gi"), "cpu": parse_quantity("2"), "ephemeral-storage": parse_quantity("10Gi")}
PROBE_INT_MAX = 600
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEY_RE = re.compile(r"^[-._a-zA-Z0-9]+$")
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
POD_TEMPLATE_HASH = "pod-template-hash"
PROBES = ("liveness", "readiness", "startup")
TOLERATION_KEYS = {"key", "operator", "value", "effect", "tolerationSeconds"}
RESOURCES = {
    "deploy": "deployments.apps",
    "svc": "services",
    "cm": "configmaps",
    "hpa": "horizontalpodautoscalers.autoscaling",
    "pdb": "poddisruptionbudgets.policy",
    "pod": "pods",
    "rs": "replicasets.apps",
}


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Intent:
    tool: str
    tier: str
    target: str
    resource: str
    name: str
    summary: str
    before: dict
    patch_type: str | None = None
    patch: object = None
    allowed: tuple = ()
    inverse: tuple | None = None
    delete: bool = False
    details: dict = field(default_factory=dict)

    @property
    def namespace(self):
        return self.before.get("metadata", {}).get("namespace")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bounds(message):
    raise Refusal("bounds", message)


def valid_name(name, what="name"):
    if not isinstance(name, str) or not NAME_RE.match(name):
        bounds(f"invalid {what} {name!r}")
    return name


def quantity(value, what):
    try:
        return parse_quantity(str(value))
    except (ValueError, TypeError):
        bounds(f"{what} {value!r} is not a Kubernetes quantity (examples: 256Mi, 500m, 1)")


def positive_int(value, what, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 1 or value > maximum:
        bounds(f"{what} must be a whole number between 1 and {maximum}, got {value!r}")
    return int(value)


def template_restore(before):
    return ("json", [{"op": "replace", "path": "/spec/template", "value": before["spec"]["template"]}])


class ActTools:
    def __init__(self, sandbox, revisions_at_alert=None):
        self.sandbox = sandbox
        self.kubeconfig = SANDBOXES / sandbox / "agent.kubeconfig"
        self.context = f"kind-{sandbox}-agent"
        self.revisions_at_alert = {k: int(v) for k, v in (revisions_at_alert or {}).items()}

    def _kubectl(self, *args):
        argv = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.context, *args]
        result = subprocess.run(argv, text=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise Refusal("read", (result.stderr or result.stdout).strip()[:400])
        return result.stdout

    def _get(self, short, name):
        valid_name(name)
        return json.loads(self._kubectl("get", RESOURCES[short], "-n", NAMESPACE, name, "-o", "json"))

    def _list(self, short):
        return json.loads(self._kubectl("get", RESOURCES[short], "-n", NAMESPACE, "-o", "json"))["items"]

    @staticmethod
    def _container(deployment, container):
        containers = deployment["spec"]["template"]["spec"]["containers"]
        if container is None:
            return containers[0]
        for c in containers:
            if c["name"] == container:
                return c
        bounds(f"deployment {deployment['metadata']['name']} has no container {container!r}; it has {[c['name'] for c in containers]}")

    def _deployment_intent(self, tool, tier, deployment, summary, patch_type, patch, allowed, inverse="template", details=None):
        return Intent(
            tool=tool, tier=tier, target=f"deploy/{deployment['metadata']['name']}", resource=RESOURCES["deploy"],
            name=deployment["metadata"]["name"], summary=summary, before=deployment, patch_type=patch_type, patch=patch,
            allowed=tuple(allowed), inverse=template_restore(deployment) if inverse == "template" else inverse, details=details or {},
        )

    # ---- act-alone tools ----

    def rollout_restart(self, deployment):
        obj = self._get("deploy", deployment)
        stamp = now_iso()
        patch = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}}
        return self._deployment_intent(
            "rollout_restart", "auto", obj, f"restart the pods of deployment {deployment}", "strategic", patch,
            [f"spec.template.metadata.annotations.{RESTART_ANNOTATION}"], inverse=None,
        )

    def rollback(self, deployment, to_revision=None):
        obj = self._get("deploy", deployment)
        live = int(obj["metadata"].get("annotations", {}).get(REVISION_ANNOTATION, 0) or 0)
        at_alert = self.revisions_at_alert.get(deployment, live)
        target = at_alert - 1 if to_revision is None else positive_int(to_revision, "to_revision", 10_000)
        if target < 1:
            bounds(f"deployment {deployment} has no revision before {at_alert}; nothing to roll back to")
        if target >= at_alert:
            bounds(f"rollback only goes backwards: revision {target} is not before the revision live at alert time ({at_alert})")
        uid = obj["metadata"]["uid"]
        candidates = {}
        for rs in self._list("rs"):
            owners = [o for o in rs["metadata"].get("ownerReferences", []) if o.get("uid") == uid]
            revision = rs["metadata"].get("annotations", {}).get(REVISION_ANNOTATION)
            if owners and revision:
                candidates[int(revision)] = rs
        if target not in candidates:
            bounds(f"deployment {deployment} keeps no ReplicaSet for revision {target}; it has {sorted(candidates)}")
        template = copy.deepcopy(candidates[target]["spec"]["template"])
        template.get("metadata", {}).get("labels", {}).pop(POD_TEMPLATE_HASH, None)
        if template == obj["spec"]["template"]:
            bounds(f"revision {target} has the same pod template as the live deployment; a rollback would change nothing")
        patch = [{"op": "replace", "path": "/spec/template", "value": template}]
        image = next((c.get("image") for c in template["spec"]["containers"]), None)
        return self._deployment_intent(
            "rollback", "auto", obj, f"roll deployment {deployment} back from revision {live} to revision {target} ({image})",
            "json", patch, ["spec.template"], details={"from_revision": live, "to_revision": target, "revision_at_alert": at_alert},
        )

    def scale(self, deployment, replicas):
        obj = self._get("deploy", deployment)
        replicas = positive_int(replicas, "replicas", REPLICAS_MAX)
        if replicas < REPLICAS_MIN:
            bounds(f"replicas must be at least {REPLICAS_MIN}")
        current = obj["spec"].get("replicas", 1)
        if replicas == current:
            bounds(f"deployment {deployment} already has {current} replicas")
        return self._deployment_intent(
            "scale", "auto", obj, f"scale deployment {deployment} from {current} to {replicas} replicas", "merge",
            {"spec": {"replicas": replicas}}, ["spec.replicas"], inverse=("merge", {"spec": {"replicas": current}}),
            details={"from": current, "to": replicas},
        )

    def raise_limits(self, deployment, memory=None, cpu=None, container=None):
        obj = self._get("deploy", deployment)
        target = self._container(obj, container)
        limits = target.get("resources", {}).get("limits", {}) or {}
        new_limits = {}
        for key, value in (("memory", memory), ("cpu", cpu)):
            if value is None:
                continue
            wanted = quantity(value, f"{key} limit")
            if key not in limits:
                bounds(f"container {target['name']} has no {key} limit to raise; propose patch_resources instead")
            current = parse_quantity(limits[key])
            if wanted <= current:
                bounds(f"raise_limits never lowers: {key} limit is {limits[key]}, asked {value}")
            if wanted > current * LIMIT_FACTOR_MAX:
                bounds(f"{key} limit {value} is more than {LIMIT_FACTOR_MAX}x the current {limits[key]}")
            if wanted > RAISE_CAPS[key]:
                bounds(f"{key} limit {value} is above the act-alone cap ({'1Gi' if key == 'memory' else '1 CPU'}); propose patch_resources instead")
            new_limits[key] = str(value)
        if not new_limits:
            bounds("give a memory or cpu limit to raise")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": target["name"], "resources": {"limits": new_limits}}]}}}}
        text = ", ".join(f"{k} {limits[k]} -> {v}" for k, v in new_limits.items())
        return self._deployment_intent(
            "raise_limits", "auto", obj, f"raise limits on {deployment}/{target['name']}: {text}", "strategic", patch,
            [f"spec.template.spec.containers[{target['name']}].resources.limits"], details={"from": limits, "to": new_limits},
        )

    def delete_pod(self, pod):
        obj = self._get("pod", pod)
        owners = obj["metadata"].get("ownerReferences", [])
        rs_owner = next((o for o in owners if o.get("kind") == "ReplicaSet"), None)
        if rs_owner is None:
            bounds(f"pod {pod} is not owned by a ReplicaSet; only Deployment pods may be deleted")
        rs = self._get("rs", rs_owner["name"])
        deploy_owner = next((o for o in rs["metadata"].get("ownerReferences", []) if o.get("kind") == "Deployment"), None)
        if deploy_owner is None:
            bounds(f"pod {pod} belongs to ReplicaSet {rs_owner['name']}, which no Deployment owns")
        return Intent(
            tool="delete_pod", tier="auto", target=f"pod/{pod}", resource=RESOURCES["pod"], name=pod,
            summary=f"delete pod {pod} so deployment {deploy_owner['name']} replaces it", before=obj, delete=True,
            details={"deployment": deploy_owner["name"]},
        )

    # ---- propose tools ----

    def patch_probe(self, deployment, probe, container=None, path=None, port=None, initial_delay_s=None, period_s=None, timeout_s=None, failure_threshold=None):
        obj = self._get("deploy", deployment)
        target = self._container(obj, container)
        if probe not in PROBES:
            bounds(f"probe must be one of {PROBES}")
        key = f"{probe}Probe"
        patch_probe = {}
        if path is not None:
            if not isinstance(path, str) or not path.startswith("/"):
                bounds(f"path must start with /, got {path!r}")
            patch_probe.setdefault("httpGet", {})["path"] = path
        if port is not None:
            patch_probe.setdefault("httpGet", {})["port"] = positive_int(port, "port", 65535)
        for name, value in (("initialDelaySeconds", initial_delay_s), ("periodSeconds", period_s), ("timeoutSeconds", timeout_s), ("failureThreshold", failure_threshold)):
            if value is not None:
                patch_probe[name] = positive_int(value, name, PROBE_INT_MAX)
        if not patch_probe:
            bounds("give at least one probe field to change")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": target["name"], key: patch_probe}]}}}}
        return self._deployment_intent(
            "patch_probe", "propose", obj, f"change the {probe} probe of {deployment}/{target['name']}: {json.dumps(patch_probe)}",
            "strategic", patch, [f"spec.template.spec.containers[{target['name']}].{key}"], details={"from": target.get(key), "to": patch_probe},
        )

    def patch_env(self, deployment, set=None, unset=None, container=None):
        obj = self._get("deploy", deployment)
        target = self._container(obj, container)
        entries = []
        set = set or {}
        unset = unset or []
        if not isinstance(set, dict) or not isinstance(unset, list):
            bounds("set must be an object of NAME: value and unset a list of names")
        for name, value in set.items():
            if not ENV_NAME_RE.match(str(name)):
                bounds(f"invalid environment variable name {name!r}")
            entries.append({"name": name, "value": str(value)})
        for name in unset:
            if not ENV_NAME_RE.match(str(name)):
                bounds(f"invalid environment variable name {name!r}")
            entries.append({"name": name, "$patch": "delete"})
        if not entries:
            bounds("give variables to set or unset")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": target["name"], "env": entries}]}}}}
        current = {e["name"]: e.get("value") for e in target.get("env", []) if "value" in e}
        text = ", ".join([f"{k}={v}" for k, v in set.items()] + [f"unset {k}" for k in unset])
        return self._deployment_intent(
            "patch_env", "propose", obj, f"change environment of {deployment}/{target['name']}: {text}", "strategic", patch,
            [f"spec.template.spec.containers[{target['name']}].env"], details={"from": {k: current.get(k) for k in list(set) + list(unset)}, "to": set, "unset": unset},
        )

    def patch_configmap(self, configmap, set=None, remove=None):
        obj = self._get("cm", configmap)
        set = set or {}
        remove = remove or []
        if not isinstance(set, dict) or not isinstance(remove, list):
            bounds("set must be an object of key: value and remove a list of keys")
        data = {}
        for key, value in set.items():
            if not KEY_RE.match(str(key)):
                bounds(f"invalid ConfigMap key {key!r}")
            data[key] = str(value)
        for key in remove:
            if not KEY_RE.match(str(key)):
                bounds(f"invalid ConfigMap key {key!r}")
            data[key] = None
        if not data:
            bounds("give keys to set or remove")
        current = obj.get("data") or {}
        inverse = {"data": {key: current.get(key) for key in data}}
        return Intent(
            tool="patch_configmap", tier="propose", target=f"cm/{configmap}", resource=RESOURCES["cm"], name=configmap,
            summary=f"change ConfigMap {configmap}: {json.dumps(data)}", before=obj, patch_type="merge", patch={"data": data},
            allowed=("data",), inverse=("merge", inverse), details={"from": {k: current.get(k) for k in data}, "to": data},
        )

    def patch_service(self, service, selector=None, port=None, target_port=None, port_name=None):
        obj = self._get("svc", service)
        spec_patch = {}
        current_selector = obj["spec"].get("selector") or {}
        if selector is not None:
            if not isinstance(selector, dict) or not selector:
                bounds("selector must be a non-empty object of label: value")
            for key, value in selector.items():
                if value is not None and not isinstance(value, str):
                    bounds(f"selector value for {key!r} must be a string or null")
            spec_patch["selector"] = selector
        ports = copy.deepcopy(obj["spec"].get("ports") or [])
        if port is not None or target_port is not None:
            if not ports:
                bounds(f"service {service} has no ports to change")
            chosen = ports[0] if port_name is None else next((p for p in ports if p.get("name") == port_name), None)
            if chosen is None:
                bounds(f"service {service} has no port named {port_name!r}")
            if port is not None:
                chosen["port"] = positive_int(port, "port", 65535)
            if target_port is not None:
                if isinstance(target_port, str):
                    valid_name(target_port, "target_port name")
                    chosen["targetPort"] = target_port
                else:
                    chosen["targetPort"] = positive_int(target_port, "target_port", 65535)
            spec_patch["ports"] = ports
        if not spec_patch:
            bounds("give a selector, a port or a target_port to change")
        inverse_selector = {key: current_selector.get(key) for key in (selector or {})}
        inverse = {"spec": {"selector": inverse_selector, "ports": obj["spec"].get("ports")}}
        return Intent(
            tool="patch_service", tier="propose", target=f"svc/{service}", resource=RESOURCES["svc"], name=service,
            summary=f"change Service {service}: {json.dumps(spec_patch)}", before=obj, patch_type="merge", patch={"spec": spec_patch},
            allowed=("spec.selector", "spec.ports"), inverse=("merge", inverse),
            details={"from": {"selector": current_selector, "ports": obj["spec"].get("ports")}, "to": spec_patch},
        )

    def patch_hpa(self, hpa, min_replicas=None, max_replicas=None, target_cpu_percent=None, target_deployment=None):
        obj = self._get("hpa", hpa)
        spec = obj["spec"]
        spec_patch = {}
        low = spec.get("minReplicas", 1) if min_replicas is None else positive_int(min_replicas, "min_replicas", REPLICAS_MAX)
        high = spec.get("maxReplicas") if max_replicas is None else positive_int(max_replicas, "max_replicas", REPLICAS_MAX)
        if low > high:
            bounds(f"min_replicas {low} is above max_replicas {high}")
        if min_replicas is not None:
            spec_patch["minReplicas"] = low
        if max_replicas is not None:
            spec_patch["maxReplicas"] = high
        if target_cpu_percent is not None:
            spec_patch["metrics"] = [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": positive_int(target_cpu_percent, "target_cpu_percent", 100)}}}]
        if target_deployment is not None:
            self._get("deploy", target_deployment)
            spec_patch["scaleTargetRef"] = {"apiVersion": "apps/v1", "kind": "Deployment", "name": target_deployment}
        if not spec_patch:
            bounds("give min_replicas, max_replicas, target_cpu_percent or target_deployment")
        inverse = {"spec": {key: spec.get(key) for key in spec_patch}}
        return Intent(
            tool="patch_hpa", tier="propose", target=f"hpa/{hpa}", resource=RESOURCES["hpa"], name=hpa,
            summary=f"change HPA {hpa}: {json.dumps(spec_patch)}", before=obj, patch_type="merge", patch={"spec": spec_patch},
            allowed=("spec",), inverse=("merge", inverse), details={"from": {k: spec.get(k) for k in spec_patch}, "to": spec_patch},
        )

    def patch_pdb(self, pdb, min_available=None, max_unavailable=None):
        obj = self._get("pdb", pdb)
        if (min_available is None) == (max_unavailable is None):
            bounds("give exactly one of min_available or max_unavailable")
        for value in (min_available, max_unavailable):
            if value is None:
                continue
            if isinstance(value, str):
                if not re.match(r"^\d{1,3}%$", value):
                    bounds(f"{value!r} must be a whole number or a percentage like 50%")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > REPLICAS_MAX:
                bounds(f"{value!r} must be between 0 and {REPLICAS_MAX} or a percentage")
        spec_patch = {"minAvailable": min_available, "maxUnavailable": max_unavailable}
        spec = obj["spec"]
        inverse = {"spec": {"minAvailable": spec.get("minAvailable"), "maxUnavailable": spec.get("maxUnavailable")}}
        return Intent(
            tool="patch_pdb", tier="propose", target=f"pdb/{pdb}", resource=RESOURCES["pdb"], name=pdb,
            summary=f"change PDB {pdb}: {json.dumps({k: v for k, v in spec_patch.items() if v is not None})}", before=obj,
            patch_type="merge", patch={"spec": spec_patch}, allowed=("spec.minAvailable", "spec.maxUnavailable"), inverse=("merge", inverse),
            details={"from": inverse["spec"], "to": spec_patch},
        )

    def patch_affinity(self, deployment, node_selector=None, tolerations=None, clear_affinity=False):
        obj = self._get("deploy", deployment)
        pod_spec = obj["spec"]["template"]["spec"]
        ops = []
        if node_selector is not None:
            if not isinstance(node_selector, dict) or not all(isinstance(v, str) for v in node_selector.values()):
                bounds("node_selector must be an object of label: value; {} clears it")
            if node_selector:
                ops.append({"op": "add", "path": "/spec/template/spec/nodeSelector", "value": node_selector})
            elif "nodeSelector" in pod_spec:
                ops.append({"op": "remove", "path": "/spec/template/spec/nodeSelector"})
        if tolerations is not None:
            if not isinstance(tolerations, list) or not all(isinstance(t, dict) and set(t) <= TOLERATION_KEYS for t in tolerations):
                bounds(f"tolerations must be a list of objects with keys from {sorted(TOLERATION_KEYS)}; [] clears them")
            if tolerations:
                ops.append({"op": "add", "path": "/spec/template/spec/tolerations", "value": tolerations})
            elif "tolerations" in pod_spec:
                ops.append({"op": "remove", "path": "/spec/template/spec/tolerations"})
        if clear_affinity and "affinity" in pod_spec:
            ops.append({"op": "remove", "path": "/spec/template/spec/affinity"})
        if not ops:
            bounds("nothing to change: give node_selector, tolerations or clear_affinity")
        return self._deployment_intent(
            "patch_affinity", "propose", obj, f"change scheduling of {deployment}: {json.dumps(ops)}", "json", ops,
            ["spec.template.spec.nodeSelector", "spec.template.spec.tolerations", "spec.template.spec.affinity"],
            details={"from": {k: pod_spec.get(k) for k in ("nodeSelector", "tolerations", "affinity")}, "ops": ops},
        )

    def patch_resources(self, deployment, container=None, requests=None, limits=None):
        obj = self._get("deploy", deployment)
        target = self._container(obj, container)
        resources = {}
        for section, values in (("requests", requests), ("limits", limits)):
            if values is None:
                continue
            if not isinstance(values, dict) or not values:
                bounds(f"{section} must be a non-empty object like {{\"memory\": \"256Mi\"}}")
            cleaned = {}
            for key, value in values.items():
                if key not in RESOURCE_CAPS:
                    bounds(f"unknown resource {key!r}; use {sorted(RESOURCE_CAPS)}")
                if quantity(value, f"{section}.{key}") > RESOURCE_CAPS[key]:
                    bounds(f"{section}.{key} {value} is above the sandbox cap")
                cleaned[key] = str(value)
            resources[section] = cleaned
        if not resources:
            bounds("give requests or limits")
        current = target.get("resources", {})
        for key in set(resources.get("requests", {})) & set(resources.get("limits", {})):
            if parse_quantity(resources["requests"][key]) > parse_quantity(resources["limits"][key]):
                bounds(f"request {key} {resources['requests'][key]} is above its limit {resources['limits'][key]}")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": target["name"], "resources": resources}]}}}}
        return self._deployment_intent(
            "patch_resources", "propose", obj, f"change resources of {deployment}/{target['name']}: {json.dumps(resources)}", "strategic", patch,
            [f"spec.template.spec.containers[{target['name']}].resources"], details={"from": current, "to": resources},
        )

    # ---- dispatch ----

    def build(self, name, arguments):
        if name not in AUTO | PROPOSE:
            raise Refusal("allow-list", f"{name!r} is not a tool the agent has")
        handler = getattr(self, name)
        try:
            return handler(**(arguments or {}))
        except TypeError as exc:
            raise Refusal("bounds", f"{name}: bad arguments: {exc}")

    def deployments_ready(self):
        for item in self._list("deploy"):
            spec, status = item["spec"], item.get("status", {})
            wanted = spec.get("replicas", 1)
            if status.get("observedGeneration", 0) < item["metadata"].get("generation", 0):
                return False
            if status.get("readyReplicas", 0) != wanted or status.get("updatedReplicas", 0) != wanted or status.get("replicas", 0) != wanted:
                return False
        return True

    def verify(self, timeout_s):
        start = time.monotonic()
        streak = 0
        while True:
            try:
                ready = self.deployments_ready()
            except Refusal:
                ready = False
            streak = streak + 1 if ready else 0
            waited = round(time.monotonic() - start)
            if streak >= 3:
                return {"improved": True, "waited_s": waited, "health": {"source": "agent view: every storefront Deployment fully ready for 3 polls"}}
            if waited >= timeout_s:
                return {"improved": False, "waited_s": waited, "health": {"source": "agent view: a storefront Deployment is still not fully ready"}}
            time.sleep(5)


def _p(**props):
    return {"type": "object", "properties": props}


TOOL_SPECS = [
    {"type": "function", "function": {"name": "rollout_restart", "description": "Act alone. Restart the pods of a storefront Deployment (new pods, same spec).",
        "parameters": {**_p(deployment={"type": "string"}), "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "rollback", "description": "Act alone. Roll a storefront Deployment back to the revision before the one that was live when the alert fired (revision_at_alert in the alert), or to an older to_revision. Never forward.",
        "parameters": {**_p(deployment={"type": "string"}, to_revision={"type": "integer"}), "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "scale", "description": "Act alone. Set the replica count of a storefront Deployment, between 1 and 6.",
        "parameters": {**_p(deployment={"type": "string"}, replicas={"type": "integer"}), "required": ["deployment", "replicas"]}}},
    {"type": "function", "function": {"name": "raise_limits", "description": "Act alone. Raise the memory and/or cpu limit of a container: at most 2x the current limit, never above 1Gi memory or 1 cpu, never lower.",
        "parameters": {**_p(deployment={"type": "string"}, memory={"type": "string"}, cpu={"type": "string"}, container={"type": "string"}), "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "delete_pod", "description": "Act alone. Delete one Deployment-owned pod in storefront so its controller replaces it.",
        "parameters": {**_p(pod={"type": "string"}), "required": ["pod"]}}},
    {"type": "function", "function": {"name": "patch_probe", "description": "Propose. Change a container's liveness, readiness or startup probe: path, port, initial_delay_s, period_s, timeout_s, failure_threshold. Applied after approval.",
        "parameters": {**_p(deployment={"type": "string"}, probe={"type": "string", "enum": list(PROBES)}, container={"type": "string"}, path={"type": "string"}, port={"type": "integer"}, initial_delay_s={"type": "integer"}, period_s={"type": "integer"}, timeout_s={"type": "integer"}, failure_threshold={"type": "integer"}), "required": ["deployment", "probe"]}}},
    {"type": "function", "function": {"name": "patch_env", "description": "Propose. Set or unset plain environment variables on a container of a storefront Deployment. Applied after approval.",
        "parameters": {**_p(deployment={"type": "string"}, set={"type": "object", "additionalProperties": {"type": "string"}}, unset={"type": "array", "items": {"type": "string"}}, container={"type": "string"}), "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "patch_configmap", "description": "Propose. Set or remove keys in a storefront ConfigMap. Applied after approval.",
        "parameters": {**_p(configmap={"type": "string"}, set={"type": "object", "additionalProperties": {"type": "string"}}, remove={"type": "array", "items": {"type": "string"}}), "required": ["configmap"]}}},
    {"type": "function", "function": {"name": "patch_service", "description": "Propose. Change a storefront Service's selector labels and/or one port's port and target_port. Applied after approval.",
        "parameters": {**_p(service={"type": "string"}, selector={"type": "object", "additionalProperties": {"type": "string"}}, port={"type": "integer"}, target_port={"type": "integer"}, port_name={"type": "string"}), "required": ["service"]}}},
    {"type": "function", "function": {"name": "patch_hpa", "description": "Propose. Change a storefront HorizontalPodAutoscaler: min_replicas, max_replicas (1 to 6), target_cpu_percent, target_deployment. Applied after approval.",
        "parameters": {**_p(hpa={"type": "string"}, min_replicas={"type": "integer"}, max_replicas={"type": "integer"}, target_cpu_percent={"type": "integer"}, target_deployment={"type": "string"}), "required": ["hpa"]}}},
    {"type": "function", "function": {"name": "patch_pdb", "description": "Propose. Change a storefront PodDisruptionBudget: exactly one of min_available or max_unavailable (number or percentage). Applied after approval.",
        "parameters": {**_p(pdb={"type": "string"}, min_available={"type": ["integer", "string"]}, max_unavailable={"type": ["integer", "string"]}), "required": ["pdb"]}}},
    {"type": "function", "function": {"name": "patch_affinity", "description": "Propose. Change where a storefront Deployment's pods may run: node_selector ({} clears it), tolerations ([] clears them), clear_affinity. Applied after approval.",
        "parameters": {**_p(deployment={"type": "string"}, node_selector={"type": "object", "additionalProperties": {"type": "string"}}, tolerations={"type": "array", "items": {"type": "object"}}, clear_affinity={"type": "boolean"}), "required": ["deployment"]}}},
    {"type": "function", "function": {"name": "patch_resources", "description": "Propose. Set a container's resource requests and/or limits (memory, cpu, ephemeral-storage), including changes outside the raise_limits bounds. Applied after approval.",
        "parameters": {**_p(deployment={"type": "string"}, container={"type": "string"}, requests={"type": "object", "additionalProperties": {"type": "string"}}, limits={"type": "object", "additionalProperties": {"type": "string"}}), "required": ["deployment"]}}},
]

CONTROL_SPECS = [
    {"type": "function", "function": {"name": "escalate", "description": "Stop and wake a human. Use when the cause is outside storefront, when you are not confident, or when your changes did not help.",
        "parameters": {**_p(reason={"type": "string"}), "required": ["reason"]}}},
    {"type": "function", "function": {"name": "finish", "description": "End the incident with a short note: what you saw, what you did and why.",
        "parameters": {**_p(summary={"type": "string"}), "required": ["summary"]}}},
]
