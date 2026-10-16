# Contributing

- Open a GitHub Issue first for anything non-trivial: https://github.com/redevops-io/DataOpsBench/issues
- Pull requests against the default branch are welcome. Keep changes small and focused.
- New scenarios need a synthetic builder, hidden ground truth and a deterministic verifier (see README "Contributing").
- Run the checks before submitting: `python3 dataopsbench.py validate`
- Documentation changes: do not add numbers or performance/accuracy claims without a reproducible artifact in the repo, or label them
  `Illustrative/synthetic`, `Internally reproduced`, `Externally reproduced` or `Field validated` as appropriate.
- Maintainer contact for anything else: redevops@redevops.io.
