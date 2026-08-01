# DataOpsBench

A standardized, model- and framework-agnostic workload for **AI-assisted data operations** — the DataOps
analogue of TPC-C. It doesn't ask "can an LLM write SQL?" It asks:

> Can an AI system **diagnose, repair, and verify a production data platform** — preserving correctness,
> lineage, evidence, and governance — when the relevant code and metadata no longer fit in one model
> context window?

Every scenario ships a **seeded defect, hidden ground truth, and a deterministic verifier**. Any solver —
a direct model, LangGraph, OpenHands, Claude Code, Codex, Cursor, or your own runtime — is scored on the
**identical** scenarios by the **same** deterministic gates. Two reference solvers are included so the
workload runs out of the box.

Background: [why bounded integration becomes *required* past the context window](https://redevops.io/blog/does-formal-orchestration-beat-a-supervisor)
· [the DataOpsBench results write-up](https://redevops.io/blog/dataopsbench-operating-a-data-platform).

## Validate the gates (no API keys, no model calls)

```bash
python3 dataopsbench.py validate      # RESULT 4/4 — each gate FAILS on the defect and PASSES on the fix
python3 dataopsbench.py spec [S03]    # scenario spec(s)
```

`validate` is the integrity check: it proves every scenario's gates **discriminate** — the seeded defect
fails them, the reference fix passes them all. A gate that passed on the defect would be worthless.

## Scenarios (v0)

| id | family | task | deterministic gates |
|----|--------|------|---------------------|
| **S03** | data quality | duplicate policy records after a retried load | no dup key · distinct count · premium total after dedup |
| **S10** | reconciliation | source vs. finance totals disagree (join fan-out) | source total unchanged · finance reconciles within tolerance |
| **S14** | lineage | lineage breaks after a table rename | downstream views execute · lineage edges restored |
| **S14L** | repair @ scale | one table renamed, broken refs hidden across `scale` pipelines — localize them all | localization recall / precision |
| **S20** | consolidation @ scale | consolidate cross-source (USD + EUR) lineage + reconcile a normalized total; `scale` grows past the window | lineage recall 1.0 · reconciled total within tolerance |
| **S21** | reconciliation-conflict @ scale | an acquired source (Northstar + Meridian) reports the *same* logical metric with *different* values; detect every cross-source conflict, hidden among `scale` clean metrics | conflict recall 1.0 · precision 1.0 · no overflow |

Platforms are **fully synthetic** (deterministic generators — reproducible ground truth, no external data).
`scale` on S14L/S20/S21 is the overload dial: it grows the platform text past a model's context window.

## Run the reference solvers

Point at any OpenAI-compatible endpoint (no keys are stored here — read from the environment):

```bash
export QWEN_URL=http://localhost:8000 QWEN_MODEL=your-model     # the arm model
python3 run_arm.py                        # repair scenarios S03/S10/S14 (Arm A, direct)
python3 run_arm.py s20  20 60 120         # consolidation under overload: direct vs bounded reference
python3 run_arm.py s14l 20 60 120         # localization at scale: direct vs deterministic reference
python3 run_arm.py s21  20 100 300 600    # reconciliation-conflict: direct vs naive-union vs metric-keyed merge

# judge-graded family (report quality). arms + a DIFFERENT-family judge, blinded pairwise:
export JUDGE_URL=http://localhost:8001 JUDGE_MODEL=your-judge-model
python3 run_judge.py 1 2 3 4
```

**Arm A** is a direct model over the raw platform. **Arm B** is a bounded reference (chunk under a token
budget; for consolidation a bounded model pass, for localization a deterministic scan). The observed
pattern: the direct arm succeeds while the platform fits the window, then overflows; a bounded solver stays
feasible at any scale.

## Plug in your own solver (the arm interface)

A solver receives a scenario and returns a result; scoring calls the scenario's verifier on it — identical
for every solver:

- **repair** (S03/S10/S14): return `{artifact_name: corrected_sql}`; scored by `<scenario>_verify(conn)`.
- **consolidation** (S20): return `facts = [[view, source, amount, currency], ...]`; scored by `s20_verify`.
- **reconciliation-conflict** (S21): return the list of metric names in conflict across sources; scored by `s21_verify`. A value-keyed union reconciles disagreements silently (recall 0); only a metric-keyed contradiction detector surfaces them — the same working set on each side, only the union key differs.
- **localization** (S14L): return the list of broken view names; scored by `s14l_localize_verify`.
- **report** (judge family): return a markdown incident report; scored by a deterministic groundedness gate
  first, then a blinded pairwise judge from a *different* model family.

`dataopsbench.py` exposes `build`, `verify`, ground truth, and the pipeline artifacts for each scenario.

## Design principles

- **Deterministic gates first**, a blinded judge only for the subjective residual (explanations/docs), and
  always secondary — grounded in ground truth, order-randomized to cancel position bias.
- **One variable.** Where arms are compared, they get identical tools and budgets and differ only in
  orchestration/memory/planning/retrieval — never in raw capability.
- **Mechanism test, not a leaderboard.** It measures *whether* and *how* a system keeps operating a
  platform past the context boundary, not a universal ranking of agents.

## Contributing

New scenarios are welcome — each needs a synthetic builder, hidden ground truth, and a **deterministic**
verifier that `validate` can show *discriminates* (fails on the defect, passes on the fix). Keep data
synthetic and reproducible; no customer data.

## License

Apache-2.0. Maintained by [redevops.io](https://redevops.io).
