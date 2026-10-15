# Deterministic evaluation fixtures

`plan.json` contains six local coding tasks. Each task starts from a fresh copy of its `baseline/` directory. Public tests are in that directory; holdout assertions live only in the case manifest and are mounted after the agent terminates. `scripts/` are fixed action sequences for testing the harness, not model evaluations. Keep this entire fixture set outside the agent workspace for every run.

Run with `uv run agent-harness eval-batch --plan benchmarks/plan.json --script-map benchmarks/scripts/map.json --traces .runs --output .runs/experiments/local-fixtures-1` after building the documented Docker image. Choose a new output path for each repeat. Then run `uv run agent-harness experiment-verify .runs/experiments/local-fixtures-1`.

`Dockerfile` pins the Python base image by registry digest. Build it with `docker build --provenance=false -t coding-agent-sandbox:1 -f benchmarks/Dockerfile .`; disabling build provenance avoids a changing attestation manifest in the local image ID. The evaluator resolves the tag to a local ID before starting a batch. To move the image offline, run `docker save -o .exports/coding-agent-sandbox.tar coding-agent-sandbox:1`, record its SHA-256 separately, then use `docker load -i` on the destination. The experiment ZIP contains traces and results, not the Docker image or task baselines.
