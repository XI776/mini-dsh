# Evaluation fixtures

## Deterministic harness fixtures

`plan.json` contains six local coding tasks. Each task starts from a fresh copy of its `baseline/` directory. Public tests are in that directory; holdout assertions live only in the case manifest and are mounted after the agent terminates. `scripts/` are fixed action sequences for testing the harness, not model evaluations. Keep this entire fixture set outside the agent workspace for every run.

Run with `uv run agent-harness eval-batch --plan benchmarks/plan.json --script-map benchmarks/scripts/map.json --traces .runs --output .runs/experiments/local-fixtures-1` after building the documented Docker image. Choose a new output path for each repeat. Then run `uv run agent-harness experiment-verify .runs/experiments/local-fixtures-1`.

These tasks are deliberately tiny (one-file baselines, six to eight steps, 6,000 tokens). They exercise the evaluation chain; they cannot separate context strategies, because the full history never approaches the budget.

## Real-model context tasks

`real/` holds six tasks built for the context ablation, planned by `real-context-plan.json` (five repetitions) with `pilot-plan.json` as the small first batch. Each task is a small repository with its own README stating the contract, a failing public test suite, and a holdout suite that only passes when every documented rule is implemented.

| Case | Baseline shape | Initial public failures |
| --- | --- | --- |
| `duration.json` | one small module, parser edge cases | concatenated segments, whitespace |
| `inventory.json` | six-file package, per-line tier rules | tier aggregation |
| `pipeline.json` | four-stage pipeline in one module | partial chunk, dedupe order |
| `textrefactor.json` | helper plus three call sites | `keep_case` keyword missing |
| `config_migration.json` | config, defaults and client modules | base URL normalization |
| `logtriage.json` | 33 KB event log plus a stub module | unimplemented summary |

All six real cases share one budget: 50 steps, 200,000 tokens, 100 tool calls and 1,800 seconds. The first calibration batch used smaller, per-case limits, and six of twelve runs ended at `tool_calls`. In the 108-run formal batch, 35 runs still ended at a budget limit. Report budget exhaustion alongside Token and cost figures; totals from truncated runs do not isolate the effect of a Context strategy. Runs from the earlier calibration batch stay in `.runs/experiments/` as superseded artifacts.

`real/<task>/reference/` contains the reference fix for each task. It is never copied into an agent workspace: the evaluator copies only `real/<task>/baseline/`. `tests/test_real_tasks.py` uses the reference to assert, in CI, that every baseline starts red, that the reference turns public and holdout tests green, and that holdout content does not appear in the baseline.

`real/logtriage/generate_log.py` regenerates the checked-in log fixture deterministically; the holdout counts in `real/logtriage.json` must be updated together with it.

## Image

`Dockerfile` pins the Python base image by registry digest. Build it with `docker build --provenance=false -t coding-agent-sandbox:1 -f benchmarks/Dockerfile .`; disabling build provenance avoids a changing attestation manifest in the local image ID. The evaluator resolves the tag to a local ID before starting a batch. To move the image offline, run `docker save -o .exports/coding-agent-sandbox.tar coding-agent-sandbox:1`, record its SHA-256 separately, then use `docker load -i` on the destination. The experiment ZIP contains traces and results, not the Docker image or task baselines.
