#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from kubernetes import client
from kubernetes.client.exceptions import ApiException

import sandbox
import score
import specs
from sandbox import SandboxError

ROOT = sandbox.ROOT
CHART = ROOT / "demo-app" / "chart"
RELEASE = "storefront"
APP_NAMESPACE = "storefront"
MON_RELEASE = "mon"
MON_NAMESPACE = "monitoring"
MON_CHART = ROOT / "cluster" / "charts" / "kube-prometheus-stack-92.3.0.tgz"
MON_VALUES = ROOT / "cluster" / "prometheus-values.yaml"
MONITORING_DIR = ROOT / "monitoring"
PROMETHEUS_STS = "prometheus-mon-kube-prometheus-stack-prometheus"
IMAGE = "localhost:5000/storefront:dev"
ANON_DOCKER_CONFIG = Path.home() / ".oncall" / "docker-anon"
BUILDX_PLUGIN = Path.home() / ".docker" / "cli-plugins" / "docker-buildx"
RUNS_DIR = ROOT / "reports" / "runs"
DEFAULT_RPS = 20
GREEN_TIMEOUT_S = 420
ALERT_TIMEOUT_S = 600
ROLLOUT_TIMEOUT = "--timeout=300s"
KIND_CLUSTER_LABEL = "io.x-k8s.kind.cluster"
APP_ROLLOUTS = (
    ("storefront", "statefulset/postgres"),
    ("storefront", "deployment/redis"),
    ("storefront", "deployment/api"),
    ("storefront", "deployment/worker"),
    ("external", "deployment/payments-gw"),
)
MON_ROLLOUTS = (
    "deployment/mon-kube-prometheus-stack-operator",
    "deployment/mon-kube-state-metrics",
    "daemonset/mon-prometheus-node-exporter",
)
HARNESS_ROLLOUTS = ("daemonset/registry", "deployment/slo-probe", "deployment/loadgen")


class HarnessError(Exception):
    pass


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sandbox_env(name):
    env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG" and not k.startswith("HELM_KUBE")}
    env["KUBECONFIG"] = str(sandbox.kubeconfig_path(name))
    return env


def guard(name):
    failed = [r for r in sandbox.run_checks(name, only={3, 4}) if not r[2]]
    if failed:
        raise HarnessError("sandbox checks failed before a write: " + "; ".join(f"{n}. {label}: {detail}" for n, label, _, detail in failed))


def helm(name, *args):
    guard(name)
    return sandbox.sh(
        ["helm", "--kubeconfig", str(sandbox.kubeconfig_path(name)), "--kube-context", sandbox.context_name(name), *args],
        capture=True,
    )


def kubectl(name, *args):
    guard(name)
    return sandbox.kubectl(name, *args)


def run_spec_command(name, command):
    argv = specs.check_command(command.replace("{sandbox}", name))
    guard(name)
    if argv[0] == "docker":
        owner = subprocess.run(
            ["docker", "inspect", "-f", "{{ index .Config.Labels \"" + KIND_CLUSTER_LABEL + "\" }}", argv[2]],
            text=True,
            capture_output=True,
        )
        if owner.returncode != 0 or owner.stdout.strip() != name:
            raise HarnessError(f"refusing {command!r}: container {argv[2]!r} is not a node of sandbox {name!r}")
    print("$ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=ROOT, env=sandbox_env(name), text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.returncode != 0:
        raise HarnessError(f"command failed ({result.returncode}): {command}\n{result.stderr.strip()}")


def reset_app(name, reset="helm", cleanup=()):
    for command in cleanup:
        run_spec_command(name, command)
    if reset == "reinstall":
        helm(name, "uninstall", RELEASE, "-n", APP_NAMESPACE, "--ignore-not-found")
        kubectl(name, "-n", APP_NAMESPACE, "delete", "pvc", "data-postgres-0", "--ignore-not-found", "--timeout=180s")
    helm(name, "upgrade", "--install", RELEASE, str(CHART), "-n", APP_NAMESPACE, "--reset-values", "--force-conflicts")
    kubectl(name, "-n", APP_NAMESPACE, "delete", "pods", "--field-selector=status.phase=Failed", "--ignore-not-found")
    for namespace, obj in APP_ROLLOUTS:
        kubectl(name, "-n", namespace, "rollout", "status", obj, ROLLOUT_TIMEOUT)
    kubectl(name, "-n", APP_NAMESPACE, "exec", "deploy/redis", "--", "redis-cli", "DEL", "jobs")


def set_loadgen(name, api, rps):
    deployment = client.AppsV1Api(api).read_namespaced_deployment("loadgen", "harness")
    env = {e.name: e.value for e in deployment.spec.template.spec.containers[0].env or []}
    want = f"{rps:g}"
    if env.get("RPS") == want:
        return
    kubectl(name, "-n", "harness", "set", "env", "deployment/loadgen", f"RPS={want}")
    kubectl(name, "-n", "harness", "rollout", "status", "deployment/loadgen", ROLLOUT_TIMEOUT)


def wait_for_statefulset(api, namespace, sts_name, timeout_s=300):
    apps = client.AppsV1Api(api)
    start = time.monotonic()
    while True:
        try:
            apps.read_namespaced_stateful_set(sts_name, namespace)
            return
        except ApiException as exc:
            if exc.status != 404 or time.monotonic() - start > timeout_s:
                raise
        time.sleep(5)


def anonymous_docker_env():
    endpoint = sandbox.sh(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], capture=True).strip()
    plugins = ANON_DOCKER_CONFIG / "cli-plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    config_file = ANON_DOCKER_CONFIG / "config.json"
    if not config_file.exists():
        config_file.write_text("{}\n")
    link = plugins / BUILDX_PLUGIN.name
    if BUILDX_PLUGIN.exists() and not link.exists():
        link.symlink_to(BUILDX_PLUGIN)
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT")}
    env.update(DOCKER_CONFIG=str(ANON_DOCKER_CONFIG), DOCKER_HOST=endpoint)
    return env


def prune_node_images(name):
    for node in sandbox.sh(["kind", "get", "nodes", "--name", name], capture=True).split():
        listing = subprocess.run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "ls"], text=True, capture_output=True)
        if listing.returncode != 0:
            continue
        refs_by_digest = {}
        for line in listing.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3:
                refs_by_digest.setdefault(fields[2], []).append(fields[0])
        stale = [ref for refs in refs_by_digest.values() if all(r.startswith("import-") for r in refs) for ref in refs]
        if stale:
            subprocess.run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "rm", *stale], capture_output=True)
        print(f"  {node}: removed {len(stale)} unused image copies", flush=True)


def build(name):
    argv = ["docker", "build", "-t", IMAGE, str(ROOT / "demo-app")]
    print(f"$ DOCKER_CONFIG={ANON_DOCKER_CONFIG} " + " ".join(argv), flush=True)
    if subprocess.run(argv, env=anonymous_docker_env()).returncode != 0:
        raise HarnessError("image build failed")
    guard(name)
    sandbox.sh(["kind", "load", "docker-image", IMAGE, "--name", name])
    prune_node_images(name)


def bootstrap(name):
    api = sandbox.admin_client(name)
    helm(name, "upgrade", "--install", MON_RELEASE, str(MON_CHART), "-n", MON_NAMESPACE, "--create-namespace", "-f", str(MON_VALUES))
    for obj in MON_ROLLOUTS:
        kubectl(name, "-n", MON_NAMESPACE, "rollout", "status", obj, ROLLOUT_TIMEOUT)
    wait_for_statefulset(api, MON_NAMESPACE, PROMETHEUS_STS)
    kubectl(name, "-n", MON_NAMESPACE, "rollout", "status", f"statefulset/{PROMETHEUS_STS}", ROLLOUT_TIMEOUT)
    guard(name)
    sandbox.sh(["kind", "load", "docker-image", IMAGE, "--name", name])
    reset_app(name)
    kubectl(name, "apply", "-f", str(MONITORING_DIR))
    for obj in HARNESS_ROLLOUTS:
        kubectl(name, "-n", "harness", "rollout", "status", obj, ROLLOUT_TIMEOUT)


def recreate(name):
    if name in sandbox.kind_clusters():
        sandbox.delete(name)
    if not sandbox.create(name):
        raise HarnessError(f"sandbox {name!r} failed its checks after create")
    bootstrap(name)


def wait_green(name):
    api = sandbox.admin_client(name)
    set_loadgen(name, api, DEFAULT_RPS)
    return score.wait_for(score.Prometheus(api), True, GREEN_TIMEOUT_S)


def reset_and_wait(name, spec):
    if spec.needs_fresh_cluster:
        recreate(name)
    else:
        reset_app(name, spec.reset, spec.cleanup)
    return wait_green(name)


def watch(prom, spec, timeout_s):
    alert = specs.resolve_alert(spec.alert)
    want_red = spec.expected != "noop"
    start = time.monotonic()
    alert_after = 0 if spec.synthetic else None
    red_after = red_reason = None
    while True:
        elapsed = round(time.monotonic() - start)
        try:
            if alert_after is None and alert in prom.firing():
                alert_after = elapsed
                print(f"  alert {alert} firing after {elapsed}s", flush=True)
            if want_red and red_after is None:
                current = score.health(prom)
                if not current.healthy:
                    red_after, red_reason = elapsed, current.reason
                    print(f"  health red after {elapsed}s: {red_reason}", flush=True)
        except Exception as exc:
            print(f"  poll failed: {exc}", file=sys.stderr, flush=True)
        if alert_after is not None and (red_after is not None or not want_red):
            break
        if elapsed >= timeout_s:
            break
        time.sleep(score.POLL_S)
    result = {"alert": "synthetic" if spec.synthetic else alert, "alert_fired": alert_after is not None, "alert_after_s": alert_after}
    if want_red:
        result.update(red_after_inject=red_after is not None, red_after_s=red_after, red_reason=red_reason)
    else:
        current = score.health(prom)
        result.update(red_after_inject=not current.healthy, red_after_s=None, red_reason=current.reason)
    return result


def append_row(row):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{dt.date.today().isoformat()}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return path


def passed(row):
    red_ok = row.get("red_after_inject") or row.get("expected") == "noop"
    return bool(row.get("healthy_before") and row.get("alert_fired") and red_ok and row.get("healthy_after_reset") and not row.get("error"))


def run_fault(name, spec, mode):
    print(f"\n=== {spec.id} {spec.title} (expected {spec.expected}) ===", flush=True)
    row = {"ts": now_iso(), "sandbox": name, "fault": spec.id, "mode": mode, "expected": spec.expected, "healthy_before": False}
    errors = []
    start = time.monotonic()
    try:
        api = sandbox.admin_client(name)
        prom = score.Prometheus(api)
        reset_app(name)
        set_loadgen(name, api, spec.loadgen_rps)
        before, _ = score.wait_for(prom, True, GREEN_TIMEOUT_S)
        row["healthy_before"] = before.healthy
        if not before.healthy:
            raise HarnessError(f"not healthy before inject: {before.reason}")
        row["inject_at"] = now_iso()
        for command in spec.inject:
            run_spec_command(name, command)
        row.update(watch(prom, spec, ALERT_TIMEOUT_S))
    except Exception as exc:
        errors.append(f"{exc.__class__.__name__}: {exc}")
        print(f"error: {exc}", file=sys.stderr, flush=True)
    finally:
        row["reset"] = "fresh-cluster" if spec.needs_fresh_cluster else spec.reset
        try:
            try:
                after, waited = reset_and_wait(name, spec)
                problem = None if after.healthy else after.reason
            except Exception as exc:
                after, waited, problem = None, None, f"{exc.__class__.__name__}: {exc}"
            if problem and not spec.needs_fresh_cluster:
                print(f"  reset did not recover the sandbox ({problem}); rebuilding it", file=sys.stderr, flush=True)
                errors.append(f"reset did not recover the sandbox, rebuilt it: {problem}")
                row["reset"] = f"{spec.reset} then fresh-cluster"
                recreate(name)
                after, waited = wait_green(name)
            row["healthy_after_reset"] = bool(after and after.healthy)
            row["reset_to_green_s"] = waited
            if after is None or not after.healthy:
                errors.append(f"not healthy after reset: {after.reason if after else problem}")
        except Exception as exc:
            row["healthy_after_reset"] = False
            errors.append(f"reset failed: {exc.__class__.__name__}: {exc}")
            print(f"reset failed: {exc}", file=sys.stderr, flush=True)
        row["duration_s"] = round(time.monotonic() - start)
        if errors:
            row["error"] = " | ".join(errors)
        path = append_row(row)
        print(f"  {'PASS' if passed(row) else 'FAIL'} {spec.id}: row written to {path.relative_to(ROOT)}", flush=True)
    return row


def print_summary(rows):
    print("\nfault  expected  alert_s  red_s  reset_ok  result  error")
    for row in rows:
        print(
            f"{row['fault']:<6} {row['expected']:<9} {str(row.get('alert_after_s')):<8} {str(row.get('red_after_s')):<6} "
            f"{str(row.get('healthy_after_reset')):<9} {'PASS' if passed(row) else 'FAIL':<7} {row.get('error', '')[:80]}"
        )
    print(f"\n{sum(passed(r) for r in rows)}/{len(rows)} passed")


def self_test(name, skip, partial, seed):
    library = specs.load_library(complete=not partial)
    chosen = [s for s in library.values() if s.id not in skip]
    random.Random(seed).shuffle(chosen)
    print(f"self-test: {len(chosen)} faults, order {' '.join(s.id for s in chosen)}", flush=True)
    rows = [run_fault(name, spec, "self-test") for spec in chosen]
    print_summary(rows)
    return all(passed(r) for r in rows)


def soak(name, seconds):
    api = sandbox.admin_client(name)
    prom = score.Prometheus(api)
    set_loadgen(name, api, DEFAULT_RPS)
    row = {"ts": now_iso(), "sandbox": name, "mode": "soak", "soak_s": seconds}
    before, _ = score.wait_for(prom, True, GREEN_TIMEOUT_S)
    row["healthy_before"] = before.healthy
    samples = red = 0
    first_red = None
    alerts_seen = set()
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        current = score.health(prom)
        samples += 1
        if not current.healthy:
            red += 1
            first_red = first_red or current.reason
        alerts_seen |= {a["labels"].get("alertname") for a in prom.alerts()}
        time.sleep(score.POLL_S)
    row.update(samples=samples, red_samples=red, first_red_reason=first_red, alerts_seen=sorted(alerts_seen))
    row["passed"] = bool(before.healthy and red == 0 and not alerts_seen)
    path = append_row(row)
    print(f"soak {seconds}s: {samples} samples, {red} red, alerts {sorted(alerts_seen) or 'none'} -> {'PASS' if row['passed'] else 'FAIL'} ({path.relative_to(ROOT)})")
    return row["passed"]


def validate(partial):
    library = specs.load_library(complete=not partial)
    by_expected = {e: sum(s.expected == e for s in library.values()) for e in specs.EXPECTED}
    sealed = sorted(s.id for s in library.values() if s.sealed)
    print(f"{len(library)} specs valid; expected {by_expected}; sealed {len(sealed)} {sealed}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="reset, inject, confirm and reset faults on an AI on-call sandbox")
    parser.add_argument("--sandbox", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--fault", help="run one fault, e.g. F11")
    action.add_argument("--self-test", action="store_true", help="run every fault once with no agent")
    action.add_argument("--soak", type=int, metavar="SECONDS", help="watch a clean cluster for alerts and red health")
    action.add_argument("--bootstrap", action="store_true", help="install monitoring, the app and the harness workloads")
    action.add_argument("--build", action="store_true", help="build the app image with no registry login and load it into the sandbox")
    action.add_argument("--validate", action="store_true", help="load and validate the fault library")
    parser.add_argument("--no-agent", action="store_true", help="inject and confirm only; required until the agent exists")
    parser.add_argument("--skip", default="", help="comma-separated fault ids to leave out of --self-test")
    parser.add_argument("--partial", action="store_true", help="allow a library with fewer than 50 specs")
    parser.add_argument("--seed", type=int, default=None, help="shuffle seed for --self-test")
    args = parser.parse_args(argv)
    try:
        sandbox.validate_name(args.sandbox)
        if args.validate:
            ok = validate(args.partial)
        elif args.bootstrap:
            bootstrap(args.sandbox)
            ok = True
        elif args.build:
            build(args.sandbox)
            ok = True
        elif args.soak:
            ok = soak(args.sandbox, args.soak)
        elif args.self_test:
            skip = {s.strip() for s in args.skip.split(",") if s.strip()}
            ok = self_test(args.sandbox, skip, args.partial, args.seed)
        else:
            if not args.no_agent:
                raise HarnessError("the agent arrives in week 2; pass --no-agent")
            library = specs.load_library()
            if args.fault not in library:
                raise HarnessError(f"no fault {args.fault!r}; known: {' '.join(sorted(library))}")
            ok = passed(run_fault(args.sandbox, library[args.fault], "no-agent"))
    except (HarnessError, SandboxError, specs.SpecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
