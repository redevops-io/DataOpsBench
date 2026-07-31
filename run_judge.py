#!/usr/bin/env python3
"""Judge-graded family for DataOpsBench — the subjective axis (incident-report quality).

SECONDARY to a deterministic groundedness gate, per the plan (§12/§22). Both arms (Qwen) write an incident
report for the S10 reconciliation incident. Arm B additionally receives the runtime's VERIFIED evidence
(root cause + reconciled total + fix, produced by the deterministic operators); Arm A must derive it
alone. A deterministic groundedness gate scores each report against ground truth; then a BLINDED, PAIRWISE
judge from a DIFFERENT model family (DeepSeek, local — the arms are Qwen) picks the better report, with the
X/Y order randomized across seeds to cancel position bias.

  QWEN_URL=... JUDGE_URL=http://localhost:8001 JUDGE_MODEL=your-judge-model python3 run_judge.py [seeds]
"""
from __future__ import annotations
import json, os, re, sys, random, urllib.request
from dataopsbench import s10_build, get_gt, _pipeline

ARM_URL = os.environ.get("QWEN_URL", "http://localhost:8000")
ARM_MODEL = os.environ.get("QWEN_MODEL", "your-arm-model")
JUDGE_URL = os.environ.get("JUDGE_URL", "http://localhost:8001")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "your-judge-model")

def _chat(url, model, system, user, max_tokens=900):
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "Qwen" in model:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=400))["choices"][0]["message"]["content"] or ""

def s10_evidence(conn) -> dict:
    """The runtime's verified evidence — what its deterministic reconcile/lineage operators establish."""
    return {"root_cause": "finance_report double-counts revenue by SUMming over a fan-out join to the "
                          "commissions table (policies with multiple agents are counted multiple times).",
            "correct_total": get_gt(conn)["source_total"], "affected_pipeline": "finance_report",
            "fix": "SUM source_premiums directly (or aggregate commissions to one row per policy before joining)."}

REPORT_SYS = ("You are an on-call data engineer. Write a concise incident report in markdown with sections "
              "Summary, Root cause, Fix, Evidence, Verification. Be specific and factual.")

def arm_report(conn, with_evidence: bool) -> str:
    ev = s10_evidence(conn)
    base = ("Incident: the finance revenue report disagrees with source totals.\nPipeline artifacts:\n"
            + "\n".join(f"- {n}: {s}" for n, s in _pipeline(conn))
            + "\nSymptom: finance_report.total is higher than SUM(source_premiums.amount).")
    if with_evidence:
        base += ("\n\nVERIFIED RUNTIME EVIDENCE (use it; do not contradict it):\n"
                 f"- root cause: {ev['root_cause']}\n- correct reconciled total (USD): {ev['correct_total']}\n"
                 f"- affected pipeline: {ev['affected_pipeline']}\n- fix: {ev['fix']}")
    return _chat(ARM_URL, ARM_MODEL, REPORT_SYS, base + "\n\nWrite the incident report.")

def groundedness(report: str, conn) -> tuple[float, list]:
    gt = get_gt(conn); r = report.lower()
    r_num = re.sub(r"[,\s$]", "", report)          # normalize numbers (strip commas/$/space)
    checks = [("names affected pipeline", "finance_report" in r),
              ("names root cause", any(k in r for k in ["fan-out", "fanout", "double", "join", "commission"])),
              ("states correct total", str(gt["source_total"]) in r_num),
              ("describes the fix", any(k in r for k in ["source_premiums", "aggregate", "one row", "distinct", "group by", "sum("]))]
    return sum(ok for _, ok in checks) / len(checks), checks

JUDGE_SYS = ("You are a neutral technical reviewer grading two incident reports for the SAME data incident. "
             "Judge accuracy vs. the ground truth, groundedness, completeness, and clarity. "
             "Respond ONLY JSON: {\"winner\":\"X\"|\"Y\",\"reason\":\"...\"}.")

def judge(conn, report_x: str, report_y: str) -> dict:
    ev = s10_evidence(conn)
    user = (f"Ground truth:\n- root cause: {ev['root_cause']}\n- correct total: {ev['correct_total']}\n"
            f"- affected pipeline: finance_report\n\nREPORT X:\n{report_x}\n\nREPORT Y:\n{report_y}\n\n"
            "Which report is better? JSON only.")
    out = _chat(JUDGE_URL, JUDGE_MODEL, JUDGE_SYS, user, max_tokens=2500)  # room for the judge's reasoning + JSON
    ms = re.findall(r"\{[^{}]*\"winner\"[^{}]*\}", out, re.DOTALL)         # take the JSON verdict (last if repeated)
    try:
        return json.loads(ms[-1]) if ms else {"winner": "?", "reason": out[:120]}
    except Exception:
        return {"winner": "?", "reason": out[:120]}

def main():
    seeds = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4]
    ca = s10_build(); ra = arm_report(ca, False); ga, ca_checks = groundedness(ra, ca)
    cb = s10_build(); rb = arm_report(cb, True);  gb, cb_checks = groundedness(rb, cb)
    print(f"S10-report · arms={ARM_MODEL} · judge={JUDGE_MODEL} (blinded pairwise)\n")
    print(f"deterministic groundedness  ->  Arm A {ga:.2f}   Arm B {gb:.2f}")
    for (n, a), (_, b) in zip(ca_checks, cb_checks):
        print(f"    [{'A' if a else '.'}{'B' if b else '.'}] {n}")
    print()
    wins = {"A": 0, "B": 0, "?": 0}
    for s in seeds:
        a_is_x = random.Random(s).random() < 0.5
        rx, ry = (ra, rb) if a_is_x else (rb, ra)
        v = judge(s10_build(), rx, ry)
        w = v.get("winner", "?")
        arm = ("?" if w not in ("X", "Y") else (("A" if a_is_x else "B") if w == "X" else ("B" if a_is_x else "A")))
        wins[arm] = wins.get(arm, 0) + 1
        print(f"  seed {s}: A={'X' if a_is_x else 'Y'}  judge->{w}  = Arm {arm}   ({v.get('reason','')[:90]})")
    print(f"\nRESULT groundedness A {ga:.2f} vs B {gb:.2f} | blinded judge wins A={wins['A']} B={wins['B']} ?={wins['?']}")

if __name__ == "__main__":
    main()
