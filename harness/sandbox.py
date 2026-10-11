#!/usr/bin/env python3
import argparse
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

ROOT = Path(__file__).resolve().parent.parent
SANDBOXES = Path.home() / ".oncall" / "sandboxes"
KIND_TEMPLATE = ROOT / "cluster" / "kind-config.yaml"
CALICO_MANIFEST = ROOT / "cluster" / "calico.yaml"
AUDIT_DIR = ROOT / "reports" / "audit"
NAMESPACES = ("storefront", "harness", "external")
MARKER_NAMESPACE = "storefront"
MARKER_NAME = "oncall-sandbox"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
ROLLOUT_TIMEOUT = "1200s"


class SandboxError(Exception):
    pass


class BlockDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockDumper.add_representer(str, _represent_str)


def sandbox_dir(name):
    return SANDBOXES / name


def kubeconfig_path(name):
    return sandbox_dir(name) / "admin.kubeconfig"


def id_path(name):
    return sandbox_dir(name) / "id"


def context_name(name):
    return f"kind-{name}"


def validate_name(name):
    if not NAME_RE.match(name):
        raise SandboxError(f"invalid sandbox name {name!r}: use lowercase letters, digits, '.' and '-'")


def sh(args, capture=False):
    print("$ " + " ".join(args), flush=True)
    result = subprocess.run(args, text=True, capture_output=capture)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise SandboxError(f"command failed ({result.returncode}): {' '.join(args)}" + (f"\n{detail}" if detail else ""))
    return result.stdout if capture else ""


def kubectl(name, *args):
    return sh(["kubectl", "--kubeconfig", str(kubeconfig_path(name)), "--context", context_name(name), *args])


def dump_diagnostics(name):
    if not kubeconfig_path(name).is_file():
        return
    base = ["kubectl", "--kubeconfig", str(kubeconfig_path(name)), "--context", context_name(name), "-n", "kube-system"]
    for args in (["get", "pods", "-o", "wide"], ["get", "events", "--sort-by=.lastTimestamp"]):
        result = subprocess.run(base + args, text=True, capture_output=True)
        lines = (result.stdout or result.stderr).strip().splitlines()
        print("\n$ " + " ".join(base + args), file=sys.stderr)
        print("\n".join(lines[-30:]), file=sys.stderr, flush=True)


def kind_clusters():
    result = subprocess.run(["kind", "get", "clusters"], text=True, capture_output=True)
    if result.returncode != 0:
        raise SandboxError(f"kind get clusters failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def render_kind_config(name):
    cfg = yaml.safe_load(KIND_TEMPLATE.read_text())
    for node in cfg.get("nodes", []):
        for mount in node.get("extraMounts") or []:
            host = Path(mount["hostPath"])
            if not host.is_absolute():
                host = (ROOT / host).resolve()
            if not host.exists():
                raise SandboxError(f"extraMounts hostPath does not exist: {host}")
            mount["hostPath"] = str(host)
    rendered = sandbox_dir(name) / "kind-config.yaml"
    rendered.write_text(yaml.dump(cfg, Dumper=BlockDumper, sort_keys=False))
    return rendered


def new_api_client(name):
    return config.new_client_from_config(
        config_file=str(kubeconfig_path(name)),
        context=context_name(name),
        persist_config=False,
    )


def load_kubeconfig(name):
    return yaml.safe_load(kubeconfig_path(name).read_text()) or {}


def check_kubeconfig_exists(name):
    path = kubeconfig_path(name)
    return path.is_file(), str(path)


def check_context(name):
    kc = load_kubeconfig(name)
    expected = context_name(name)
    contexts = [c.get("name") for c in kc.get("contexts", [])]
    current = kc.get("current-context")
    return current == expected and contexts == [expected], f"current-context={current!r} contexts={contexts}"


def check_server_host(name):
    kc = load_kubeconfig(name)
    expected = context_name(name)
    ctx = next((c for c in kc.get("contexts", []) if c.get("name") == expected), {})
    cluster_ref = ctx.get("context", {}).get("cluster")
    cluster = next((c for c in kc.get("clusters", []) if c.get("name") == cluster_ref), {})
    server = cluster.get("cluster", {}).get("server", "")
    return urlparse(server).hostname == "127.0.0.1", f"server={server!r}"


def check_marker(name):
    file_id = id_path(name).read_text().strip() if id_path(name).is_file() else None
    core = client.CoreV1Api(new_api_client(name))
    try:
        cm = core.read_namespaced_config_map(MARKER_NAME, MARKER_NAMESPACE, _request_timeout=10)
    except ApiException as exc:
        return False, f"ConfigMap {MARKER_NAMESPACE}/{MARKER_NAME} unreadable: HTTP {exc.status}"
    except Exception as exc:
        return False, f"cluster unreachable: {exc.__class__.__name__}"
    cluster_id = (cm.data or {}).get("id")
    ok = bool(file_id) and bool(ID_RE.match(file_id)) and cluster_id == file_id and cm.immutable is True
    return ok, f"file id={file_id} cluster id={cluster_id} immutable={cm.immutable}"


CHECKS = (
    ("kubeconfig file exists", check_kubeconfig_exists),
    ("context is exactly kind-<name>", check_context),
    ("server host is 127.0.0.1", check_server_host),
    ("cluster marker id equals the id file", check_marker),
)


def run_checks(name, only=None):
    results = []
    for number, (label, fn) in enumerate(CHECKS, start=1):
        if only and number not in only:
            continue
        try:
            ok, detail = fn(name)
        except Exception as exc:
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        results.append((number, label, ok, detail))
    return results


def print_checks(results):
    for number, label, ok, detail in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {number}. {label}: {detail}", flush=True)
    return all(ok for _, _, ok, _ in results)


def admin_client(name):
    validate_name(name)
    failed = [r for r in run_checks(name) if not r[2]]
    if failed:
        raise SandboxError("sandbox checks failed: " + "; ".join(f"{n}. {label}: {detail}" for n, label, _, detail in failed))
    return new_api_client(name)


def create(name):
    validate_name(name)
    if name in kind_clusters():
        raise SandboxError(f"kind already has a cluster named {name!r}; pick another name or run: python harness/sandbox.py delete {name}")
    folder = sandbox_dir(name)
    if folder.exists():
        raise SandboxError(f"{folder} exists but kind has no cluster {name!r}; remove that folder by hand if it is stale")
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    folder.mkdir(parents=True)
    rendered = render_kind_config(name)
    kubeconfig = kubeconfig_path(name)
    try:
        sh(["kind", "create", "cluster", "--name", name, "--config", str(rendered), "--kubeconfig", str(kubeconfig)])
        kubectl(name, "apply", "--server-side", "-f", str(CALICO_MANIFEST))
        kubectl(name, "-n", "kube-system", "rollout", "status", "daemonset/calico-node", f"--timeout={ROLLOUT_TIMEOUT}")
        kubectl(name, "-n", "kube-system", "rollout", "status", "deployment/calico-kube-controllers", f"--timeout={ROLLOUT_TIMEOUT}")
        kubectl(name, "wait", "--for=condition=Ready", "node", "--all", f"--timeout={ROLLOUT_TIMEOUT}")
        core = client.CoreV1Api(new_api_client(name))
        for ns in NAMESPACES:
            core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
            print(f"namespace/{ns} created")
        sandbox_id = secrets.token_hex(16)
        core.create_namespaced_config_map(
            MARKER_NAMESPACE,
            client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=MARKER_NAME, namespace=MARKER_NAMESPACE),
                immutable=True,
                data={"id": sandbox_id},
            ),
        )
        id_path(name).write_text(sandbox_id + "\n")
    except BaseException as exc:
        print(f"\ncreate failed: {exc}", file=sys.stderr, flush=True)
        try:
            dump_diagnostics(name)
        except Exception as diag_exc:
            print(f"could not collect diagnostics: {diag_exc}", file=sys.stderr)
        print(f"\nremoving cluster {name!r} and {folder} so no half-built sandbox is left behind", file=sys.stderr, flush=True)
        subprocess.run(["kind", "delete", "cluster", "--name", name, "--kubeconfig", str(kubeconfig)])
        shutil.rmtree(folder, ignore_errors=True)
        raise
    print(f"\nsandbox {name!r} ready")
    print(f"  id          {sandbox_id}")
    print(f"  context     {context_name(name)}")
    print(f"  kubeconfig  {kubeconfig}")
    print(f"  audit log   {AUDIT_DIR / 'kube-apiserver-audit.log'}")
    print(f"\nexport KUBECONFIG={kubeconfig}\n")
    return print_checks(run_checks(name))


def check(name):
    validate_name(name)
    return print_checks(run_checks(name))


def delete(name):
    validate_name(name)
    if not print_checks(run_checks(name, only={3, 4})):
        raise SandboxError(f"refusing to delete {name!r}: it is not a sandbox this script created, or it is unreachable")
    sh(["kind", "delete", "cluster", "--name", name, "--kubeconfig", str(kubeconfig_path(name))])
    shutil.rmtree(sandbox_dir(name))
    print(f"removed {sandbox_dir(name)}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="create, check and delete AI on-call sandbox clusters")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("create", "create a kind sandbox with Calico, namespaces and the oncall-sandbox marker"),
        ("check", "run the four safety checks against a sandbox"),
        ("delete", "delete a sandbox this script created"),
    ):
        sub.add_parser(command, help=help_text).add_argument("name")
    args = parser.parse_args(argv)
    try:
        ok = {"create": create, "check": check, "delete": delete}[args.command](args.name)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
