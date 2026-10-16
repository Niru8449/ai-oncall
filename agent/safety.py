import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from kubernetes import client, config

SANDBOXES = Path.home() / ".oncall" / "sandboxes"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MARKER_NAMESPACE = "storefront"
MARKER_NAME = "oncall-sandbox"
AUTO = {"rollout_restart", "rollback", "scale", "raise_limits", "delete_pod"}
PROPOSE = {"patch_probe", "patch_env", "patch_configmap", "patch_service", "patch_hpa", "patch_pdb", "patch_affinity", "patch_resources"}
MODES = ("diagnose", "act")
VERIFY_S = 120
MAX_WRITES = 3
DEFAULT_FLOOR = 0.7
IGNORED_PATHS = (
    "status",
    "metadata.resourceVersion",
    "metadata.generation",
    "metadata.managedFields",
    "metadata.uid",
    "metadata.creationTimestamp",
    "metadata.annotations.deployment.kubernetes.io/revision",
)


class Refused(Exception):
    pass


def refuse(reason):
    raise Refused(f"Refusing to run: {reason}")


def connect_to_sandbox(name):
    home = SANDBOXES / name
    kubeconfig = home / "agent.kubeconfig"
    if not kubeconfig.is_file():
        refuse(f"no sandbox called {name!r}. Only harness/sandbox.py creates sandboxes.")
    context = f"kind-{name}-agent"
    try:
        api = config.new_client_from_config(config_file=str(kubeconfig), context=context)
    except config.ConfigException:
        refuse(f"{kubeconfig} has no context {context!r}.")
    host = urlparse(api.configuration.host).hostname
    if host not in LOCAL_HOSTS:
        refuse(f"the API server {host!r} is not on this Mac.")
    assert_same_cluster(api, home)
    return api


def assert_same_cluster(api, home):
    expected = (home / "id").read_text().strip()
    try:
        marker = client.CoreV1Api(api).read_namespaced_config_map(MARKER_NAME, MARKER_NAMESPACE)
    except client.ApiException:
        refuse("this cluster has no oncall-sandbox marker.")
    if (marker.data or {}).get("id") != expected:
        refuse("this cluster's ID doesn't match the sandbox's.")


def mode_from_env():
    mode = os.environ.get("ONCALL_MODE", "diagnose")
    if mode not in MODES:
        refuse(f"ONCALL_MODE must be one of {MODES}, got {mode!r}")
    return mode


def floor_from_env():
    try:
        value = float(os.environ.get("ONCALL_CONFIDENCE_FLOOR", DEFAULT_FLOOR))
    except ValueError:
        refuse("ONCALL_CONFIDENCE_FLOOR must be a number")
    if not 0 <= value <= 1:
        refuse("ONCALL_CONFIDENCE_FLOOR must be between 0 and 1")
    return value


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_key(items):
    if items and all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        return "name"
    if items and all(isinstance(i, dict) and "port" in i for i in items):
        return "port"
    return None


def changed_paths(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(set(before) | set(after), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                present = after[key] if key in after else before[key]
                if isinstance(present, dict):
                    paths.extend(changed_paths(before.get(key, {}), after.get(key, {}), path))
                elif isinstance(present, list):
                    paths.extend(changed_paths(before.get(key, []), after.get(key, []), path))
                else:
                    paths.append(path)
            else:
                paths.extend(changed_paths(before[key], after[key], path))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        key = _list_key(before) or _list_key(after)
        if key and _list_key(before) == _list_key(after):
            paths = []
            left = {i[key]: i for i in before}
            right = {i[key]: i for i in after}
            for name in sorted(set(left) | set(right), key=str):
                path = f"{prefix}[{name}]"
                if name not in left or name not in right:
                    paths.append(path)
                else:
                    paths.extend(changed_paths(left[name], right[name], path))
            return paths
        if len(before) != len(after):
            return [prefix]
        paths = []
        for index, (l, r) in enumerate(zip(before, after)):
            paths.extend(changed_paths(l, r, f"{prefix}[{index}]"))
        return paths
    return [] if before == after else [prefix]


def relevant_changes(before, after):
    return [p for p in changed_paths(before, after) if not any(p == i or p.startswith(i + ".") or p.startswith(i + "[") for i in IGNORED_PATHS)]


class Gate:
    def __init__(self, sandbox, api, mode, floor, verify, approver, max_writes=MAX_WRITES, deadline=None, log=print):
        if mode not in MODES:
            refuse(f"unknown mode {mode!r}")
        self.sandbox = sandbox
        self.home = SANDBOXES / sandbox
        self.kubeconfig = self.home / "agent.kubeconfig"
        self.context = f"kind-{sandbox}-agent"
        self.api = api
        self.mode = mode
        self.floor = floor
        self.verify = verify
        self.approver = approver
        self.max_writes = max_writes
        self.deadline = deadline
        self.log = log
        self.actions = []
        self.writes = 0
        self.escalation = None
        self.health_before_writes = None

    @property
    def budget_left(self):
        return self.writes < self.max_writes and (self.deadline is None or time.monotonic() < self.deadline)

    def _record(self, **fields):
        record = {"ts": now_iso(), **fields}
        self.actions.append(record)
        self.log(f"  write: {record['tool']} {record.get('target') or ''} -> {record['status']}" + (f" ({record['reason']})" if record.get("reason") else ""))
        return record

    def refuse(self, tool, arguments, code, message):
        return self._record(tool=tool, target=None, tier=None, args=arguments, status="refused", reason=code, detail=message, attempt=code == "allow-list")

    def escalate(self, reason):
        self.escalation = reason
        self.log(f"  escalate: {reason}")
        return reason

    def write(self, intent, confidence):
        base = {"tool": intent.tool, "target": intent.target, "tier": intent.tier, "summary": intent.summary, "patch_type": intent.patch_type, "patch": intent.patch, "details": intent.details}
        if intent.tool not in AUTO | PROPOSE:
            return self._record(**base, status="refused", reason="allow-list", detail=f"{intent.tool} is not an allowed tool", attempt=True)
        if intent.namespace != MARKER_NAMESPACE:
            return self._record(**base, status="refused", reason="allow-list", detail=f"{intent.target} is outside {MARKER_NAMESPACE}", attempt=True)
        if self.mode == "diagnose":
            return self._record(**base, status="proposed", reason="ONCALL_MODE=diagnose", applied=False)
        try:
            assert_same_cluster(self.api, self.home)
        except Refused as exc:
            return self._record(**base, status="refused", reason="sandbox", detail=str(exc))
        if self.writes >= self.max_writes:
            return self._record(**base, status="refused", reason="budget", detail=f"{self.max_writes} writes already made")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return self._record(**base, status="refused", reason="budget", detail="time budget used up")
        if confidence is not None and confidence < self.floor:
            return self._record(**base, status="refused", reason="confidence", detail=f"confidence {confidence} is below the floor {self.floor}")
        approved_by = None
        if intent.tier == "propose":
            approved_by = self.approver(intent) if self.approver else None
            if not approved_by:
                return self._record(**base, status="proposed", reason="not approved", applied=False)
            base["approved_by"] = approved_by
        ok, changed, disallowed, error = self._dry_run(intent)
        if not ok:
            return self._record(**base, status="refused", reason="dry-run", detail=error, changed=changed, disallowed=disallowed)
        if self.writes == 0 and self.verify:
            self.health_before_writes = (self.verify(0) or {}).get("health")
        self.writes += 1
        applied_at = now_iso()
        rc, out, err = self._apply(intent)
        if rc != 0:
            return self._record(**base, status="failed", reason="apply", detail=(err or out).strip()[:400], applied_at=applied_at, changed=changed)
        result = self.verify(VERIFY_S) if self.verify else {"improved": True, "waited_s": 0, "health": None}
        if result.get("improved"):
            return self._record(**base, status="kept", applied=True, applied_at=applied_at, changed=changed, verify=result)
        undo = self._undo(intent)
        return self._record(**base, status="undone", applied=True, applied_at=applied_at, changed=changed, verify=result, undo=undo, reason="health did not improve")

    def _kubectl(self, *args):
        argv = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.context, "-n", MARKER_NAMESPACE, *args]
        result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
        return result.returncode, result.stdout, result.stderr

    def _command(self, intent, patch_type=None, patch=None, dry_run=False):
        args = ["patch", intent.resource, intent.name, f"--type={patch_type}", "-p", json.dumps(patch), "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return args

    def _delete(self, intent, dry_run=False):
        try:
            client.CoreV1Api(self.api).delete_namespaced_pod(intent.name, MARKER_NAMESPACE, dry_run="All" if dry_run else None, _request_timeout=30)
        except client.ApiException as exc:
            return 1, "", f"{exc.status} {exc.reason}: {(exc.body or '')[:300]}"
        return 0, "", ""

    def _dry_run(self, intent):
        if intent.delete:
            rc, out, err = self._delete(intent, dry_run=True)
            return (True, ["(delete)"], [], None) if rc == 0 else (False, [], [], err)
        rc, out, err = self._kubectl(*self._command(intent, intent.patch_type, intent.patch, dry_run=True))
        if rc != 0:
            return False, [], [], (err or out).strip()[:400]
        try:
            after = json.loads(out)
        except json.JSONDecodeError:
            return False, [], [], "dry run returned no object"
        changed = relevant_changes(intent.before, after)
        disallowed = [p for p in changed if not any(p == a or p.startswith(a + ".") or p.startswith(a + "[") for a in intent.allowed)]
        if disallowed:
            return False, changed, disallowed, f"the patch would also change {', '.join(disallowed[:6])}"
        return True, changed, [], None

    def _apply(self, intent):
        if intent.delete:
            return self._delete(intent)
        return self._kubectl(*self._command(intent, intent.patch_type, intent.patch))

    def _undo(self, intent):
        if intent.inverse is None:
            return {"status": "none", "detail": f"{intent.tool} has nothing to undo"}
        patch_type, patch = intent.inverse
        rc, out, err = self._kubectl(*self._command(intent, patch_type, patch))
        if rc != 0:
            return {"status": "failed", "detail": (err or out).strip()[:400]}
        self.log(f"  undo: {intent.summary} reverted")
        return {"status": "reverted", "patch_type": patch_type, "patch": patch}


def main(argv=None):
    name = (argv or sys.argv[1:] or ["oncall"])[0]
    try:
        api = connect_to_sandbox(name)
    except Refused as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"connected to sandbox {name!r} as the agent: {api.configuration.host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
