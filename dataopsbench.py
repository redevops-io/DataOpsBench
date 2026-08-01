#!/usr/bin/env python3
"""DataOpsBench v0 — deterministic scenario fixtures + verifiers (the model-agnostic core).

Three fully-deterministic scenarios (§9 of the plan), each self-contained:
  S03  duplicate policy records after a retried load
  S10  source and finance totals disagree (join fan-out double-count)
  S14  lineage breaks after a table rename

Each scenario builds a defective in-memory SQLite "Northstar" platform, exposes editable **pipeline
artifacts** (the SQL an arm may rewrite), and a **deterministic verifier** (the gates). No model calls
live here — this is the neutral workload any arm (ReDevOps runtime, LangGraph, Claude Code, Codex, …) is
scored on. `validate` proves the gates discriminate: the defect fails the gates before the fix and every
gate passes after the reference fix.

  python3 dataopsbench.py validate         # prove all scenarios' gates (no API keys, no model calls)
  python3 dataopsbench.py spec [S03|…]     # print the scenario spec(s)
"""
from __future__ import annotations
import json, re, sqlite3, sys

# --- deterministic helpers (no Math.random / real RNG — fixed LCG for reproducible fixtures) -----------
_SEED = 1729
def _ints(n: int, lo: int, hi: int) -> list[int]:
    out, x = [], _SEED
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out.append(lo + (x % (hi - lo + 1)))
    return out

def table_refs(sql: str) -> set[str]:
    """Lineage: tables referenced via FROM / JOIN (deterministic, no model)."""
    return {m.group(1).lower() for m in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_]\w*)", sql, re.I)}

def _rebuild_pipeline(conn: sqlite3.Connection, artifacts: list[tuple[str, str]]) -> dict[str, str | None]:
    """(Re)create each pipeline artifact (a CREATE VIEW) in order; return {name: error-or-None}."""
    errs: dict[str, str | None] = {}
    for name, sql in artifacts:
        try:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.executescript(sql)
            errs[name] = None
        except Exception as e:  # broken reference, syntax, etc.
            errs[name] = str(e)
    return errs

def _pipeline(conn) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT name, sql FROM pipeline ORDER BY ord")]

def _set_artifact(conn, name: str, sql: str):
    conn.execute("UPDATE pipeline SET sql=? WHERE name=?", (sql, name))

def set_gt(conn, gt: dict):
    conn.execute("CREATE TABLE IF NOT EXISTS _gt (j TEXT)")
    conn.execute("INSERT INTO _gt VALUES (?)", (json.dumps(gt),))

def get_gt(conn) -> dict:
    return json.loads(conn.execute("SELECT j FROM _gt").fetchone()[0])


# =====================================================================================================
# S03 — Duplicate policy records after a retried load
# =====================================================================================================
def s03_build() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE policies_raw (rid INTEGER PRIMARY KEY, policy_id TEXT, customer_id TEXT, premium INTEGER)")
    prem = _ints(50, 200, 2000)
    rows = [(f"POL{i:04d}", f"CUST{i%30:03d}", prem[i]) for i in range(50)]
    for r in rows:
        conn.execute("INSERT INTO policies_raw (policy_id, customer_id, premium) VALUES (?,?,?)", r)
    # the retry re-inserted the first 8 rows -> duplicates
    for r in rows[:8]:
        conn.execute("INSERT INTO policies_raw (policy_id, customer_id, premium) VALUES (?,?,?)", r)
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, sql TEXT)")
    conn.execute("INSERT INTO pipeline VALUES (1,'policies_clean',?)",
                 ("CREATE VIEW policies_clean AS SELECT policy_id, customer_id, premium FROM policies_raw",))
    conn.commit()
    set_gt(conn, {"distinct": 50, "total": sum(prem)})  # hidden ground truth
    return conn

def s03_reference_fix(conn):
    _set_artifact(conn, "policies_clean",
        "CREATE VIEW policies_clean AS SELECT policy_id, customer_id, premium FROM policies_raw "
        "GROUP BY policy_id")

def s03_verify(conn) -> dict:
    errs = _rebuild_pipeline(conn, _pipeline(conn))
    gt = get_gt(conn)
    built = errs["policies_clean"] is None
    n = conn.execute("SELECT COUNT(*) FROM policies_clean").fetchone()[0] if built else -1
    dups = conn.execute("SELECT COUNT(*) FROM (SELECT policy_id FROM policies_clean GROUP BY policy_id HAVING COUNT(*)>1)").fetchone()[0] if built else -1
    total = conn.execute("SELECT COALESCE(SUM(premium),0) FROM policies_clean").fetchone()[0] if built else -1
    distinct_ok = conn.execute("SELECT COUNT(DISTINCT policy_id) FROM policies_clean").fetchone()[0] == gt["distinct"] if built else False
    return _result([
        ("pipeline builds", built),
        ("no duplicate policy_id", dups == 0),
        ("row count == distinct policies", n == gt["distinct"]),
        ("all distinct policies retained", distinct_ok),
        ("premium total matches (dedup)", total == gt["total"]),
    ])


# =====================================================================================================
# S10 — Source and finance totals disagree (join fan-out double-counts premiums)
# =====================================================================================================
def s10_build() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE source_premiums (policy_id TEXT, amount INTEGER)")
    amt = _ints(40, 100, 1500)
    for i in range(40):
        conn.execute("INSERT INTO source_premiums VALUES (?,?)", (f"POL{i:04d}", amt[i]))
    conn.execute("CREATE TABLE commissions (policy_id TEXT, agent TEXT)")
    for i in range(40):
        conn.execute("INSERT INTO commissions VALUES (?,?)", (f"POL{i:04d}", "A1"))
        if i % 4 == 0:  # 10 policies have a second agent -> fan-out
            conn.execute("INSERT INTO commissions VALUES (?,?)", (f"POL{i:04d}", "A2"))
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, sql TEXT)")
    # buggy finance transformation: SUM over the join double-counts fan-out policies
    conn.execute("INSERT INTO pipeline VALUES (1,'finance_report',?)",
                 ("CREATE VIEW finance_report AS SELECT SUM(p.amount) AS total "
                  "FROM source_premiums p JOIN commissions c ON p.policy_id=c.policy_id",))
    conn.commit()
    set_gt(conn, {"source_total": sum(amt), "tol": 0})
    return conn

def s10_reference_fix(conn):
    _set_artifact(conn, "finance_report",
        "CREATE VIEW finance_report AS SELECT SUM(amount) AS total FROM source_premiums")

def s10_verify(conn) -> dict:
    errs = _rebuild_pipeline(conn, _pipeline(conn))
    gt = get_gt(conn)
    built = errs["finance_report"] is None
    fin = conn.execute("SELECT total FROM finance_report").fetchone()[0] if built else -1
    src = conn.execute("SELECT SUM(amount) FROM source_premiums").fetchone()[0]
    return _result([
        ("finance pipeline builds", built),
        ("source total unchanged", src == gt["source_total"]),
        ("finance total reconciles to source (within tol)", built and abs(fin - src) <= gt["tol"]),
    ])


# =====================================================================================================
# S14 — Lineage breaks after a table rename (customers -> dim_customer)
# =====================================================================================================
def s14_build() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dim_customer (id TEXT, name TEXT)")   # renamed from 'customers'
    for i in range(20):
        conn.execute("INSERT INTO dim_customer VALUES (?,?)", (f"CUST{i:03d}", f"Customer {i}"))
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, sql TEXT)")
    # downstream artifacts still reference the OLD table name -> broken
    conn.execute("INSERT INTO pipeline VALUES (1,'active_customers',?)",
                 ("CREATE VIEW active_customers AS SELECT id, name FROM customers",))
    conn.execute("INSERT INTO pipeline VALUES (2,'customer_report',?)",
                 ("CREATE VIEW customer_report AS SELECT c.name FROM active_customers c",))
    conn.commit()
    set_gt(conn, {"expected_edges": {"active_customers": ["dim_customer"],
                                   "customer_report": ["active_customers"]}})
    return conn

def s14_reference_fix(conn):
    _set_artifact(conn, "active_customers",
        "CREATE VIEW active_customers AS SELECT id, name FROM dim_customer")

def s14_verify(conn) -> dict:
    arts = _pipeline(conn)
    errs = _rebuild_pipeline(conn, arts)
    all_build = all(v is None for v in errs.values())
    edges = {name: sorted(table_refs(sql)) for name, sql in arts}
    exp = {k: sorted(v) for k, v in get_gt(conn)["expected_edges"].items()}
    edges_ok = edges == exp
    no_old = all("customers" not in refs for refs in edges.values())  # exact old name gone (dim_customer ok)
    return _result([
        ("all downstream views execute", all_build),
        ("lineage edges match expected", edges_ok),
        ("no reference to renamed 'customers'", no_old),
    ])


# =====================================================================================================
# S20 — Massive cross-source consolidation (Northstar USD + Meridian EUR); overload dial = `scale`
# A consolidation task (not a repair): extract every lineage edge + every revenue fact across BOTH
# sources, then reconcile a normalized total. `scale` grows the platform text past the model window, so
# a direct arm (one prompt) overflows while a bounded/runtime arm (grouped extraction + deterministic
# reconcile) holds. Both arms use the SAME deterministic reconcile — only context handling differs.
# =====================================================================================================
MER_FX = 1.1  # EUR -> USD
_COLDICT = "\n".join(f"--   col_{j}: dimension attribute {j} (nullable, indexed, lineage-tracked)" for j in range(22))

def s20_build(scale: int = 60) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, sql TEXT)")
    amts = _ints(scale, 100, 900)
    edges, ns_sum, mer_sum = [], 0, 0
    for i in range(scale):
        src, view = f"src_{i:03d}", f"v_{i:03d}"
        plat = "northstar" if i % 2 == 0 else "meridian"
        cur = "USD" if plat == "northstar" else "EUR"
        if plat == "northstar": ns_sum += amts[i]
        else: mer_sum += amts[i]
        sql = (f"-- platform: {plat} | currency: {cur} | owner: team_{i % 6}\n"
               f"-- description: revenue feed {view} sourced from {src} in the {plat} platform.\n"
               f"-- revenue: {amts[i]} {cur}\n{_COLDICT}\n"
               f"CREATE VIEW {view} AS SELECT customer_id, amount AS revenue FROM {src};")
        conn.execute("INSERT INTO pipeline VALUES (?,?,?)", (i, view, sql))
        edges.append([view, src])
    set_gt(conn, {"edges": edges, "total": ns_sum + round(mer_sum * MER_FX),
                  "scale": scale, "fx": MER_FX})
    conn.commit()
    return conn

def s20_artifacts(conn) -> list[tuple[str, str]]:
    return [(r[1], r[2]) for r in conn.execute("SELECT ord, name, sql FROM pipeline ORDER BY ord")]

def s20_reconcile(facts: list) -> tuple[list, int]:
    """Deterministic reconcile operator (shared by both arms): edges + normalized USD total."""
    edges, total = [], 0
    for f in facts:
        try:
            view, source, amount, cur = f[0], f[1], int(f[2]), str(f[3]).upper()
        except Exception:
            continue
        edges.append([view, source])
        total += amount if cur == "USD" else round(amount * MER_FX)
    return edges, total

def s20_verify(conn, facts: list, completed: bool) -> dict:
    gt = get_gt(conn)
    gt_edges = {tuple(e) for e in gt["edges"]}
    edges, total = s20_reconcile(facts)
    got = {tuple(e) for e in edges}
    recall = len(got & gt_edges) / len(gt_edges) if gt_edges else 0.0
    tol = max(2, round(gt["total"] * 0.005))
    return _result([
        ("completed (no context overflow)", completed),
        ("lineage recall == 1.0", recall == 1.0),
        (f"reconciled total within tol of {gt['total']}", abs(total - gt["total"]) <= tol),
    ]) | {"recall": round(recall, 3), "total": total, "gt_total": gt["total"]}


# =====================================================================================================
# S14L — Lineage-break REPAIR at scale: one table renamed, `broken_n` of `scale` views still reference the
# old name, hidden among the rest. The task is defect LOCALIZATION (find every broken view). A direct arm
# can only see a window of the platform; the runtime's deterministic lineage scan finds all at any scale.
# =====================================================================================================
_COLDICT2 = "\n".join(f"--   col_{j}: report attribute {j} (nullable, lineage-tracked)" for j in range(22))

def s14l_build(scale: int = 60, broken_n: int | None = None) -> sqlite3.Connection:
    if broken_n is None: broken_n = max(2, scale // 10)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dim_customer (id TEXT, name TEXT)")   # renamed from 'customers'
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, sql TEXT)")
    broken = []
    # broken views are spread across the platform (not all at the front), so a window can't catch them all
    step = max(1, scale // broken_n)
    for i in range(scale):
        view = f"rep_{i:04d}"
        is_broken = (i % step == 0) and len(broken) < broken_n
        src = "customers" if is_broken else "dim_customer"
        if is_broken: broken.append(view)
        conn.execute("INSERT INTO pipeline VALUES (?,?,?)",
                     (i, view, f"-- report view {view}\n{_COLDICT2}\nCREATE VIEW {view} AS SELECT id, name FROM {src};"))
    set_gt(conn, {"broken": sorted(broken), "renamed_old": "customers", "renamed_new": "dim_customer", "scale": scale})
    conn.commit()
    return conn

def s14l_known_tables(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name!='pipeline' AND name NOT LIKE '\\_%' ESCAPE '\\'")}

def s14l_localize_verify(conn, found_broken) -> dict:
    gtb = set(get_gt(conn)["broken"]); fb = set(found_broken)
    return {"recall": round(len(fb & gtb) / len(gtb), 3) if gtb else 1.0,
            "precision": round(len(fb & gtb) / len(fb), 3) if fb else 1.0,
            "broken_gt": len(gtb), "found": len(fb)}


# =====================================================================================================
# S21 — Cross-source reconciliation-CONFLICT detection (the acquisition problem). Operating a platform
# with an acquired source (Northstar + Meridian) is not "can you consolidate?" (S20) but "do you surface
# every reconciliation conflict the merge introduces?" — the same logical metric reported with different
# values across the two source systems (different currency / definition, not a benign FX conversion).
# `conflicts` metric pairs disagree on purpose, hidden among `scale` single-source metrics. The two source
# systems are laid out as separate blocks (as on Spark: Northstar modules on some executors, Meridian on
# others), so a conflict NEVER lives inside one bounded working set — it only surfaces at the cross-source
# union. The distinction is the union KEY: a value-keyed union (S20-style) merges a disagreement silently
# as two benign facts; a metric-keyed contradiction detector flags same-metric/different-value as a
# CONFLICT. Deterministic gate (recall/precision), no model required.
# =====================================================================================================
_METRIC = re.compile(r"-- metric:\s*(\w+)\s*=\s*(\d+\s+\w+)")

def s21_build(scale: int = 60, conflicts: int | None = None) -> sqlite3.Connection:
    if conflicts is None: conflicts = max(2, scale // 20)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE pipeline (ord INTEGER, name TEXT, source TEXT, sql TEXT)")
    amts = _ints(scale, 1000, 9000)
    ord_ = 0
    # Northstar block: (scale - conflicts) reconciled single-source metrics + the northstar side of each conflict
    for i in range(scale - conflicts):
        v = f"ns_rev_{i:04d}"
        conn.execute("INSERT INTO pipeline VALUES (?,?,?,?)", (ord_, v, "northstar",
            f"-- owner: team_{i % 6}\n{_COLDICT}\nCREATE VIEW {v} AS SELECT amount FROM northstar_src_{i};\n"
            f"-- metric: m_{i} = {amts[i]} USD")); ord_ += 1
    for j in range(conflicts):
        v = f"ns_c_{j:04d}"
        conn.execute("INSERT INTO pipeline VALUES (?,?,?,?)", (ord_, v, "northstar",
            f"-- owner: team_{j % 6}\n{_COLDICT}\nCREATE VIEW {v} AS SELECT amount FROM northstar_src;\n"
            f"-- metric: cm_{j} = {1200 + j} USD")); ord_ += 1
    # Meridian block: the acquired side of each conflict — SAME metric key cm_j, different value + currency
    for j in range(conflicts):
        v = f"md_c_{j:04d}"
        conn.execute("INSERT INTO pipeline VALUES (?,?,?,?)", (ord_, v, "meridian",
            f"-- owner: acq_team_{j % 4}\n{_COLDICT}\nCREATE VIEW {v} AS SELECT total FROM meridian_src;\n"
            f"-- metric: cm_{j} = {1080 + j} EUR")); ord_ += 1
    set_gt(conn, {"conflicts": [f"cm_{j}" for j in range(conflicts)], "scale": scale, "n_conflicts": conflicts})
    conn.commit()
    return conn

def s21_artifacts(conn) -> list[tuple[str, str, str]]:
    """[(name, source_system, sql)] in platform order — Northstar block then Meridian block."""
    return [(r[0], r[1], r[2]) for r in conn.execute("SELECT name, source, sql FROM pipeline ORDER BY ord")]

def s21_metrics(text: str) -> dict[str, str]:
    """Deterministic keyed extraction (shared by both arms): {logical_metric: 'value currency'}."""
    return {m.group(1): m.group(2) for m in _METRIC.finditer(text)}

def s21_detect(chunk_maps: list[dict[str, str]], keyed: bool) -> set[str]:
    """Union bounded per-chunk facts into a store, then flag any store key carrying >1 distinct value.
    Both arms run the SAME detection — only the store KEY differs (this is the whole point):
      keyed=False — value-keyed union (S20-style): the key is 'metric|value', so a disagreement splits into
                    two DIFFERENT keys (each with one value). No key ever carries >1 value -> 0 conflicts:
                    the contradiction reconciled silently.
      keyed=True  — metric-keyed contradiction detector (merge()): the key is the logical metric, so the two
                    source values collide under one key -> flagged as a CONFLICT."""
    store: dict[str, set[str]] = {}
    for cm in chunk_maps:
        for metric, value in cm.items():
            k = metric if keyed else f"{metric}|{value}"
            store.setdefault(k, set()).add(value)
    flagged = {k for k, vs in store.items() if len(vs) > 1}
    return flagged if keyed else {k.split("|", 1)[0] for k in flagged}  # naive: none survive the >1 test

def s21_verify(conn, found_conflicts, completed: bool) -> dict:
    gt = set(get_gt(conn)["conflicts"]); fc = set(found_conflicts)
    recall = len(fc & gt) / len(gt) if gt else 1.0
    precision = len(fc & gt) / len(fc) if fc else 1.0
    return _result([
        ("completed (no context overflow)", completed),
        ("conflict recall == 1.0", recall == 1.0),
        ("conflict precision == 1.0", precision == 1.0),
    ]) | {"recall": round(recall, 3), "precision": round(precision, 3),
          "conflicts_gt": len(gt), "found": len(fc)}


# --- registry + gate machinery -------------------------------------------------------------------------
def _result(checks: list[tuple[str, bool]]) -> dict:
    return {"passed": all(ok for _, ok in checks), "checks": checks}

SCENARIOS = {
    "S03": dict(title="Duplicate policy records after a retried load",
                defect="A retried load re-inserted 8 policy rows; counts and premium totals are inflated.",
                symptom="policies_clean returns more rows than there are distinct policy_id values, and the premium total is higher than expected.",
                build=s03_build, verify=s03_verify, fix=s03_reference_fix,
                gates=["no duplicate policy_id", "distinct-policy count", "premium total after dedup"]),
    "S10": dict(title="Source and finance totals disagree",
                defect="finance_report SUMs over a commissions join that fans out; totals double-count.",
                symptom="finance_report.total does not equal SUM(source_premiums.amount); finance is higher.",
                build=s10_build, verify=s10_verify, fix=s10_reference_fix,
                gates=["source total unchanged", "finance reconciles to source within tolerance"]),
    "S14": dict(title="Lineage breaks after a table rename",
                defect="'customers' was renamed to 'dim_customer'; downstream views still reference the old name.",
                symptom="Rebuilding the downstream views fails with a 'no such table' error.",
                build=s14_build, verify=s14_verify, fix=s14_reference_fix,
                gates=["downstream views execute", "lineage edges restored", "no stale reference"]),
}

def validate() -> int:
    print("DataOpsBench v0 — gate validation (no model calls)\n")
    ok_all = True; n_ok = 0
    for sid, s in SCENARIOS.items():
        conn = s["build"]()
        pre = s["verify"](conn)                 # defect must be visible: gates fail before the fix
        s["fix"](conn)
        post = s["verify"](conn)                # reference fix must pass every gate
        discriminates = (not pre["passed"]) and post["passed"]
        ok_all &= discriminates; n_ok += int(discriminates)
        print(f"{sid}  {s['title']}")
        print(f"     defect present (pre-fix gates fail): {not pre['passed']}")
        for name, c in post["checks"]:
            print(f"       [{'PASS' if c else 'FAIL'}] {name}")
        print(f"     -> {'OK (gates discriminate defect vs fix)' if discriminates else 'BROKEN'}\n")
    # S20 overload-consolidation gate — deterministic self-check (no model)
    c = s20_build(8)
    perfect = [[n, re.search(r"FROM (\w+)", s).group(1),
                int(re.search(r"-- revenue: (\d+)", s).group(1)),
                re.search(r"-- revenue: \d+ (\w+)", s).group(1)] for n, s in s20_artifacts(c)]
    s20_ok = (not s20_verify(c, [], False)["passed"]) and s20_verify(c, perfect, True)["passed"]
    ok_all &= s20_ok; n_ok += int(s20_ok)
    print("S20  Massive cross-source consolidation (overload gate)  ->  "
          + ("OK (gate discriminates: empty extraction fails, perfect passes)" if s20_ok else "BROKEN") + "\n")
    # S21 reconciliation-conflict gate — deterministic self-check (no model): the metric-keyed detector must
    # surface every seeded cross-source conflict at precision 1.0; the value-keyed union must surface none.
    c21 = s21_build(60, 5)
    maps = [s21_metrics(sql) for _, _, sql in s21_artifacts(c21)]
    runtime = s21_verify(c21, s21_detect(maps, keyed=True), True)
    naive = s21_verify(c21, s21_detect(maps, keyed=False), True)
    clean = s21_verify(s21_build(60, 0), s21_detect([s21_metrics(s) for _, _, s in s21_artifacts(s21_build(60, 0))], keyed=True), True)
    s21_ok = runtime["passed"] and (not naive["passed"]) and naive["recall"] == 0.0 and clean["passed"]
    ok_all &= s21_ok; n_ok += int(s21_ok)
    print("S21  Cross-source reconciliation-conflict detection (acquisition gate)  ->  "
          + (f"OK (metric-keyed merge recall {runtime['recall']}/prec {runtime['precision']}; "
             f"value-keyed union recall {naive['recall']}; clean-platform precision {clean['precision']})"
             if s21_ok else "BROKEN") + "\n")
    print(f"RESULT {n_ok}/{len(SCENARIOS) + 2} scenarios validate")
    return 0 if ok_all else 1

def spec(which: str | None):
    for sid, s in SCENARIOS.items():
        if which and sid != which:
            continue
        print(json.dumps({"id": sid, "title": s["title"], "defect": s["defect"],
                          "deterministic_gates": s["gates"]}, indent=2))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if cmd == "validate":
        sys.exit(validate())
    elif cmd == "spec":
        spec(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__); sys.exit(2)
