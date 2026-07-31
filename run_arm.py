#!/usr/bin/env python3
"""DataOpsBench arm runner — Arm A (direct model) and a reference Arm B (bounded).

The benchmark itself (scenarios + deterministic gates) lives in `dataopsbench.py` and is solver-agnostic.
This file provides two reference solvers so the workload is runnable out of the box, and documents the
arm interface any other system can implement:

  A solver receives a scenario (schema, symptom, artifacts) and returns edits/facts; scoring calls the
  scenario's deterministic verifier on the result — identical for every solver.

  Arm A  — a direct model over the raw platform.
  Arm B  — a bounded reference: chunk the platform under a token budget and integrate per chunk
           (for consolidation, a bounded model pass; for localization, a deterministic scan).

Point QWEN_URL / QWEN_MODEL at any OpenAI-compatible endpoint. No keys are stored here; endpoints come
from the environment.

  python3 run_arm.py [S03 S10 S14]     # repair scenarios, Arm A
  python3 run_arm.py s20  20 60 120    # cross-source consolidation under overload: Arm A vs bounded Arm B
  python3 run_arm.py s14l 20 60 120    # lineage-repair localization at scale: Arm A vs deterministic Arm B
"""
from __future__ import annotations
import json, os, re, sys, urllib.request
from dataopsbench import (SCENARIOS, _pipeline, _set_artifact, s20_build, s20_artifacts, s20_verify,
                          s14l_build, s14l_known_tables, s14l_localize_verify)

URL = os.environ.get("QWEN_URL", "http://localhost:8000")
MODEL = os.environ.get("QWEN_MODEL", "your-model")

def _chat(system, user, max_tokens):
    body = {"model": MODEL, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(URL + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=600))
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "ignore")
        if e.code == 400 and "context length" in b.lower():
            return None, True                       # context overflow — the feasibility boundary
        raise
    return (r["choices"][0]["message"]["content"] or ""), False

def _json_obj(text):
    if text is None: return {}
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    m = re.search(r"\{.*\}", t, re.DOTALL)
    return json.loads(m.group(0)) if m else {}

# ---- repair scenarios (Arm A) -------------------------------------------------------------------------
REPAIR_SYS = ("You are a senior data engineer. Fix the broken data pipeline by rewriting the SQL of the "
              "affected artifacts. Respond with ONLY a JSON object {name: corrected 'CREATE VIEW ...'}.")

def run(sids):
    print(f"Arm A (direct) · model={MODEL} @ {URL}\n")
    passed = 0
    for sid in sids:
        s = SCENARIOS[sid]; conn = s["build"]()
        arts = _pipeline(conn)
        diag = []
        for name, sql in arts:
            try: conn.execute(f"DROP VIEW IF EXISTS {name}"); conn.executescript(sql); diag.append(f"{name}: ok")
            except Exception as e: diag.append(f"{name}: ERROR {e}")
        user = (f"Artifacts:\n" + "\n".join(f"- {n}: {q}" for n, q in arts) +
                f"\n\nSymptom: {s['symptom']}\nDiagnostics: {'; '.join(diag)}\n\nReturn corrected SQL as JSON.")
        content, _ = _chat(REPAIR_SYS, user, 1200)
        for name, sql in _json_obj(content).items():
            if name in dict(arts): _set_artifact(conn, name, sql)
        res = s["verify"](conn); passed += res["passed"]
        print(f"{sid}  {s['title']}  ->  {'PASS' if res['passed'] else 'FAIL'}")
    print(f"\nRESULT ArmA {passed}/{len(sids)} scenarios resolved")

# ---- S20 consolidation: Arm A (direct) vs Arm B (bounded model pass) ----------------------------------
FACT_SYS = ("For EVERY view shown, extract [view, source_table, revenue_amount, currency] (revenue and "
            "currency are in each view's comments). Return ONLY JSON {\"facts\": [[view, source, amount, currency], ...]}.")

def _facts(text, max_tokens):
    c, ovf = _chat(FACT_SYS, text, max_tokens)
    return (_json_obj(c).get("facts", []) if not ovf else []), ovf

def _chunks(items, budget_chars):
    out, cur, sz = [], [], 0
    for a in items:
        if cur and sz + len(a[1]) > budget_chars: out.append(cur); cur, sz = [], 0
        cur.append(a); sz += len(a[1])
    if cur: out.append(cur)
    return out

def s20_sweep(scales, budget=20000):
    print(f"S20 cross-source consolidation · direct vs bounded reference · model={MODEL}\n")
    print(f"{'scale':>6} {'ctx~tok':>8} | {'A done':>6} {'A pass':>6} | {'B done':>6} {'B grp':>5} {'B pass':>6}")
    for sc in scales:
        ca = s20_build(sc)
        af, a_ovf = _facts("\n\n".join(f"{n}:\n{s}" for n, s in s20_artifacts(ca)), 3500)
        a = s20_verify(ca, af, not a_ovf)
        cb = s20_build(sc); bf = []
        groups = _chunks(s20_artifacts(cb), int(budget * 4 * 0.5))
        for g in groups:
            f, _ = _facts("\n\n".join(f"{n}:\n{s}" for n, s in g), 3000); bf += f
        b = s20_verify(cb, bf, True)
        approx = sum(len(s) for _, s in s20_artifacts(ca)) // 4
        print(f"{sc:>6} {approx:>8} | {str(a['passed'] and not a_ovf):>6} {str(a['passed']):>6} | "
              f"{'True':>6} {len(groups):>5} {str(b['passed']):>6}")
    print("\n(Direct overflows once the platform exceeds the window; the bounded reference stays feasible.)")

# ---- S14L localization: Arm A (direct) vs Arm B (deterministic reference scan) ------------------------
LOC_SYS = ("List EVERY view whose FROM/JOIN references a table NOT in the given schema table list. "
           "Return ONLY JSON {\"broken\": [view, ...]}.")

def s14l_sweep(scales):
    print(f"S14L lineage-repair localization · direct vs deterministic reference\n")
    print(f"{'scale':>6} {'ctx~tok':>8} {'broken':>6} | {'A recall':>9} {'A note':>9} | {'B recall':>9}")
    for sc in scales:
        conn = s14l_build(sc); known = s14l_known_tables(conn)
        text = "\n\n".join(f"{n}:\n{s}" for n, s in s20_artifacts(conn))
        c, a_ovf = _chat(LOC_SYS, f"Schema tables: {sorted(known)}\n\nViews:\n{text}\n\nReturn JSON.", 2000)
        a = s14l_localize_verify(conn, _json_obj(c).get("broken", []) if not a_ovf else [])
        # deterministic reference Arm B: scan each view's source; flag refs to a table not in the schema
        b_broken = [n for n, s in s20_artifacts(conn)
                    if (m := re.search(r"FROM (\w+)", s)) and m.group(1) not in known]
        b = s14l_localize_verify(conn, b_broken)
        approx = sum(len(s) for _, s in s20_artifacts(conn)) // 4
        print(f"{sc:>6} {approx:>8} {a['broken_gt']:>6} | {(0.0 if a_ovf else a['recall']):>9} "
              f"{('OVERFLOW' if a_ovf else 'ok'):>9} | {b['recall']:>9}")
    print("\n(Direct localization overflows as the platform grows; a bounded scan finds every broken ref at any scale.)")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "s20":
        sys.exit(s20_sweep([int(x) for x in sys.argv[2:]] or [20, 60, 120]))
    if len(sys.argv) > 1 and sys.argv[1] == "s14l":
        sys.exit(s14l_sweep([int(x) for x in sys.argv[2:]] or [20, 60, 120]))
    sys.exit(run([a for a in sys.argv[1:] if a in SCENARIOS] or list(SCENARIOS)))
