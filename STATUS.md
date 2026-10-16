# Status

- **Maturity:** internally reproduced
- **Version:** v0 scenarios; no tags, releases or package version
- **Last git activity on the default branch:** 2026-09-25 (from `git log -1 --format=%cs`; the working clone is shallow)

## What is supported
- python3 dataopsbench.py validate (deterministic, no API keys): RESULT 5/5 scenarios validate (results/validate.txt)
- S21 sweep results committed in results/s21.txt (single run, one 64K-context model)
- Reference solvers via run_arm.py / run_judge.py against your own OpenAI-compatible endpoint

## What is not supported / not claimed
- Not a standard or audited benchmark; no standards body, spec versioning or leaderboard
- No committed results for S03, S10, S14, S14L, S20 beyond gate validation
- No external reproduction; synthetic data only

## Known issues
- README previously said 4/4 while results/validate.txt says 5/5; fixed (S14L is not covered by validate)
- Statements about behaviour at larger scales than tested (s21: 600 arm sweep, 2000 Spark) are not evidenced

No quantitative claim in this repo has been externally reproduced or field validated.
