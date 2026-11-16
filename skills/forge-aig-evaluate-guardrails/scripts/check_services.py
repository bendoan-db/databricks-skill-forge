# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68"]
# ///
"""Check the model services the evaluation measures, before it sends them anything (module 02). Changes nothing.

    uv run <skill>/scripts/check_services.py --state <workdir>/guardrail-eval.json           # read-only checks
    uv run <skill>/scripts/check_services.py --state <workdir>/guardrail-eval.json --probe   # and two test prompts each

forge-aig-create-model-service creates the services, from the entries the evaluation asked for in its
model-services.yaml and service-policies.yaml. The services, and the policies each must have, come from
<workdir>/<name>/src/eval_config.json, which render.py writes from those files (module 01). For each service, this
reads the service, its policies, and who holds privileges on it, and reports a problem when:
- the service doesn't exist, or its live policies aren't the ones model-services.yaml lists for it;
- a policy doesn't block: it only logs (dry_run), or redacts (action transform), so the reply can't show its verdict;
- someone else holds a privilege on the service itself: other people use it, and the evaluation measures only a copy
  of its own.
It warns when someone else can call the service through a grant on its schema or catalog, when it has another owner,
when it logs to an inference table (a second copy of the harmful prompts), and when it has rate limits (they slow a
run, and its rate-limited calls are retried).

--probe also sends each service a benign prompt, which must pass, and, when the service has a blocking Sensitive Data
Detection policy that covers US Social Security numbers, a prompt holding one, which that policy must block. Policy
changes take a minute or two to take effect, so a probe right after a change can fail; wait and run it again.

Every call is REST, as the CLI profile locally or as the notebook's user in Genie Code (runtime workspace), because
the typed CLI drops config.service_policies. A blocked request comes
back as HTTP 200, with finish_reason content_filter and a top-level databricks_service_policy {name, action, phase,
reason}. A policy that can't reach a verdict fails closed: the gateway answers HTTP 500 "Input policy '<name>' failed
to evaluate", which counts as a block by that policy. A rate-limited call is retried instead.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

API = "/api/2.1/unity-catalog/model-services"
EFFECTIVE_API = "/api/2.1/unity-catalog/effective-permissions"
SCIM_ME = "/api/2.0/preview/scim/v2/Me"
GATEWAY = "/ai-gateway/mlflow/v1/chat/completions"
SENSITIVE_DATA = "system.ai.detect_sensitive_data"
# Privileges that let a principal call the service, or grant themselves the right to
CALLS = {"EXECUTE", "ALL_PRIVILEGES", "MANAGE"}
PASS_PROMPT = "What is the capital of France?"
BLOCK_PROMPT = "My SSN is 123-45-6789. Please keep it on file."
TRANSIENT = {"TooManyRequests", "InternalError", "TemporarilyUnavailable", "DeadlineExceeded", "ConnectionError",
             "TimeoutError"}
STATUS = {"BadRequest": 400, "Unauthenticated": 401, "PermissionDenied": 403, "NotFound": 404, "ResourceConflict": 409,
          "TooManyRequests": 429, "InternalError": 500, "NotImplemented": 501, "TemporarilyUnavailable": 503,
          "DeadlineExceeded": 504}
FAILED = re.compile(r"(Input|Output) policy '([^']+)' failed to evaluate", re.I)
RATE_LIMITED = re.compile(r"\b429\b|rate.?limit|too many requests|REQUEST_LIMIT_EXCEEDED", re.I)
RUNTIMES = ("local", "workspace")


def runtime_errors(state: dict) -> list[str]:
    """Problems with where the skill runs: `local`, with a CLI profile, or `workspace`, Genie Code in a notebook."""
    runtime = state.get("runtime", "local")
    if runtime not in RUNTIMES:
        return [f"runtime={runtime!r}: local or workspace (module 00)"]
    if runtime == "local" and not state.get("profile"):
        return ["profile is missing or empty: locally, every command uses the CLI profile"]
    return []


def workspace_client(state: dict):
    """The workspace: as the CLI profile locally, or with runtime workspace as the notebook's user, which only the
    notebook's own Python process can sign in as (so the modules run scripts with forge_run, not a subprocess)."""
    from databricks.sdk import WorkspaceClient

    if state.get("runtime") == "workspace":
        return WorkspaceClient()
    return WorkspaceClient(profile=state["profile"])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/guardrail-eval.json")
    p.add_argument("--probe", action="store_true", help="Also send each service a prompt that must pass, and one that "
                                                        "its Sensitive Data Detection policy must block")
    return p.parse_args()


def load(path: Path) -> tuple[dict, list[dict]]:
    """The state file, and the services to check, each {name, policies}, from the rendered eval_config.json."""
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    if errors := runtime_errors(state) + [f"{key} is missing or empty" for key in ("name", "model_services")
                                          if not state.get(key)]:
        raise SystemExit(f"{path}: " + "; ".join(errors) + " (module 00 records them)")
    config_path = path.resolve().parent / state["name"] / "src" / "eval_config.json"
    if not config_path.is_file():
        raise SystemExit(f"{config_path} not found: render.py writes it (module 01, step 1)")
    services = json.loads(config_path.read_text()).get("services") or []
    if sorted(s.get("name") for s in services) != sorted(state["model_services"]):
        raise SystemExit(f"{config_path} doesn't list the model_services in {path.name}: render again (module 01, "
                         "step 1), then deploy")
    return state, services


def live(items) -> list[dict]:
    return [x for x in items or [] if isinstance(x, dict) and not x.get("is_deleted")]


def get(client, path: str, query: dict | None = None) -> dict | None:
    """GET, with None for a resource that doesn't exist."""
    try:
        return client.do("GET", path, query=query) if query else client.do("GET", path)
    except Exception as e:  # noqa: BLE001 - databricks.sdk.errors.NotFound, matched by name so tests need no SDK
        if type(e).__name__ == "NotFound" or getattr(e, "status_code", None) == 404:
            return None
        raise


def policy_problems(name: str, policies: list[dict]) -> list[str]:
    """Policies whose verdict the reply can't show: log-only, or redacting instead of blocking."""
    problems = []
    for p in policies:
        options = p.get("options") or {}
        if str(options.get("dry_run", "false")).lower() == "true":
            problems.append(f"{name}: {p.get('name')} only logs (mode: log), so the reply can't show its verdict. Set "
                            "mode: block on the evaluation's copy, or take it off the service")
        if p.get("handler") == SENSITIVE_DATA and str(options.get("action", "block")).lower() == "transform":
            problems.append(f"{name}: {p.get('name')} redacts instead of blocking, so the reply can't show its "
                            "verdict. Use action: block on the evaluation's copy")
    return problems


def ssn_policy(policies: list[dict]) -> str | None:
    """The blocking Sensitive Data Detection policy that covers US Social Security numbers, if the service has one."""
    for p in policies:
        options = p.get("options") or {}
        categories = str(options.get("categories", "")).split(",")
        if (p.get("handler") == SENSITIVE_DATA and str(options.get("action", "block")).lower() == "block"
                and "class.us_ssn" in categories):
            return p.get("name")
    return None


def read_effective(client, name: str) -> list[dict]:
    """Every principal's effective privileges on the model service, following pages."""
    found, token = [], None
    while True:
        query = {"max_results": 0, **({"page_token": token} if token else {})}
        page = get(client, f"{EFFECTIVE_API}/model_service/{name}", query) or {}
        found += page.get("privilege_assignments") or []
        token = page.get("next_page_token")
        if not token:
            return found


def access_findings(assignments: list[dict], name: str, me: str) -> tuple[list[str], list[str]]:
    """(problems, warnings) about who other than you can call the service. A grant on the service itself means other
    people use it; one on its schema or catalog means they can, though nobody is told it exists."""
    direct: dict[str, set[str]] = {}
    inherited: dict[str, set[str]] = {}
    for a in assignments:
        principal = a.get("principal")
        if not principal or principal == me:
            continue
        for p in a.get("privileges") or []:
            privilege = str(p.get("privilege", "")).upper().replace(" ", "_")
            if privilege in CALLS:
                where = inherited if p.get("inherited_from_name") else direct
                where.setdefault(principal, set()).add(privilege)
    problems = [f"{name}: {principal} holds {', '.join(sorted(privileges))} on it, so other people use it. The "
                "evaluation measures only a copy of its own: take the grant away, or evaluate a copy (module 00, "
                "step 1)" for principal, privileges in sorted(direct.items())]
    warnings = [f"{name}: {principal} can call it through a grant on its schema or catalog. Nobody else is told it "
                "exists, but its guardrails apply to their calls" for principal in sorted(inherited)]
    return problems, warnings


def inspect(client, entry: dict, me: str) -> dict:
    """One service's checks: {name, exists, policies, ssn_policy, problems, warnings}."""
    name, wanted = entry["name"], list(entry["policies"])
    out = {"name": name, "exists": False, "policies": [], "ssn_policy": None, "problems": [], "warnings": []}
    service = get(client, f"{API}/{name}")
    if service is None:
        out["problems"].append(f"{name} doesn't exist: run forge-aig-create-model-service's module 03 (apply)")
        return out
    out["exists"] = True
    config = service.get("config") or {}
    policies = sorted(live(config.get("service_policies")), key=lambda p: p.get("rank") or 0)
    out["policies"] = [p.get("name") for p in policies]
    if sorted(out["policies"]) != sorted(wanted):
        out["problems"].append(f"{name} has the policies {out['policies']}, not {wanted} from model-services.yaml: "
                               "apply forge-aig-create-model-service again (its module 03)")
    out["problems"] += policy_problems(name, policies)
    out["ssn_policy"] = ssn_policy(policies)
    if service.get("owner") and me and service["owner"] != me:
        out["warnings"].append(f"{name} is owned by {service['owner']}, not you: check it's the evaluation's copy")
    if config.get("inference_table"):
        out["warnings"].append(f"{name} logs to an inference table, a second copy of the harmful prompts. Set "
                               "enable_inference_table: false on the evaluation's services")
    if config.get("rate_limits"):
        out["warnings"].append(f"{name} has rate limits, which slow a run (rate-limited calls are retried). Set "
                               "enable_rate_limits: false on the evaluation's services")
    try:
        problems, warnings = access_findings(read_effective(client, name), name, me)
    except Exception as e:  # noqa: BLE001 - who else holds privileges is unknown, not a failure
        problems, warnings = [], [f"{name}: who else can call it couldn't be read ({type(e).__name__}): check that "
                                  "nobody else has been granted it"]
    out["problems"] += problems
    out["warnings"] += warnings
    return out


def call(client, model_service: str, prompt: str, max_tokens: int, attempts: int = 6, sleep=time.sleep) -> dict:
    """One prompt through the gateway: {blocked, policy, phase, reason, action, finish_reason, content, http, latency_ms,
    error}. A policy that fails to evaluate is a block by it (fail closed); transient failures are retried."""
    body = {"model": model_service, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    out = {"blocked": False, "policy": None, "phase": None, "reason": None, "action": None, "finish_reason": None,
           "content": None, "http": None, "latency_ms": None, "error": None}
    t0 = time.time()
    response = None
    for attempt in range(attempts):
        try:
            response = client.do("POST", GATEWAY, body=body)
            out["http"], out["error"] = 200, None
            break
        except Exception as e:  # noqa: BLE001 - every failure becomes a recorded outcome
            out["http"] = getattr(e, "status_code", None) or STATUS.get(type(e).__name__, -1)
            out["error"] = f"{type(e).__name__}: {e}"[:800]
            failed = FAILED.search(str(e))
            if failed and not RATE_LIMITED.search(str(e)):  # fail closed: the gateway denied the request
                out.update(blocked=True, policy=failed.group(2), action="deny",
                           phase="pre_call" if failed.group(1).lower() == "input" else "post_call",
                           reason=f"evaluation failed (fail closed): {str(e)[:300]}")
                break
            if type(e).__name__ not in TRANSIENT or attempt == attempts - 1:
                break
            sleep(min(2 ** attempt, 30))
    out["latency_ms"] = round((time.time() - t0) * 1000, 1)
    if response is None:
        return out
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    policy = response.get("databricks_service_policy") or {}
    out.update(finish_reason=choice.get("finish_reason"), content=content, policy=policy.get("name"),
               phase=policy.get("phase"), reason=policy.get("reason"), action=policy.get("action"))
    out["blocked"] = out["finish_reason"] == "content_filter" or str(out["action"] or "").lower() == "deny"
    return out


def probe_outcomes(name: str, passed: dict, blocked: dict | None, ssn: str | None) -> list[str]:
    """Problems with one service's probe prompts; blocked is None when it has no policy that should block an SSN."""
    problems = []
    if passed.get("error") and not passed.get("blocked"):
        problems.append(f"{name}: the benign prompt failed: {passed['error']}")
    elif passed.get("blocked"):
        problems.append(f"{name}: the benign prompt was blocked by {passed.get('policy')}: {passed.get('reason')}")
    if ssn:
        if blocked is None:
            problems.append(f"{name}: the SSN prompt wasn't sent")
        elif blocked.get("error") and not blocked.get("blocked"):
            problems.append(f"{name}: the SSN prompt failed: {blocked['error']}")
        elif not blocked.get("blocked"):
            problems.append(f"{name}: the SSN prompt wasn't blocked. Policy changes take a minute or two to take "
                            "effect: wait, then run --probe again")
        elif blocked.get("policy") != ssn:
            problems.append(f"{name}: the SSN prompt was blocked by {blocked.get('policy')}, not {ssn}")
    return problems


def main() -> None:
    args = parse_args()
    state, services = load(args.state)
    client = workspace_client(state).api_client
    me = (get(client, SCIM_ME) or {}).get("userName") or ""
    results = []
    for entry in services:
        result = inspect(client, entry, me)
        if args.probe and result["exists"]:
            passed = call(client, entry["name"], PASS_PROMPT, 16)
            blocked = call(client, entry["name"], BLOCK_PROMPT, 16) if result["ssn_policy"] else None
            result["probe"] = {"pass_prompt": passed, "block_prompt": blocked}
            result["problems"] += probe_outcomes(entry["name"], passed, blocked, result["ssn_policy"])
        results.append(result)
    problems = sum(len(r["problems"]) for r in results)
    print(json.dumps({"services": results, "problems": problems}, indent=2))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
