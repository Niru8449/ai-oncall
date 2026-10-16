import json
import sys
import time
from pathlib import Path

import safety
from tools import read

PROMPTS = Path(__file__).resolve().parent / "prompts"
LABELS = [line.strip() for line in (PROMPTS / "labels.txt").read_text().splitlines() if line.strip()]
SYSTEM = (PROMPTS / "system.md").read_text().replace("{labels}", "\n".join(LABELS))
DECISIONS = ("fix", "propose", "escalate", "noop")
MAX_READS = 20
MAX_TURNS = 40


class DiagnosisError(Exception):
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


def diagnose(sandbox_name, alert, client, max_reads=MAX_READS, log=print):
    api = safety.connect_to_sandbox(sandbox_name)
    tools = read.ReadTools(sandbox_name, api)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Alert received:\n" + json.dumps(alert, indent=2) + "\n\nInvestigate with the tools, then answer with the diagnosis JSON only."},
    ]
    reads = 0
    trace = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    start = time.monotonic()
    for _ in range(MAX_TURNS):
        message, used = client.chat(messages, tools=read.TOOL_SPECS if reads < max_reads else None)
        for key in usage:
            usage[key] += int(used.get(key) or 0)
        messages.append(_assistant_message(message))
        calls = message.get("tool_calls") or []
        if not calls:
            diagnosis = parse_diagnosis(message.get("content"))
            return {"diagnosis": diagnosis, "reads": reads, "trace": trace, "usage": usage, "elapsed_s": round(time.monotonic() - start)}
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if reads >= max_reads:
                result = f"read budget of {max_reads} used up; answer now with the diagnosis JSON"
            else:
                reads += 1
                try:
                    result = read.call(tools, name, args)
                except read.ToolError as exc:
                    result = f"tool error: {exc}"
            trace.append({"tool": name, "args": args, "chars": len(result)})
            log(f"  read {reads}: {name} {json.dumps(args)} -> {len(result)} chars")
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name, "content": result})
        if reads >= max_reads:
            messages.append({"role": "user", "content": f"You have used all {max_reads} reads. Answer now with the diagnosis JSON only."})
    raise DiagnosisError(f"no diagnosis after {MAX_TURNS} turns")


def main(argv=None):
    import llm

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: loop.py <sandbox> <alertname>", file=sys.stderr)
        return 2
    alert = {"alertname": argv[1], "labels": {"severity": "page", "namespace": "storefront"}, "annotations": {}}
    try:
        result = diagnose(argv[0], alert, llm.Client())
    except (safety.Refused, llm.LLMError, DiagnosisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result["diagnosis"], indent=2))
    print(f"reads {result['reads']}, {result['elapsed_s']}s, tokens {result['usage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
