# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Check that what the YAML files describe is in place, and gate on it (forge-aig-create-model-service module 04).

    uv run <skill>/scripts/evaluate.py --state <workdir>/aig-model-services.json

Three rates, each of which must reach its threshold:
- config_match_rate: the services that match model-services.yaml and service-policies.yaml, from deploy_services.py's
  read-only plan. Re-attaching a SQL policy isn't a difference.
- answer_rate: the services that answer `test_prompt` through the gateway's chat completions API. A reply blocked by a
  service policy counts: the service works, and the policy did its job. A policy that fails to evaluate doesn't.
  Rate limits and a service still settling are retried.
- grant_rate: the grants in permissions.yaml that are in effect, from grant_access.py's read-only plan.
A rate whose part isn't in `parts` is left out. Prints a JSON report and exits 1 when a rate is below its threshold.
Each test prompt is billed like any request, plus one evaluator call per LLM-based policy on the service.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

THRESHOLDS = {"config_match_rate": 1.0, "answer_rate": 1.0, "grant_rate": 1.0}
SCRIPTS = Path(__file__).resolve().parent
GATEWAY = "/ai-gateway/mlflow/v1/chat/completions"
TEST_PROMPT = "What is Databricks? Answer in one sentence."
RETRIED = {"TooManyRequests", "TemporarilyUnavailable", "InternalError", "DeadlineExceeded", "NotFound"}
FAILED_POLICY = re.compile(r"(Input|Output) policy '([^']+)' failed to evaluate", re.I)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/aig-model-services.json")
    return p.parse_args()


def run_plan(script: str, state: Path) -> dict:
    """A plan script's JSON output. Run as a separate process: scripts don't import each other."""
    result = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--state", str(state)],
                            capture_output=True, text=True, check=False)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"errors": [(result.stderr.strip() or f"{script} printed no plan")[-800:]]}


def ask(client, service: str, prompt: str, attempts: int = 6, sleep=time.sleep) -> dict:
    """{service, answered, blocked_by, reason, error} for one prompt through the gateway."""
    out = {"service": service, "answered": False, "blocked_by": None, "reason": None, "error": None}
    body = {"model": service, "messages": [{"role": "user", "content": prompt}], "max_tokens": 32}
    for attempt in range(attempts):
        try:
            response = client.do("POST", GATEWAY, body=body)
        except Exception as e:  # noqa: BLE001 - every failure is a recorded outcome
            out["error"] = f"{type(e).__name__}: {e}"[:500]
            if FAILED_POLICY.search(str(e)):  # fail closed: the evaluator didn't reach a verdict
                out["blocked_by"] = FAILED_POLICY.search(str(e)).group(2)
                out["reason"] = "the policy failed to evaluate, so the gateway denied the request (fail closed)"
                return out
            if type(e).__name__ not in RETRIED or attempt == attempts - 1:
                return out
            sleep(min(10 * 2 ** attempt, 60))
            continue
        policy = response.get("databricks_service_policy") or {}
        out.update(answered=True, error=None, blocked_by=policy.get("name"), reason=policy.get("reason"))
        return out
    return out


def rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def score(state: dict, services_plan: dict | None, grants_plan: dict | None, answers: list[dict]) -> dict:
    metrics, details = {}, {"errors": [], "not_matching": [], "not_answering": [], "grants_not_in_effect": []}
    if services_plan is not None:
        services = services_plan.get("services") or []
        details["errors"] += services_plan.get("errors") or []
        metrics["config_match_rate"] = 0.0 if services_plan.get("errors") else rate(
            sum(bool(s.get("matches")) for s in services), len(services))
        details["not_matching"] = [{"service": s["name"], "action": s.get("action"), "changes": s.get("changes"),
                                    "policies": s.get("policy_steps")} for s in services if not s.get("matches")]
        metrics["answer_rate"] = rate(sum(a["answered"] for a in answers), len(answers))
        details["not_answering"] = [a for a in answers if not a["answered"]]
    if grants_plan is not None:
        grants = grants_plan.get("grants") or []
        details["errors"] += grants_plan.get("errors") or []
        metrics["grant_rate"] = 0.0 if grants_plan.get("errors") else rate(
            sum(g.get("status") == "ok" for g in grants), len(grants))
        details["grants_not_in_effect"] = [g for g in grants if g.get("status") != "ok"]
    failed = sorted(k for k, v in metrics.items() if v is None or v < THRESHOLDS[k])
    return {"metrics": metrics, "thresholds": {k: THRESHOLDS[k] for k in metrics}, "passed": not failed,
            "below_threshold": failed, "answers": answers, **details}


def main() -> None:
    args = parse_args()
    if not args.state.is_file():
        sys.exit(f"{args.state} not found. Module 00 writes it; run preflight first.")
    state = json.loads(args.state.read_text())
    parts = state.get("parts") or []
    services_plan = run_plan("deploy_services.py", args.state) if "model_services" in parts else None
    grants_plan = run_plan("grant_access.py", args.state) if "grants" in parts else None
    answers = []
    if services_plan and not services_plan.get("errors"):
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient(profile=state["profile"]).api_client
        prompt = state.get("test_prompt") or TEST_PROMPT
        answers = [ask(client, s["name"], prompt) for s in services_plan.get("services") or []]
    report = score(state, services_plan, grants_plan, answers)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
