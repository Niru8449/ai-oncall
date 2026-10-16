import json
import sys
import time
from pathlib import Path

import safety
from tools import act, read

PROMPTS = Path(__file__).resolve().parent / "prompts"
LABELS = [line.strip() for line in (PROMPTS / "labels.txt").read_text().splitlines() if line.strip()]
SYSTEM = (PROMPTS / "system.md").read_text().replace("{labels}", "\n".join(LABELS))
ACT_RULES = (PROMPTS / "act.md").read_text()
DECISIONS = ("fix", "propose", "escalate", "noop")
MAX_READS = 20
MAX_TURNS = 40
DEFAULT_TIME_BUDGET_S = 600


class DiagnosisError(Exception):
    pass


class TimeoutExceeded(Exception):
    pass


def parse_diagnosis(text):
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise DiagnosisError("no JSON object in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise DiagnosisError(f"diagnosis is not valid JSON: {exc}")
    root_cause = data.get("root_cause")
    if root_cause not in LABELS:
        raise DiagnosisError(f"root_cause {root_cause!r} is not in labels.txt")
    decision = data.get("decision")
    if decision not in DECISIONS:
        raise DiagnosisError(f"decision {decision!r} must be one of {DECISIONS}")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise DiagnosisError(f"confidence {data.get('confidence')!r} is not a number")
    if not 0 <= confidence <= 1:
        raise DiagnosisError(f"confidence {confidence} is outside 0..1")
    evidence = data.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]
    return {
        "root_cause": root_cause,
        "evidence": [str(e) for e in evidence][:10],
        "confidence": round(confidence, 3),
        "decision": decision,
        "action": data.get("action") or None,
    }


def _assistant_message(message):
    kept = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        kept["tool_calls"] = message["tool_calls"]
    return kept


def _arguments(call):
    try:
        return json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return {}


class Session:
    def __init__(self, sandbox_name, alert, client, mode, max_reads, time_budget_s, log):
        self.sandbox = sandbox_name
        self.alert = alert
        self.client = client
        self.mode = mode
        self.max_reads = max_reads
        self.log = log
        self.api = safety.connect_to_sandbox(sandbox_name)
        self.reads_tools = read.ReadTools(sandbox_name, self.api)
        self.reads = 0
        self.trace = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.start = time.monotonic()
        self.deadline = self.start + time_budget_s
        system = SYSTEM + ("\n\n" + ACT_RULES if mode == "act" else "")
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "Alert received:\n" + json.dumps(alert, indent=2) + "\n\nInvestigate with the tools, then answer with the diagnosis JSON only."},
        ]

    @property
    def reads_left(self):
        return self.reads < self.max_reads

    @property
    def time_left(self):
        return time.monotonic() < self.deadline

    def chat(self, tools):
        message, used = self.client.chat(self.messages, tools=tools or None)
        for key in self.usage:
            self.usage[key] += int(used.get(key) or 0)
        self.messages.append(_assistant_message(message))
        return message

    def tool_result(self, call, name, result):
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name, "content": result})

    def do_read(self, name, args):
        if not self.reads_left:
            return f"read budget of {self.max_reads} used up; answer now"
        self.reads += 1
        try:
            result = read.call(self.reads_tools, name, args)
        except read.ToolError as exc:
            result = f"tool error: {exc}"
        self.trace.append({"tool": name, "args": args, "chars": len(result)})
        self.log(f"  read {self.reads}: {name} {json.dumps(args)} -> {len(result)} chars")
        return result

    def investigate(self):
        for _ in range(MAX_TURNS):
            if not self.time_left:
                raise TimeoutExceeded("time budget used up before a diagnosis")
            message = self.chat(read.TOOL_SPECS if self.reads_left else None)
            calls = message.get("tool_calls") or []
            if not calls:
                return parse_diagnosis(message.get("content"))
            for call in calls:
                name = call["function"]["name"]
                args = _arguments(call)
                if name in read.HANDLERS:
                    result = self.do_read(name, args)
                else:
                    result = f"tool error: {name!r} is not available while investigating"
                self.tool_result(call, name, result)
            if not self.reads_left:
                self.messages.append({"role": "user", "content": f"You have used all {self.max_reads} reads. Answer now with the diagnosis JSON only."})
        raise DiagnosisError(f"no diagnosis after {MAX_TURNS} turns")

    def act(self, diagnosis, gate, acts):
        self.messages.append({"role": "user", "content": (
            f"Diagnosis recorded: {diagnosis['root_cause']} (confidence {diagnosis['confidence']}), decision {diagnosis['decision']}. "
            f"You may now act. Call one write tool at a time; each is watched for {safety.VERIFY_S} seconds and undone if health does not improve. "
            f"You have {gate.max_writes} writes. Call escalate(reason) if you should not act, and finish(summary) when the app is healthy again or you are done."
        )})
        nudged = False
        for _ in range(MAX_TURNS):
            writes_open = gate.budget_left and self.time_left
            if not writes_open and not nudged:
                self.messages.append({"role": "user", "content": "Your write or time budget is used up. Call escalate(reason) or finish(summary) now."})
                nudged = True
            tools = list(act.CONTROL_SPECS)
            if writes_open:
                tools += act.TOOL_SPECS
            if self.reads_left and self.time_left:
                tools += read.TOOL_SPECS
            message = self.chat(tools)
            calls = message.get("tool_calls") or []
            if not calls:
                return {"note": (message.get("content") or "").strip() or None}
            for call in calls:
                name = call["function"]["name"]
                args = _arguments(call)
                if name == "finish":
                    return {"note": str(args.get("summary") or "").strip() or None}
                if name == "escalate":
                    gate.escalate(str(args.get("reason") or "no reason given"))
                    return {"note": None}
                if name in read.HANDLERS:
                    result = self.do_read(name, args)
                elif name in act.AUTO | act.PROPOSE:
                    result = self.do_write(name, args, diagnosis, gate, acts)
                else:
                    record = gate.refuse(name, args, "allow-list", f"{name!r} is not a tool the agent has")
                    result = f"refused ({record['reason']}): {record['detail']}"
                self.tool_result(call, name, result)
        raise TimeoutExceeded(f"no finish or escalate after {MAX_TURNS} turns")

    def do_write(self, name, args, diagnosis, gate, acts):
        if not gate.budget_left:
            return "refused (budget): the write or time budget is used up; call escalate(reason) or finish(summary)"
        try:
            intent = acts.build(name, args)
        except act.Refusal as exc:
            record = gate.refuse(name, args, exc.code, exc.message)
            return f"refused ({exc.code}): {exc.message}"
        record = gate.write(intent, diagnosis["confidence"])
        return describe(record)


def describe(record):
    status = record["status"]
    if status == "kept":
        health = record.get("verify", {}).get("health") or {}
        return f"applied and kept: {record['summary']}. Health improved after {record.get('verify', {}).get('waited_s')}s" + (f" ({health.get('reason') or 'healthy'})" if health else "")
    if status == "undone":
        reason = (record.get("verify", {}).get("health") or {}).get("reason") or "not healthy"
        return f"applied then undone: {record['summary']}. Health did not improve within {safety.VERIFY_S}s ({reason}); the change was reverted. Reconsider, then escalate or try something else."
    if status == "proposed":
        return f"recorded as a proposal, not applied ({record.get('reason')}): {record['summary']}"
    if status == "failed":
        return f"the write failed ({record.get('detail')}); nothing changed"
    return f"refused ({record.get('reason')}): {record.get('detail')}"


def run(sandbox_name, alert, client, mode="diagnose", floor=None, verify=None, approver=None, max_writes=safety.MAX_WRITES, time_budget_s=DEFAULT_TIME_BUDGET_S, max_reads=MAX_READS, log=print):
    if mode not in safety.MODES:
        raise safety.Refused(f"unknown mode {mode!r}")
    session = Session(sandbox_name, alert, client, mode, max_reads, time_budget_s, log)
    floor = safety.floor_from_env() if floor is None else floor
    result = {"mode": mode, "floor": floor, "diagnosis": None, "actions": [], "writes": 0, "escalated": False, "escalation_reason": None, "note": None, "timed_out": False}
    try:
        diagnosis = session.investigate()
        result["diagnosis"] = diagnosis
        if mode == "act":
            gate = safety.Gate(sandbox_name, session.api, mode, floor, verify, approver, max_writes=max_writes, deadline=session.deadline, log=log)
            acts = act.ActTools(sandbox_name, alert.get("revision_at_alert"))
            if diagnosis["decision"] == "escalate":
                gate.escalate("diagnosis: " + "; ".join(diagnosis["evidence"][:3]))
            elif diagnosis["decision"] == "noop":
                result["note"] = "nothing to do: " + "; ".join(diagnosis["evidence"][:3])
            elif diagnosis["confidence"] < floor:
                gate.escalate(f"confidence {diagnosis['confidence']} is below the floor {floor}: " + "; ".join(diagnosis["evidence"][:3]))
            else:
                result.update(session.act(diagnosis, gate, acts))
            result.update(actions=gate.actions, writes=gate.writes, escalated=gate.escalation is not None, escalation_reason=gate.escalation, health_before_writes=gate.health_before_writes)
        else:
            if diagnosis["decision"] in ("fix", "propose") and diagnosis.get("action"):
                action = diagnosis["action"] if isinstance(diagnosis["action"], dict) else {"tool": str(diagnosis["action"])}
                result["actions"] = [{"ts": safety.now_iso(), "tool": action.get("tool"), "args": action.get("args"), "status": "proposed", "reason": "ONCALL_MODE=diagnose", "applied": False}]
            result["escalated"] = diagnosis["decision"] == "escalate"
    except TimeoutExceeded as exc:
        result["timed_out"] = True
        result["note"] = str(exc)
    finally:
        result.update(reads=session.reads, trace=session.trace, usage=session.usage, elapsed_s=round(time.monotonic() - session.start))
    return result


def diagnose(sandbox_name, alert, client, max_reads=MAX_READS, log=print):
    result = run(sandbox_name, alert, client, mode="diagnose", max_reads=max_reads, log=log)
    if result["diagnosis"] is None:
        raise DiagnosisError(result.get("note") or "no diagnosis")
    return result


def main(argv=None):
    import llm

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: loop.py <sandbox> <alertname>", file=sys.stderr)
        return 2
    alert = {"alertname": argv[1], "labels": {"severity": "page", "namespace": "storefront"}, "annotations": {}}
    mode = safety.mode_from_env()
    acts = act.ActTools(argv[0])

    def terminal_approver(intent):
        answer = input(f"approve? {intent.summary} [y/N] ").strip().lower()
        return "terminal" if answer == "y" else None

    try:
        result = run(argv[0], alert, llm.Client(), mode=mode, verify=acts.verify if mode == "act" else None, approver=terminal_approver)
    except (safety.Refused, llm.LLMError, DiagnosisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("mode", "diagnosis", "actions", "escalated", "escalation_reason", "note", "timed_out")}, indent=2))
    print(f"reads {result['reads']}, writes {result['writes']}, {result['elapsed_s']}s, tokens {result['usage']}")
    return 0 if result["diagnosis"] else 1


if __name__ == "__main__":
    sys.exit(main())
