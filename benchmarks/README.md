# Deterministic evaluation fixtures

`plan.json` contains six local coding tasks. Each task starts from a fresh copy of its `baseline/` directory. Public tests are in that directory; holdout assertions live only in the case manifest and are mounted after the agent terminates. `scripts/` are fixed action sequences for testing the harness, not model evaluations. Keep this entire fixture set outside the agent workspace for every run.

Run with `uv run agent-harness eval-batch --plan benchmarks/plan.json --script-map benchmarks/scripts/map.json --traces .runs --output .runs/experiments/local-fixtures-1` after building the documented Docker image. Choose a new output path for each repeat. Then run `uv run agent-harness experiment-verify .runs/experiments/local-fixtures-1`.
