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

import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

import audit
import invariants
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
ALERT_CLEAR_TIMEOUT_S = 180
ROLLOUT_TIMEOUT = "--timeout=300s"
KIND_CLUSTER_LABEL = "io.x-k8s.kind.cluster"
AGENT_MODES = ("diagnose", "act")
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
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


def parse_iso(text):
    return dt.datetime.fromisoformat(text)


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
    if argv[:2] == ["helm", "upgrade"] and "--force-conflicts" not in argv:
        # An earlier agent write (kubectl scale, patch) leaves its own field manager on the object;
        # without this, helm refuses the inject with a server-side apply conflict. The reset already uses it.
        argv.append("--force-conflicts")
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
    kubectl(name, "-n", APP_NAMESPACE, "delete", "pods", "--field-selector=status.phase=Succeeded", "--ignore-not-found")
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


def wait_alert_clear(prom, alert, timeout_s):
    start = time.monotonic()
    while alert in prom.active():
        waited = round(time.monotonic() - start)
        if waited >= timeout_s:
            raise HarnessError(f"alert {alert} still pending or firing {waited}s after the sandbox turned green")
        time.sleep(score.POLL_S)
    waited = round(time.monotonic() - start)
    if waited:
        print(f"  alert {alert} from the previous fault cleared after {waited}s", flush=True)
    return waited


def watch(prom, spec, timeout_s, inject_at):
    alert = specs.resolve_alert(spec.alert)
    want_red = spec.expected != "noop" and spec.customer_impact
    start = time.monotonic()
    alert_after = 0 if spec.synthetic else None
    red_after = red_reason = None
    while True:
        elapsed = round(time.monotonic() - start)
        try:
            active_at = prom.firing().get(alert)
            if alert_after is None and active_at and parse_iso(active_at) >= inject_at:
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


def alert_payload(alert_name, prom):
    for alert in prom.alerts():
        if alert["labels"].get("alertname") == alert_name and alert.get("state") == "firing":
            return {"alertname": alert_name, "labels": alert.get("labels", {}), "annotations": alert.get("annotations", {}), "activeAt": alert.get("activeAt")}
    rules = yaml.safe_load((MONITORING_DIR / "rules.yaml").read_text())
    for group in rules["spec"]["groups"]:
        for rule in group["rules"]:
            if rule.get("alert") == alert_name:
                labels = {"alertname": alert_name, **rule.get("labels", {})}
                return {"alertname": alert_name, "labels": labels, "annotations": rule.get("annotations", {}), "activeAt": now_iso()}
    return {"alertname": alert_name, "labels": {"alertname": alert_name}, "annotations": {}, "activeAt": now_iso()}


def agent_mode():
    mode = os.environ.get("ONCALL_MODE", "diagnose")
    if mode not in AGENT_MODES:
        raise HarnessError(f"ONCALL_MODE must be one of {AGENT_MODES}, got {mode!r}")
    return mode


def revisions_at_alert(api):
    found = {}
    for deploy in client.AppsV1Api(api).list_namespaced_deployment(APP_NAMESPACE).items:
        revision = (deploy.metadata.annotations or {}).get(REVISION_ANNOTATION)
        if revision:
            found[deploy.metadata.name] = int(revision)
    return found


def harness_verify(prom):
    def verify(timeout_s):
        start = time.monotonic()
        while True:
            try:
                current = score.health(prom)
            except Exception as exc:
                current = score.Health(False, f"health query failed: {exc}")
            waited = round(time.monotonic() - start)
            if current.healthy or waited >= timeout_s:
                return {"improved": current.healthy, "waited_s": waited, "health": current.as_dict()}
            time.sleep(score.POLL_S)

    return verify


def run_agent(name, spec, prom, api, mode):
    sys.path.insert(0, str(ROOT / "agent"))
    import llm
    import loop

    alert_name = specs.resolve_alert(spec.alert)
    model = llm.Client()
    print(f"  agent: {model.describe()}, mode {mode}", flush=True)
    alert = alert_payload(alert_name, prom)
    row = {"agent_model": model.describe(), "health_at_alert": score.health(prom).as_dict()}
    before = None
    if mode == "act":
        alert["revision_at_alert"] = revisions_at_alert(api)
        row["revision_at_alert"] = alert["revision_at_alert"]
        before = invariants.snapshot(api)
    approved = []

    def approver(intent):
        approved.append(intent.target)
        print(f"  approve: {intent.summary} (harness auto-approver)", flush=True)
        return "harness"

    agent_start = now_iso()
    result = loop.run(name, alert, model, mode=mode, verify=harness_verify(prom) if mode == "act" else None, approver=approver, time_budget_s=spec.time_budget_s)
    diagnosis = result["diagnosis"]
    match = score.label_match(spec.root_cause, diagnosis) if diagnosis else False
    if diagnosis:
        verdict = "MATCH" if match else f"expected {spec.root_cause}"
        print(f"  diagnosis: {diagnosis['root_cause']} ({diagnosis['confidence']:.2f}) decision {diagnosis['decision']} after {result['reads']} reads, {result['elapsed_s']}s -> {verdict}", flush=True)
    row.update(
        diagnosis=diagnosis,
        agent_reads=result["reads"],
        agent_trace=result["trace"],
        agent_elapsed_s=result["elapsed_s"],
        agent_tokens=result["usage"],
        label_match=match,
        agent_actions=result["actions"],
        agent_writes=result["writes"],
        agent_escalated=result["escalated"],
        agent_escalation_reason=result["escalation_reason"],
        agent_note=result["note"],
        agent_timed_out=result["timed_out"],
        confidence_floor=result["floor"],
        health_before_writes=result.get("health_before_writes"),
    )
    if approved:
        row["approved_by"] = "harness"
    if mode == "act":
        if spec.settle_s:
            print(f"  settling {spec.settle_s}s before the final health check", flush=True)
            time.sleep(spec.settle_s)
        final = score.health(prom)
        row["health_final"] = final.as_dict()
        approved_cms = {a["target"].split("/", 1)[1] for a in result["actions"] if a.get("tool") == "patch_configmap" and a.get("status") in score.APPLIED}
        row["invariants_broken"] = invariants.diff(before, invariants.snapshot(api), approved_cms)
        row["audit"] = audit.summary(agent_start)
        row["audit_attempts"] = audit.attempts(agent_start)
        row["acceptable_actions"] = [[a.tool, a.target] for a in spec.acceptable_actions]
        row["outcome"], row["outcome_reasons"] = score.outcome(
            spec.expected, row["acceptable_actions"], result["actions"], result["escalated"],
            row.get("health_before_writes") or row["health_at_alert"], row["health_final"], row["invariants_broken"], row["audit_attempts"], result["timed_out"],
        )
        kept = [a for a in result["actions"] if a.get("status") == "kept"]
        if kept and final.healthy:
            last = kept[-1]
            row["healthy_at"] = (parse_iso(last["applied_at"]) + dt.timedelta(seconds=(last.get("verify") or {}).get("waited_s") or 0)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  outcome: {row['outcome']}" + (f" ({'; '.join(row['outcome_reasons'][:3])})" if row["outcome_reasons"] else ""), flush=True)
    return row


def passed(row):
    if row.get("expected") == "noop":
        red_ok = True
    elif row.get("customer_impact", True):
        red_ok = row.get("red_after_inject")
    else:
        red_ok = not row.get("red_after_inject")
    bench = bool(row.get("healthy_before") and row.get("alert_fired") and red_ok and row.get("healthy_after_reset") and not row.get("error"))
    if row.get("mode") == "act":
        return bench and row.get("outcome") in score.PASS_OUTCOMES
    return bench


def run_fault(name, spec, mode, sealed_run=False):
    print(f"\n=== {spec.id} {spec.title} (expected {spec.expected}) ===", flush=True)
    row = {"ts": now_iso(), "sandbox": name, "fault": spec.id, "mode": mode, "expected": spec.expected, "customer_impact": spec.customer_impact, "healthy_before": False}
    if sealed_run:
        row["sealed_run"] = True
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
        if not spec.synthetic:
            row["alert_clear_s"] = wait_alert_clear(prom, specs.resolve_alert(spec.alert), ALERT_CLEAR_TIMEOUT_S)
        row["inject_at"] = now_iso()
        for command in spec.inject:
            run_spec_command(name, command)
        row.update(watch(prom, spec, ALERT_TIMEOUT_S, parse_iso(row["inject_at"])))
        if mode in AGENT_MODES and row.get("alert_fired"):
            try:
                row.update(run_agent(name, spec, prom, api, mode))
            except Exception as exc:
                row["agent_error"] = f"{exc.__class__.__name__}: {exc}"
                if mode == "act":
                    row["outcome"], row["outcome_reasons"] = "error", [row["agent_error"]]
                print(f"  agent failed: {exc}", file=sys.stderr, flush=True)
            if mode == "act" and row.get("healthy_at") and row.get("alert_after_s") is not None:
                alert_at = parse_iso(row["inject_at"]) + dt.timedelta(seconds=row["alert_after_s"])
                row["time_to_healthy_s"] = round((parse_iso(row["healthy_at"]) - alert_at).total_seconds())
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
    scored = [r for r in rows if r.get("outcome")]
    print("\nfault  expected  alert_s  red_s  reset_ok  result  outcome     error")
    for row in rows:
        print(
            f"{row['fault']:<6} {row['expected']:<9} {str(row.get('alert_after_s')):<8} {str(row.get('red_after_s')):<6} "
            f"{str(row.get('healthy_after_reset')):<9} {'PASS' if passed(row) else 'FAIL':<7} {str(row.get('outcome', '-')):<11} {row.get('error', '')[:80]}"
        )
    print(f"\n{sum(passed(r) for r in rows)}/{len(rows)} passed")
    if scored:
        print(score.summary_line(scored))


def self_test(name, skip, partial, seed, only=None, category=None):
    library = specs.load_library(complete=not partial)
    if only:
        unknown = sorted(only - set(library))
        if unknown:
            raise HarnessError(f"--only names unknown faults: {' '.join(unknown)}")
    chosen = [s for s in library.values() if s.id not in skip and (not only or s.id in only) and (not category or s.category == category)]
    if not chosen:
        raise HarnessError("no faults left to run after --only, --category and --skip")
    random.Random(seed).shuffle(chosen)
    print(f"self-test: {len(chosen)} faults, order {' '.join(s.id for s in chosen)}", flush=True)
    rows = [run_fault(name, spec, "self-test") for spec in chosen]
    print_summary(rows)
    return all(passed(r) for r in rows)


def sealed_faults_seen(sealed_ids):
    seen = []
    for path in sorted(RUNS_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("fault") in sealed_ids and row.get("mode") in AGENT_MODES and not row.get("sealed_run"):
                seen.append(f"{row['fault']} met the agent on {row.get('ts', '?')[:10]} in {row['mode']} mode ({path.name})")
    return seen


def sealed_run(name, skip, seed, mode):
    if mode != "act":
        raise HarnessError("the sealed run needs ONCALL_MODE=act; the sealed faults are run once, cold, with writes on")
    library = specs.load_library(complete=True)
    sealed = [s for s in library.values() if s.sealed]
    seen = sealed_faults_seen({s.id for s in sealed})
    if seen:
        raise HarnessError("refusing the sealed run, the agent has already seen a sealed fault:\n  " + "\n  ".join(seen))
    chosen = [s for s in sealed if s.id not in skip]
    random.Random(seed).shuffle(chosen)
    print(f"sealed run: {len(chosen)} sealed faults in act mode, order {' '.join(s.id for s in chosen)}", flush=True)
    rows = [run_fault(name, spec, mode, sealed_run=True) for spec in chosen]
    print_summary(rows)
    return all(passed(r) for r in rows)


def dev_run(name, skip, seed, mode):
    library = specs.load_library(complete=True)
    chosen = [s for s in library.values() if not s.sealed and s.id not in skip]
    random.Random(seed).shuffle(chosen)
    print(f"dev run: {len(chosen)} faults with the agent in {mode} mode, order {' '.join(s.id for s in chosen)}", flush=True)
    rows = [run_fault(name, spec, mode) for spec in chosen]
    print_summary(rows)
    matched = sum(1 for r in rows if r.get("label_match"))
    print(f"\ndiagnosis: {matched} of {len(rows)} root causes matched; run harness/report.py for the breakdown", flush=True)
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
    action.add_argument("--dev-run", action="store_true", help="run every dev-set fault once with the agent; ONCALL_MODE=diagnose (default) or act")
    action.add_argument("--sealed-run", action="store_true", help="run the sealed faults once with the agent in act mode; refused if any sealed fault has met the agent before")
    action.add_argument("--soak", type=int, metavar="SECONDS", help="watch a clean cluster for alerts and red health")
    action.add_argument("--bootstrap", action="store_true", help="install monitoring, the app and the harness workloads")
    action.add_argument("--build", action="store_true", help="build the app image with no registry login and load it into the sandbox")
    action.add_argument("--validate", action="store_true", help="load and validate the fault library")
    parser.add_argument("--no-agent", action="store_true", help="inject and confirm only, no agent")
    parser.add_argument("--skip", default="", help="comma-separated fault ids to leave out of --self-test, --dev-run or --sealed-run")
    parser.add_argument("--only", default="", help="comma-separated fault ids to run in --self-test (CI smoke)")
    parser.add_argument("--category", default=None, help="run only this category in --self-test (CI nightly)")
    parser.add_argument("--partial", action="store_true", help="allow a library with fewer than 50 specs")
    parser.add_argument("--seed", type=int, default=None, help="shuffle seed for the multi-fault runs")
    args = parser.parse_args(argv)
    try:
        sandbox.validate_name(args.sandbox)
        mode = agent_mode()
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
            only = {s.strip() for s in args.only.split(",") if s.strip()}
            ok = self_test(args.sandbox, skip, args.partial, args.seed, only, args.category)
        elif args.dev_run:
            skip = {s.strip() for s in args.skip.split(",") if s.strip()}
            ok = dev_run(args.sandbox, skip, args.seed, mode)
        elif args.sealed_run:
            skip = {s.strip() for s in args.skip.split(",") if s.strip()}
            ok = sealed_run(args.sandbox, skip, args.seed, mode)
        else:
            library = specs.load_library()
            if args.fault not in library:
                raise HarnessError(f"no fault {args.fault!r}; known: {' '.join(sorted(library))}")
            spec = library[args.fault]
            if not args.no_agent and spec.sealed:
                raise HarnessError(f"{spec.id} is sealed; it never runs against the agent before week 4")
            row = run_fault(args.sandbox, spec, "no-agent" if args.no_agent else mode)
            ok = passed(row) and (args.no_agent or mode == "act" or bool(row.get("label_match")))
    except (HarnessError, SandboxError, specs.SpecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
