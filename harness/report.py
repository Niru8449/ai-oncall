#!/usr/bin/env python3
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "reports" / "runs"
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


def load_rows(paths, since, mode="diagnose"):
    rows = []
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("mode") == mode and row.get("ts", "") >= since:
                rows.append(row)
    return rows


def report_act(rows, out=sys.stdout):
    sys.path.insert(0, str(ROOT / "harness"))
    import score

    if not rows:
        print("no act rows found", file=out)
        return
    print(score.summary_line(rows), file=out)
    print(f"faults: {len(rows)}  label matched: {sum(1 for r in rows if r.get('label_match'))}  writes: {sum(r.get('agent_writes') or 0 for r in rows)}  approved proposals: {sum(1 for r in rows if r.get('approved_by'))}", file=out)
    harmful = [r for r in rows if r.get("outcome") == "harmful"]
    if harmful:
        print("\nharmful, with the reason the scorer gave:", file=out)
        for r in harmful:
            print(f"  {r['fault']}: " + "; ".join(r.get("outcome_reasons") or []), file=out)
    healthy = [r.get("time_to_healthy_s") for r in rows if r.get("time_to_healthy_s") is not None]
    if healthy:
        print(f"\ntime from alert to healthy, fixed faults: median {sorted(healthy)[len(healthy) // 2]}s, max {max(healthy)}s", file=out)
    print("\nper fault:", file=out)
    for r in rows:
        d = r.get("diagnosis") or {}
        acts = ", ".join(f"{a.get('tool')}{'' if not a.get('target') else ' ' + a['target']}:{a.get('status')}" for a in r.get("agent_actions") or []) or "-"
        label = d.get("root_cause", "-")
        mark = "ok " if r.get("outcome") in score.PASS_OUTCOMES else "XX "
        print(f"  {mark}{r['fault']} {r['expected']:9s} {str(r.get('outcome')):10s} {label:26s} conf {d.get('confidence', 0):.2f}  {acts}", file=out)
        if r.get("outcome") not in score.PASS_OUTCOMES and r.get("outcome_reasons"):
            print(f"       {'; '.join(r['outcome_reasons'])[:160]}", file=out)


def latest_per_fault(rows):
    latest = {}
    for row in rows:
        latest[row["fault"]] = row
    return sorted(latest.values(), key=lambda r: r["fault"])


def report(rows, out=sys.stdout):
    if not rows:
        print("no diagnose rows found", file=out)
        return
    diagnosed = [r for r in rows if r.get("diagnosis")]
    matched = [r for r in diagnosed if r.get("label_match")]
    print(f"faults: {len(rows)}  diagnosed: {len(diagnosed)}  matched: {len(matched)}  accuracy: {len(matched) / len(rows):.0%}", file=out)
    failed = [r for r in rows if not r.get("diagnosis")]
    if failed:
        print("no diagnosis: " + ", ".join(f"{r['fault']} ({r.get('agent_error') or r.get('error') or 'no alert'})" for r in failed), file=out)

    print("\nconfused pairs (expected -> diagnosed, count):", file=out)
    pairs = Counter((r["expected_root_cause"] if "expected_root_cause" in r else spec_label(r), r["diagnosis"]["root_cause"]) for r in diagnosed if not r.get("label_match"))
    for (exp, got), n in pairs.most_common():
        print(f"  {exp} -> {got}  {n}", file=out)
    if not pairs:
        print("  none", file=out)

    print("\naccuracy by confidence bucket:", file=out)
    for low, high in BUCKETS:
        group = [r for r in diagnosed if low <= r["diagnosis"]["confidence"] < high]
        if not group:
            continue
        ok = sum(1 for r in group if r.get("label_match"))
        label = f"{low:.1f}-{min(high, 1.0):.1f}"
        print(f"  {label}: {ok}/{len(group)} ({ok / len(group):.0%})", file=out)

    reads = [r.get("agent_reads", 0) for r in diagnosed]
    secs = [r.get("agent_elapsed_s", 0) for r in diagnosed]
    if reads:
        print(f"\nreads per diagnosis: mean {sum(reads) / len(reads):.1f}, min {min(reads)}, max {max(reads)}; time per diagnosis: mean {sum(secs) / len(secs):.0f}s, max {max(secs)}s", file=out)

    print("\ndecision vs expected:", file=out)
    table = defaultdict(Counter)
    for r in diagnosed:
        table[r["expected"]][r["diagnosis"]["decision"]] += 1
    for expected in ("fix", "propose", "escalate", "noop"):
        if table[expected]:
            print(f"  expected {expected:9s}: " + ", ".join(f"{d} {n}" for d, n in table[expected].most_common()), file=out)

    print("\nper fault:", file=out)
    for r in rows:
        d = r.get("diagnosis")
        if d:
            mark = "ok " if r.get("label_match") else "XX "
            print(f"  {mark}{r['fault']} {spec_label(r):28s} got {d['root_cause']:28s} conf {d['confidence']:.2f} {d['decision']:9s} reads {r.get('agent_reads', 0):2d} {r.get('agent_elapsed_s', 0):4d}s", file=out)
        else:
            print(f"  -- {r['fault']} {spec_label(r):28s} {r.get('agent_error') or r.get('error') or 'no alert'}", file=out)


_LABELS = None


def spec_label(row):
    global _LABELS
    if _LABELS is None:
        sys.path.insert(0, str(ROOT / "harness"))
        import specs

        _LABELS = {s.id: s.root_cause for s in specs.load_library(complete=False).values()}
    return _LABELS.get(row["fault"], "?")


def main(argv=None):
    parser = argparse.ArgumentParser(description="diagnosis report from the JSONL run rows")
    parser.add_argument("--since", default="", help="only rows with ts >= this ISO timestamp")
    parser.add_argument("--all-runs", action="store_true", help="count every row, not only the latest row per fault")
    parser.add_argument("--mode", default="diagnose", choices=("diagnose", "act"), help="which rows to report: diagnose (default) or act")
    parser.add_argument("files", nargs="*", help="JSONL files; default: every reports/runs/*.jsonl")
    args = parser.parse_args(argv)
    paths = [Path(f) for f in args.files] or sorted(RUNS_DIR.glob("*.jsonl"))
    rows = load_rows(paths, args.since, args.mode)
    if not args.all_runs:
        rows = latest_per_fault(rows)
    if args.mode == "act":
        report_act(rows)
    else:
        report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
