import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAULTS_DIR = ROOT / "harness" / "faults"
RULES_FILE = ROOT / "monitoring" / "rules.yaml"
LABELS_FILE = ROOT / "agent" / "prompts" / "labels.txt"

EXPECTED = ("fix", "propose", "escalate", "noop")
RESETS = ("helm", "reinstall")
AUTO_TOOLS = {"rollout_restart", "rollback", "scale", "raise_limits", "delete_pod"}
PROPOSE_TOOLS = {
    "patch_probe", "patch_env", "patch_configmap", "patch_service",
    "patch_hpa", "patch_pdb", "patch_affinity", "patch_resources",
}
TOOLS = AUTO_TOOLS | PROPOSE_TOOLS
MIN_TIME_BUDGET_S = 360
TOTAL_FAULTS = 50
SEALED_FAULTS = 10
ID_RE = re.compile(r"^F\d{2}$")
DIR_RE = re.compile(r"^(F\d{2})-[a-z0-9-]+$")
COMMANDS = ("kubectl", "helm", "docker")
DOCKER_VERBS = ("stop", "start")
FORBIDDEN_FLAGS = ("--kubeconfig", "--context", "--kube-context", "--server", "-s", "--cluster", "--user", "--token", "--as")

REQUIRED = ("id", "title", "category", "inject", "alert", "expected", "root_cause")
DEFAULTS = {
    "acceptable_actions": [],
    "needs_fresh_cluster": False,
    "time_budget_s": 600,
    "settle_s": 0,
    "sealed": False,
    "loadgen_rps": 20,
    "cleanup": [],
    "reset": "helm",
}


class SpecError(Exception):
    pass


@dataclass(frozen=True)
class Action:
    tool: str
    target: str


@dataclass(frozen=True)
class Spec:
    id: str
    title: str
    category: str
    inject: tuple
    alert: str
    expected: str
    root_cause: str
    acceptable_actions: tuple
    needs_fresh_cluster: bool
    time_budget_s: int
    settle_s: int
    sealed: bool
    loadgen_rps: float
    cleanup: tuple
    reset: str
    path: Path = field(compare=False)

    @property
    def synthetic(self):
        return self.alert == "synthetic"


def alert_names():
    doc = yaml.safe_load(RULES_FILE.read_text())
    return {rule["alert"] for group in doc["spec"]["groups"] for rule in group["rules"] if "alert" in rule}


def resolve_alert(alert):
    return "SLOBreached" if alert in ("default", "synthetic") else alert


def check_command(command):
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SpecError(f"cannot parse command {command!r}: {exc}")
    if not argv or argv[0] not in COMMANDS:
        raise SpecError(f"command must start with one of {COMMANDS}: {command!r}")
    if argv[0] == "docker" and (len(argv) != 3 or argv[1] not in DOCKER_VERBS):
        raise SpecError(f"docker is allowed only as 'docker stop|start <node container>': {command!r}")
    for arg in argv[1:]:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise SpecError(f"flag {flag} is not allowed; commands always run against the sandbox: {command!r}")
    return argv


def _commands(raw, where):
    if not isinstance(raw, list) or not all(isinstance(c, str) and c.strip() for c in raw):
        raise SpecError(f"{where} must be a list of non-empty command strings")
    for command in raw:
        check_command(command)
    return tuple(raw)


def parse(path, alerts, labels):
    match = DIR_RE.match(path.parent.name)
    if not match:
        raise SpecError(f"{path.parent.name}: directory must be named Fnn-slug")
    doc = yaml.safe_load(path.read_text()) or {}
    if not isinstance(doc, dict):
        raise SpecError(f"{path}: not a mapping")
    unknown = set(doc) - set(REQUIRED) - set(DEFAULTS)
    if unknown:
        raise SpecError(f"{path}: unknown keys {sorted(unknown)}")
    missing = [key for key in REQUIRED if key not in doc]
    if missing:
        raise SpecError(f"{path}: missing keys {missing}")
    data = {**DEFAULTS, **doc}
    where = f"{path.parent.name}"

    if not ID_RE.match(str(data["id"])) or data["id"] != match.group(1):
        raise SpecError(f"{where}: id {data['id']!r} must match the directory prefix {match.group(1)}")
    for key in ("title", "category", "root_cause"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise SpecError(f"{where}: {key} must be a non-empty string")
    if data["expected"] not in EXPECTED:
        raise SpecError(f"{where}: expected must be one of {EXPECTED}, got {data['expected']!r}")
    if data["alert"] not in ("default", "synthetic") and data["alert"] not in alerts:
        raise SpecError(f"{where}: alert {data['alert']!r} is not default, synthetic, or a rule in {RULES_FILE.name}")
    if labels is not None and data["root_cause"] not in labels:
        raise SpecError(f"{where}: root_cause {data['root_cause']!r} is not in {LABELS_FILE.name}")

    inject = _commands(data["inject"], f"{where}: inject")
    if data["alert"] != "synthetic" and not inject:
        raise SpecError(f"{where}: inject is empty")
    cleanup = _commands(data["cleanup"], f"{where}: cleanup")

    actions = []
    for raw in data["acceptable_actions"] or []:
        if not isinstance(raw, dict) or set(raw) != {"tool", "target"} or not all(isinstance(v, str) and v for v in raw.values()):
            raise SpecError(f"{where}: each acceptable action needs exactly a tool and a target, got {raw!r}")
        if raw["tool"] not in TOOLS:
            raise SpecError(f"{where}: unknown tool {raw['tool']!r}")
        actions.append(Action(raw["tool"], raw["target"]))
    if data["expected"] in ("fix", "propose") and not actions:
        raise SpecError(f"{where}: expected {data['expected']} needs at least one acceptable action")
    if data["expected"] in ("escalate", "noop") and actions:
        raise SpecError(f"{where}: expected {data['expected']} must have no acceptable actions")

    for key in ("needs_fresh_cluster", "sealed"):
        if not isinstance(data[key], bool):
            raise SpecError(f"{where}: {key} must be true or false")
    if not isinstance(data["time_budget_s"], int) or data["time_budget_s"] < MIN_TIME_BUDGET_S:
        raise SpecError(f"{where}: time_budget_s must be an integer >= {MIN_TIME_BUDGET_S}")
    if not isinstance(data["settle_s"], int) or data["settle_s"] < 0:
        raise SpecError(f"{where}: settle_s must be an integer >= 0")
    if not isinstance(data["loadgen_rps"], (int, float)) or data["loadgen_rps"] <= 0:
        raise SpecError(f"{where}: loadgen_rps must be a number > 0")
    if data["reset"] not in RESETS:
        raise SpecError(f"{where}: reset must be one of {RESETS}")

    return Spec(
        id=data["id"],
        title=data["title"],
        category=data["category"],
        inject=inject,
        alert=data["alert"],
        expected=data["expected"],
        root_cause=data["root_cause"],
        acceptable_actions=tuple(actions),
        needs_fresh_cluster=data["needs_fresh_cluster"],
        time_budget_s=data["time_budget_s"],
        settle_s=data["settle_s"],
        sealed=data["sealed"],
        loadgen_rps=data["loadgen_rps"],
        cleanup=cleanup,
        reset=data["reset"],
        path=path,
    )


def load_library(complete=False):
    alerts = alert_names()
    labels = set(LABELS_FILE.read_text().split()) if LABELS_FILE.is_file() else None
    specs = {}
    errors = []
    for path in sorted(FAULTS_DIR.glob("*/fault.yaml")):
        try:
            spec = parse(path, alerts, labels)
        except SpecError as exc:
            errors.append(str(exc))
            continue
        if spec.id in specs:
            errors.append(f"duplicate id {spec.id}: {specs[spec.id].path.parent.name} and {path.parent.name}")
            continue
        specs[spec.id] = spec
    if complete:
        if len(specs) != TOTAL_FAULTS:
            errors.append(f"library has {len(specs)} valid specs, expected {TOTAL_FAULTS}")
        sealed = sorted(s.id for s in specs.values() if s.sealed)
        if len(sealed) != SEALED_FAULTS:
            errors.append(f"{len(sealed)} sealed specs {sealed}, expected {SEALED_FAULTS}")
    if errors:
        raise SpecError("fault library is invalid:\n  " + "\n  ".join(errors))
    return specs
