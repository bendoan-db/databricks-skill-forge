# /// script
# requires-python = ">=3.11"
# dependencies = ["mlflow[databricks]>=3.1", "databricks-sdk>=0.40"]
# ///
"""Score what <skill-name> built against its thresholds (module <evaluate-module>), and log the run to MLflow.

    uv run <skill>/scripts/evaluate.py --state <workdir>/<state-file>

Reads the resolved values from <state-file>, measures the result, and logs the metrics to the MLflow experiment
`eval_experiment` in that file. Exits 1 when a threshold is missed, so CI can gate on it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

THRESHOLDS = {}  # metric: floor; evals/scenarios.yaml defaults.thresholds must match


def measure(state: dict, w) -> dict[str, float]:
    """Each metric in THRESHOLDS, measured on what the skill built. `w` is a WorkspaceClient."""
    # smith: measure the result, ported from the source's own evaluation, or with the method module 02 chose
    raise NotImplementedError("measure() isn't written yet")


def misses(metrics: dict[str, float]) -> list[str]:
    return [f"{name} not measured" if name not in metrics else f"{name}={metrics[name]:.3f} < {floor}"
            for name, floor in THRESHOLDS.items() if name not in metrics or metrics[name] < floor]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", type=Path, required=True, help="The skill's state file")
    args = p.parse_args()
    state = json.loads(args.state.read_text())
    if not state.get("eval_experiment"):
        raise SystemExit(f"{args.state.name}: eval_experiment is missing; record it before evaluating")
    if state.get("profile"):
        os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", state["profile"])

    import mlflow
    from databricks.sdk import WorkspaceClient

    metrics = measure(state, WorkspaceClient(profile=state.get("profile")))
    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(state["eval_experiment"])
    with mlflow.start_run(run_name="evaluate") as run:
        mlflow.log_metrics(metrics)
    print(json.dumps({"run_id": run.info.run_id, "metrics": metrics}))
    if failed := misses(metrics):
        print("Below threshold: " + ", ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
