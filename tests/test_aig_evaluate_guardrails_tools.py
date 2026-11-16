"""Checks for forge-aig-evaluate-guardrails' local tools and bundle template: check_services.py, evaluate.py,
render.py, job.py, and scripts/databricks.yml, and its handoff to forge-aig-create-model-service. No workspace: the
REST client is a stand-in. The job notebooks are tested in test_aig_evaluate_guardrails.py."""

import copy
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import SKILLS_DIR, load_script

SKILL = "forge-aig-evaluate-guardrails"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
service = load_script(SKILL, "check_services")
evaluate = load_script(SKILL, "evaluate")
render_module = load_script(SKILL, "render")
job = load_script(SKILL, "job")
deploy = load_script("forge-aig-create-model-service", "deploy_services")

SERVICE = "main.guardrail_eval.guardrail_eval_model"
STATE = {
    "profile": "DEFAULT",
    "catalog": "main",
    "schema": "guardrail_eval",
    "prefix": "guardrail_eval",
    "name": "guardrail-eval",
    "warehouse_id": "abc123",
    "dataset": {"source": "default"},
    "services_state": "aig-model-services.json",
    "model_services": [SERVICE],
    "concurrency": 8,
    "max_tokens": 64,
    "sample_size": 100,
    "settle_minutes": 5,
    "experiment": "/Users/someone@example.com/guardrail-eval",
    "targets": {"recall": 0.9, "fp_rate": 0.1},
    "completed": [],
}
ME = "someone@example.com"
EVAL_POLICIES = ["eval_sensitive_data", "eval_unsafe_content", "eval_jailbreak"]


def entries(file: str):
    """The YAML entries module 00, step 7 passes to forge-aig-create-model-service for one of its files."""
    text = (SKILLS_DIR / SKILL / "modules" / "00-preflight.md").read_text()
    blocks = [textwrap.dedent(b) for b in re.findall(r"```yaml\n(.*?)```", text, re.S)]
    return yaml.safe_load(next(b for b in blocks if b.startswith(f"# {file}")))


# --- the handoff: module 00's entries, as forge-aig-create-model-service checks and sends them ------------------

def built_policies() -> list[dict]:
    """The default service's policies as deploy_services.py sends them to the API."""
    cfg = {"catalog": "main", "schema": "guardrail_eval", "model_services": entries("model-services.yaml")}
    policies_cfg = {"default_evaluator": "system.ai.gpt-oss-20b", "service_policies": entries("service-policies.yaml")}
    names = cfg["model_services"][0]["service_policies"]
    return [deploy.build_policy(cfg, policies_cfg, n, i) for i, n in enumerate(names, 1)]


def test_module_00_entries_pass_the_model_services_skills_checks():
    services, policies = entries("model-services.yaml"), entries("service-policies.yaml")
    cfg = {"catalog": "main", "schema": "guardrail_eval", "model_services": services}
    policies_cfg = {"default_evaluator": "system.ai.gpt-oss-20b", "service_policies": policies}
    defined = {p["name"]: p for p in policies}
    assert deploy.check_services(cfg, defined, True) == ([], [])
    assert deploy.check_policies(policies_cfg, {n for s in services for n in s["service_policies"]}) == ([], [])
    assert [s["name"] for s in services] == ["guardrail_eval_model"]
    assert services[0]["service_policies"] == EVAL_POLICIES
    body = deploy.build_service(cfg, services[0])
    assert "inference_table" not in body["config"] and "rate_limits" not in body["config"]  # no copy, no throttling
    assert body["config"]["routing"]["destinations"][0]["pay_per_token_config"] == {
        "model": "models/system.ai.databricks-claude-sonnet-5-5"}


def test_module_00_entries_block_and_are_what_check_services_reads():
    policies = built_policies()
    assert [p["handler"] for p in policies] == ["system.ai.detect_sensitive_data", "system.ai.block_unsafe_content",
                                                "system.ai.block_jailbreak"]
    assert all(p["options"]["dry_run"] == "false" for p in policies)
    assert len(policies[0]["options"]["categories"].split(",")) == 15 and policies[0]["options"]["action"] == "block"
    assert policies[1]["options"]["model_service"] == "model-services/system.ai.gemma-3-12b"
    assert policies[2]["options"]["model_service"] == "model-services/system.ai.gpt-oss-20b"
    assert service.policy_problems(SERVICE, policies) == []
    assert service.ssn_policy(policies) == "eval_sensitive_data"


# --- check_services.py ---------------------------------------------------------------------------------------

def live_service(policies=None, owner=ME, **config):
    return {"name": SERVICE, "owner": owner, "config": {
        "routing": {"destinations": [{"name": "claude"}]},
        "service_policies": built_policies() if policies is None else policies, **config}}


def test_policy_problems_name_log_only_and_redacting_policies():
    policies = built_policies()
    logging = dict(policies[1], options={**policies[1]["options"], "dry_run": "true"})
    redacting = dict(policies[0], options={**policies[0]["options"], "action": "transform"})
    problems = service.policy_problems(SERVICE, [redacting, logging, policies[2]])
    assert len(problems) == 2
    assert "eval_sensitive_data redacts" in problems[0] and "eval_unsafe_content only logs" in problems[1]
    assert service.ssn_policy([redacting]) is None  # a redaction isn't a block
    no_ssn = dict(policies[0], options={**policies[0]["options"], "categories": "class.email_address"})
    assert service.ssn_policy([no_ssn]) is None


def test_access_findings_tell_grants_on_the_service_from_inherited_ones():
    assignments = [
        {"principal": ME, "privileges": [{"privilege": "MANAGE"}, {"privilege": "EXECUTE"}]},
        {"principal": "data-science", "privileges": [{"privilege": "EXECUTE"}]},
        {"principal": "account users", "privileges": [{"privilege": "EXECUTE", "inherited_from_type": "SCHEMA",
                                                        "inherited_from_name": "main.guardrail_eval"}]},
        {"principal": "auditors", "privileges": [{"privilege": "READ_METADATA"}]},  # can't call it
    ]
    problems, warnings = service.access_findings(assignments, SERVICE, ME)
    assert len(problems) == 1 and "data-science holds EXECUTE on it" in problems[0]
    assert len(warnings) == 1 and "account users can call it through a grant on its schema or catalog" in warnings[0]


class NotFound(Exception):
    """Stands in for databricks.sdk.errors.NotFound, which check_services matches by name."""


class FakeClient:
    """Stands in for WorkspaceClient().api_client: the model services, their effective permissions, and the gateway."""

    def __init__(self, services: dict, grants: dict | None = None, unreadable=False, outcomes=None):
        self.services, self.grants, self.unreadable = services, grants or {}, unreadable
        self.outcomes = list(outcomes or [])
        self.calls = []

    def do(self, method, path, query=None, body=None):
        self.calls.append((method, path, query, body))
        if method == "POST" and path == service.GATEWAY:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        assert method == "GET", f"check_services changed something: {method} {path}"
        if path == service.SCIM_ME:
            return {"userName": ME}
        if path.startswith(service.EFFECTIVE_API + "/model_service/"):
            if self.unreadable:
                raise PermissionError("PERMISSION_DENIED")
            name = path.rsplit("/", 1)[1]
            return {"privilege_assignments": [{"principal": p, "privileges": [{"privilege": x} for x in privileges]}
                                              for p, privileges in self.grants.get(name, {}).items()]}
        name = path.removeprefix(service.API + "/")
        if name not in self.services:
            raise NotFound(name)
        return json.loads(json.dumps(self.services[name]))


ENTRY = {"name": SERVICE, "policies": EVAL_POLICIES}


def test_inspect_passes_the_evaluations_own_service():
    client = FakeClient({SERVICE: live_service()}, grants={SERVICE: {ME: ["MANAGE"]}})
    out = service.inspect(client, ENTRY, ME)
    assert out["problems"] == [] and out["warnings"] == []
    assert out["policies"] == EVAL_POLICIES and out["ssn_policy"] == "eval_sensitive_data"
    assert all(method == "GET" for method, *_ in client.calls)


def test_inspect_reports_what_would_spoil_the_measurement():
    missing = service.inspect(FakeClient({}), ENTRY, ME)
    assert not missing["exists"] and "doesn't exist" in missing["problems"][0]

    shared = live_service(policies=built_policies()[:2], owner="someone.else@example.com",
                          inference_table={"table_name_prefix": "guardrail_eval_model"},
                          rate_limits=[{"requests": 60}])
    out = service.inspect(FakeClient({SERVICE: shared}, grants={SERVICE: {"data-science": ["EXECUTE"]}}), ENTRY, ME)
    assert len(out["problems"]) == 2
    assert "has the policies ['eval_sensitive_data', 'eval_unsafe_content'], not" in out["problems"][0]
    assert "data-science holds EXECUTE" in out["problems"][1]
    assert [w.split()[1] for w in out["warnings"]] == ["is", "logs", "has"]  # owner, inference table, rate limits

    unreadable = service.inspect(FakeClient({SERVICE: live_service()}, unreadable=True), ENTRY, ME)
    assert unreadable["problems"] == [] and "couldn't be read" in unreadable["warnings"][0]


def test_load_reads_the_rendered_services(tmp_path: Path):
    state = tmp_path / "guardrail-eval.json"
    state.write_text(json.dumps(STATE))
    with pytest.raises(SystemExit, match="render.py writes it"):
        service.load(state)
    config = tmp_path / "guardrail-eval" / "src" / "eval_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"services": [ENTRY]}))
    assert service.load(state)[1] == [ENTRY]
    state.write_text(json.dumps({**STATE, "model_services": [SERVICE, "main.guardrail_eval.guardrail_eval_other"]}))
    with pytest.raises(SystemExit, match="render again"):
        service.load(state)


def gateway_error(name: str, message: str, status: int) -> Exception:
    """An exception named like the SDK's (check_services matches error classes by name), with a status code."""
    error = type(name, (Exception,), {})(message)
    error.status_code = status
    return error


ALLOWED = {"choices": [{"message": {"content": "Paris."}, "finish_reason": "stop"}]}
BLOCKED = {"choices": [{"message": {"content": "Blocked by policy."}, "finish_reason": "content_filter"}],
           "databricks_service_policy": {"name": "eval_sensitive_data", "action": "deny", "phase": "pre_call",
                                         "reason": "Detected sensitive data (US_SSN)"}}


def test_call_reads_a_block_and_a_pass():
    out = service.call(FakeClient({}, outcomes=[BLOCKED]), "m", "p", 16)
    assert out["blocked"] and out["policy"] == "eval_sensitive_data" and out["phase"] == "pre_call"
    out = service.call(FakeClient({}, outcomes=[ALLOWED]), "m", "p", 16)
    assert not out["blocked"] and out["content"] == "Paris." and out["http"] == 200


def test_call_fails_closed_retries_rate_limits_and_records_errors():
    failed = gateway_error("InternalError", "Input policy 'eval_jailbreak' failed to evaluate", 500)
    client = FakeClient({}, outcomes=[failed])
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert out["blocked"] and out["policy"] == "eval_jailbreak" and len(client.calls) == 1  # not retried
    limited = gateway_error("InternalError", "Input policy 'eval_jailbreak' failed to evaluate: 429 rate limit", 500)
    client = FakeClient({}, outcomes=[limited, ALLOWED])
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert not out["blocked"] and out["error"] is None and len(client.calls) == 2  # a rate limit is retried
    client = FakeClient({}, outcomes=[gateway_error("PermissionDenied", "no EXECUTE", 403)])
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert not out["blocked"] and out["http"] == 403 and "no EXECUTE" in out["error"] and len(client.calls) == 1


def test_probe_outcomes():
    ok = {"blocked": False, "error": None}
    ssn = {"blocked": True, "policy": "eval_sensitive_data", "error": None}
    assert service.probe_outcomes(SERVICE, ok, ssn, "eval_sensitive_data") == []
    assert "wait, then run --probe again" in service.probe_outcomes(SERVICE, ok, ok, "eval_sensitive_data")[0]
    other = dict(ssn, policy="eval_unsafe_content")
    assert "blocked by eval_unsafe_content" in service.probe_outcomes(SERVICE, ok, other, "eval_sensitive_data")[0]
    assert "benign prompt was blocked" in service.probe_outcomes(SERVICE, ssn, ssn, "eval_sensitive_data")[0]
    assert service.probe_outcomes(SERVICE, ok, None, None) == []  # no sensitive-data policy: no SSN prompt


# --- evaluate.py ---------------------------------------------------------------------------------------------

def metric(breakdown, group, service=SERVICE, **values):
    row = {"run_id": "eval-full-202611021200", "mode": "full", "model_service": service, "breakdown": breakdown,
           "dataset": "all", "group_name": group}
    row.update(dict.fromkeys(evaluate.NUMERIC))
    row.update(values)
    return row


ROWS = [
    metric("overall", "all", n=1000, tp=450, fn=50, fp=40, tn=455, errors=5, recall=0.9, precision=0.918, fp_rate=0.081,
           f1=0.909, accuracy=0.905, latency_p50_ms=1300.0, latency_p95_ms=5000.0),
    metric("dataset", "aegis2", n=400, recall=0.95, fp_rate=0.05, errors=2),
    metric("category", "Violence", dataset="aegis2", n=50, recall=0.98),
    metric("category", "Hate/Identity Hate", dataset="aegis2", n=40, recall=0.8),
    metric("policy", "eval_unsafe_content", blocked=300, tp=290, fp=10),
]
OTHER = "main.guardrail_eval.guardrail_eval_jailbreak"
OTHER_ROWS = [metric("overall", "all", service=OTHER, n=1000, tp=300, fn=100, fp=10, tn=500, errors=90, recall=0.75,
                     precision=0.968, fp_rate=0.02, f1=0.845, accuracy=0.888, latency_p50_ms=900.0)]


def test_thresholds():
    assert evaluate.THRESHOLDS == {"scored_rate": 0.98}
    assert evaluate.threshold_failures(ROWS[0]) == []  # 995 of 1000 scored
    low = dict(ROWS[0], tp=400, errors=55)
    assert "errored" in evaluate.threshold_failures(low)[0]
    assert evaluate.threshold_failures(None) == ["the run has no overall metrics row"]


def test_target_results_treat_fp_rate_as_a_ceiling():
    results = evaluate.target_results(ROWS, {"recall": 0.95, "fp_rate": 0.1, "precision": 0.9})
    assert [(r["metric"], r["kind"], r["met"]) for r in results] == [
        ("recall", "floor", False), ("fp_rate", "ceiling", True), ("precision", "floor", True)]
    assert evaluate.target_results(ROWS, None) == []


def test_report_for_one_service():
    text = evaluate.report(ROWS, {"recall": 0.95})
    assert text.startswith("Run eval-full-202611021200 (full), 1 model service\n")
    assert "| Service |" not in text  # no comparison with one service
    assert f"### {SERVICE}" in text and "1000 prompts, 5 errors, scored rate 99.5%" in text
    assert "| aegis2 | 400 | 95.0% |" in text and "| eval_unsafe_content | 300 | 10 |" in text
    assert text.index("Hate/Identity Hate") < text.index("Violence")  # lowest recall first
    assert "| recall | ≥ 95.0% | 90.0% | no |" in text


def test_report_compares_services():
    text = evaluate.report(ROWS + OTHER_ROWS, None)
    assert "2 model services" in text.splitlines()[0]
    assert f"| {SERVICE} | 1000 | 90.0% | 91.8% | 8.1% | 90.9% | 5 | 99.5% | 1.3 s |" in text
    assert f"| {OTHER} | 1000 | 75.0% |" in text
    assert text.index("| Service |") < text.index(f"### {SERVICE}") < text.index(f"### {OTHER}")


def test_outcome_gates_each_service_and_needs_every_one():
    state = {"model_services": [SERVICE, OTHER], "targets": {"recall": 0.8}}
    result = evaluate.outcome(ROWS + OTHER_ROWS, state)
    assert not result["thresholds_met"] and result["failures"][0].startswith(f"{OTHER}: scored_rate=0.910")
    assert result["services"][SERVICE] == {"scored_rate": 0.995, "failures": [], "targets_met": True}
    assert result["services"][OTHER]["targets_met"] is False and result["targets_met"] is False
    missing = evaluate.outcome(ROWS, state)
    assert missing["failures"] == [f"{OTHER} isn't in this run: run the evaluation again, so it covers every service "
                                   "in model_services"]
    assert evaluate.outcome(ROWS, {"model_services": [SERVICE]})["thresholds_met"]
    old = [{k: v for k, v in r.items() if k != "model_service"} for r in ROWS]  # a table from before services compared
    assert evaluate.outcome(old, state)["thresholds_met"]


def test_typed_metrics_rows():
    row = evaluate.typed({"n": "10", "recall": "0.5", "fp": None, "group_name": "x"})
    assert row == {"n": 10, "recall": 0.5, "fp": None, "group_name": "x"}


# --- render.py and the bundle template -------------------------------------------------------------------

NOTEBOOK = "# Databricks notebook source\n# MAGIC %md\n# MAGIC # {title}\n\n# COMMAND ----------\n\nprint('{title}')\n"
APPLIED = ["00-preflight", "01-template", "02-plan", "03-apply", "04-evaluate"]


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    """A copy of the skill's render.py and template, with stand-in notebooks, so the test needs only these files."""
    folder = tmp_path / "skill" / "scripts"
    folder.mkdir(parents=True)
    for name in ("render.py", "databricks.yml"):
        shutil.copy(SCRIPTS / name, folder / name)
    for name in render_module.JOB_FILES:
        (folder / name).write_text(NOTEBOOK.format(title=name))
    return folder


def services_files(work: Path, services=None, policies=None, parts=("model_services", "service_policies"),
                   completed=APPLIED, permissions=None, runtime=None) -> None:
    """forge-aig-create-model-service's files in the working directory, as it leaves them after module 03."""
    (work / "aig-model-services.json").write_text(json.dumps({
        **({"runtime": runtime} if runtime else {}), "profile": "DEFAULT", "target_schema": "main.guardrail_eval",
        "catalog": "main", "schema": "guardrail_eval", "parts": list(parts), "config_dir": "ai-gateway",
        "completed": list(completed)}))
    folder = work / "ai-gateway"
    folder.mkdir(exist_ok=True)
    (folder / "model-services.yaml").write_text(yaml.safe_dump({
        "catalog": "main", "schema": "guardrail_eval",
        "model_services": entries("model-services.yaml") if services is None else services}))
    (folder / "service-policies.yaml").write_text(yaml.safe_dump({
        "default_evaluator": "system.ai.gpt-oss-20b",
        "service_policies": entries("service-policies.yaml") if policies is None else policies}))
    if permissions is not None:
        (folder / "permissions.yaml").write_text(yaml.safe_dump(permissions))


def write_state(tmp_path: Path, files: dict | None = None, **overrides) -> Path:
    path = tmp_path / "work" / "guardrail-eval.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({**STATE, **overrides}))
    if files is not None:  # the model services skill's state records the same runtime, unless files says otherwise
        services_files(path.parent, **{"runtime": overrides.get("runtime"), **files})
    return path


def render(scripts: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(scripts / "render.py"), *args], capture_output=True, text=True,
                          check=False)


def test_render_writes_the_bundle_and_config(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path, files={})
    files = [state.parent / "aig-model-services.json", *(state.parent / "ai-gateway").iterdir()]
    theirs = {p: p.read_text() for p in files}
    result = render(scripts, "--state", str(state))
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["services"] == [SERVICE] and summary["warnings"] == []
    bundle = state.parent / "guardrail-eval"
    parsed = yaml.safe_load((bundle / "databricks.yml").read_text())
    assert parsed["bundle"]["name"] == "guardrail-eval" and parsed["targets"]["default"]["workspace"]["profile"] == "DEFAULT"
    assert {k: v["default"] for k, v in parsed["variables"].items()} == {
        "catalog": "main", "schema": "guardrail_eval", "prefix": "guardrail_eval"}
    job = parsed["resources"]["jobs"]["guardrail_eval"]
    assert [p["name"] for p in job["parameters"]] == ["mode", "run_id"] and "trigger" not in job
    tasks = {t["task_key"]: t for t in job["tasks"]}
    assert list(tasks) == ["load_prompts", "run_eval"] and "environments" not in job
    for key, task in tasks.items():
        assert task["notebook_task"]["notebook_path"] == f"src/{key}.py" and "environment_key" not in task
        assert task["notebook_task"]["base_parameters"] == {
            "catalog": "${var.catalog}", "schema": "${var.schema}", "prefix": "${var.prefix}",
            "config": "${workspace.file_path}/src/eval_config.json"}
    assert tasks["run_eval"]["max_retries"] == 0 and tasks["run_eval"]["disable_auto_optimization"] is True
    config = json.loads((bundle / "src" / "eval_config.json").read_text())
    assert config.pop("_rendered_by").startswith(f"{SKILL} render.py from guardrail-eval.json (sha256:")
    assert config == {"dataset": {"source": "default"}, "services": [{"name": SERVICE, "policies": EVAL_POLICIES}],
                      "concurrency": 8, "max_tokens": 64, "sample_size": 100, "settle_minutes": 5,
                      "experiment": "/Users/someone@example.com/guardrail-eval"}
    copy = (bundle / "src" / "run_eval.py").read_text().splitlines()
    assert copy[0] == "# Databricks notebook source" and copy[1].startswith(f"# Copied from {SKILL}")
    assert set(json.loads(render(scripts, "--state", str(state)).stdout)["unchanged"]) == {
        str(bundle / "databricks.yml"), str(bundle / "src" / "eval_config.json"), str(bundle / "src" / "load_prompts.py"),
        str(bundle / "src" / "run_eval.py")}
    assert theirs == {p: p.read_text() for p in theirs}  # forge-aig-create-model-service's files are only read


def test_render_lists_each_service_to_compare(scripts: Path, tmp_path: Path):
    template = entries("model-services.yaml")[0]
    services = [dict(template, name=f"guardrail_eval_{p.removeprefix('eval_')}", service_policies=[p])
                for p in EVAL_POLICIES]
    services[2].pop("enable_rate_limits")  # rate limits on by default: a warning, not an error
    names = [f"main.guardrail_eval.{s['name']}" for s in services]
    state = write_state(tmp_path, files={"services": services}, model_services=names)
    result = render(scripts, "--state", str(state))
    assert result.returncode == 0, result.stderr
    config = json.loads((state.parent / "guardrail-eval" / "src" / "eval_config.json").read_text())
    assert config["services"] == [{"name": n, "policies": [p]} for n, p in zip(names, EVAL_POLICIES, strict=True)]
    assert json.loads(result.stdout)["warnings"] == [f"{names[2]} has rate limits, which slow a run: set "
                                                     "enable_rate_limits: false on it"]


def test_render_protects_a_hand_edited_config_and_keeps_an_edited_notebook(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path, files={})
    assert render(scripts, "--state", str(state)).returncode == 0
    src = state.parent / "guardrail-eval" / "src"
    config = src / "eval_config.json"
    config.write_text(config.read_text().replace('"concurrency": 8', '"concurrency": 32'))
    refused = render(scripts, "--state", str(state))
    assert refused.returncode == 1 and "Edited by hand" in refused.stderr
    assert render(scripts, "--state", str(state), "--force").returncode == 0
    assert '"concurrency": 8' in config.read_text()

    notebook = src / "run_eval.py"
    notebook.write_text(notebook.read_text().replace("print('run_eval.py')", "print('changed in the workspace')"))
    kept = json.loads(render(scripts, "--state", str(state)).stdout)
    assert kept["kept_edited"] == [str(notebook)] and "changed in the workspace" in notebook.read_text()
    forced = json.loads(render(scripts, "--state", str(state), "--force").stdout)
    assert str(notebook) in forced["written"] and "changed in the workspace" not in notebook.read_text()


@pytest.mark.parametrize("change, message", [
    ({"name": "Guardrail_Eval"}, "name="),
    ({"prefix": "Bad-Prefix"}, "prefix="),
    ({"model_services": ["svc"]}, "model_services: 'svc'"),
    ({"model_services": []}, "model_services is missing"),
    ({"model_services": ["main.prod.team_chat"]}, "isn't named guardrail_eval_..."),
    ({"model_services": [SERVICE, SERVICE]}, "each once"),
    ({"services_state": "../elsewhere.json"}, "services_state="),
    ({"experiment": "guardrail-eval"}, "experiment="),
    ({"concurrency": 0}, "concurrency="),
    ({"targets": {"latency": 1}}, "targets.latency"),
    ({"targets": {"recall": 1.5}}, "targets.recall="),
    ({"dataset": {"source": "hub"}}, "dataset.source="),
    ({"dataset": {"source": "default", "defaults": ["aegis3"]}}, "dataset.defaults="),
    ({"dataset": {"source": "table", "table": "t", "prompt_column": "text", "label_column": "label",
                  "block_values": ["1"]}}, "dataset.table="),
    ({"dataset": {"source": "file", "path": "/tmp/x.csv", "format": "xlsx", "prompt_column": "text",
                  "label_column": "label", "block_values": []}}, "dataset.block_values"),
])
def test_render_reports_bad_state(scripts: Path, tmp_path: Path, change, message):
    result = render(scripts, "--state", str(write_state(tmp_path, files={}, **change)))
    assert result.returncode == 1 and message in result.stderr, result.stderr


def policy(name: str, **settings) -> dict:
    return {**next(p for p in entries("service-policies.yaml") if p["name"] == name), **settings}


@pytest.mark.parametrize("files, message", [
    (None, "aig-model-services.json not found: run forge-aig-create-model-service"),
    ({"completed": ["00-preflight", "01-template", "02-plan"]}, "hasn't applied the services"),
    ({"parts": ["model_services"]}, "needs model_services and service_policies"),
    ({"services": []}, "isn't in ai-gateway/model-services.yaml"),
    ({"policies": [policy("eval_sensitive_data"), policy("eval_unsafe_content", mode="log"), policy("eval_jailbreak")]},
     "eval_unsafe_content has mode: log"),
    ({"policies": [policy("eval_sensitive_data", action="redact"), policy("eval_unsafe_content"),
                   policy("eval_jailbreak")]}, "eval_sensitive_data redacts instead of blocking"),
    ({"policies": [policy("eval_sensitive_data"), policy("eval_unsafe_content")]}, "eval_jailbreak isn't defined"),
    ({"parts": ["model_services", "service_policies", "grants"],
      "permissions": {"groups": [{"name": "data-science", "model_services": [SERVICE]}], "users": []}},
     "is granted to others in permissions.yaml"),
])
def test_render_checks_the_model_services_skills_files(scripts: Path, tmp_path: Path, files, message):
    result = render(scripts, "--state", str(write_state(tmp_path, files=files)))
    assert result.returncode == 1 and message in result.stderr, result.stderr
    assert not (tmp_path / "work" / "guardrail-eval").exists()  # nothing rendered


def test_render_accepts_a_user_dataset(scripts: Path, tmp_path: Path):
    dataset = {"source": "file", "path": "/Volumes/main/guardrail_eval/inbox/prompts.jsonl", "format": "jsonl",
               "prompt_column": "text", "label_column": "verdict", "block_values": ["unsafe", 1],
               "category_column": "topic", "name": "support_tickets"}
    state = write_state(tmp_path, files={}, dataset=dataset)
    assert render(scripts, "--state", str(state)).returncode == 0
    config = json.loads((state.parent / "guardrail-eval" / "src" / "eval_config.json").read_text())
    assert config["dataset"] == dataset


def test_render_lists_every_problem_at_once():
    errors = render_module.dataset_errors({"source": "table", "table": "a.b.c"})
    assert len(errors) == 3  # prompt_column, label_column, block_values


# ── Genie Code in a notebook: render.py's target, and job.py ─────────────────────────


def test_render_in_a_notebook_leaves_the_profile_out(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path, files={}, runtime="workspace", profile=None)
    result = render(scripts, "--state", str(state))
    assert result.returncode == 0, result.stderr
    text = (state.parent / "guardrail-eval" / "databricks.yml").read_text()
    target = yaml.safe_load(text)["targets"]["default"]
    assert target == {"default": True} and "profile" not in text and "# >>>" not in text


def test_render_keeps_the_profile_locally_without_the_markers(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path, files={})
    assert render(scripts, "--state", str(state)).returncode == 0
    text = (state.parent / "guardrail-eval" / "databricks.yml").read_text()
    assert yaml.safe_load(text)["targets"]["default"]["workspace"] == {"profile": "DEFAULT"} and "# >>>" not in text


@pytest.mark.parametrize("change, files, message", [
    ({"profile": None}, {}, "profile is missing or empty: locally"),
    ({"runtime": "cloud"}, {}, "runtime='cloud'"),
    ({"runtime": "workspace", "profile": None}, {"runtime": "local"},
     "has runtime 'local' and guardrail-eval.json 'workspace'"),
])
def test_render_checks_the_runtime(scripts: Path, tmp_path: Path, change, files, message):
    result = render(scripts, "--state", str(write_state(tmp_path, files=files, **change)))
    assert result.returncode == 1 and message in result.stderr, result.stderr


def test_the_scripts_sign_in_without_a_profile_in_a_notebook(tmp_path: Path):
    assert service.runtime_errors({"runtime": "workspace"}) == []
    assert service.runtime_errors({}) == ["profile is missing or empty: locally, every command uses the CLI profile"]
    state = write_state(tmp_path, runtime="workspace", profile=None)
    config = state.parent / "guardrail-eval" / "src" / "eval_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"services": [{"name": SERVICE, "policies": EVAL_POLICIES}]}))
    assert service.load(state)[1] == [{"name": SERVICE, "policies": EVAL_POLICIES}]


FILE_PATH = f"/Workspace/Users/{ME}/.bundle/guardrail-eval/default/files"


class FakeWorkspace:
    """The Jobs, Workspace, and SCIM APIs, as far as job.py uses them, keeping the jobs, files, and runs it's sent."""

    def __init__(self):
        self.config = SimpleNamespace(host="https://example.cloud.databricks.com/")
        self.api_client = self
        self.jobs, self.files, self.started, self.runs, self.outputs = {}, {}, [], {}, {}
        self.next_id = 100

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def do(self, method, path, query=None, body=None):
        if path == job.SCIM_ME:
            return {"userName": ME}
        if path == f"{job.WORKSPACE}/mkdirs":
            return {}
        if path == f"{job.WORKSPACE}/import":
            self.files[body["path"]] = body
            return {}
        if path == f"{job.JOBS}/list":
            found = [{"job_id": i} for i, settings in self.jobs.items()
                     if settings["name"].lower() == query["name"].lower()]
            return {"jobs": found} if found else {}
        if path == f"{job.JOBS}/get":
            return {"job_id": query["job_id"], "settings": copy.deepcopy(self.jobs[query["job_id"]])}
        if path == f"{job.JOBS}/create":
            job_id = self.new_id()
            self.jobs[job_id] = copy.deepcopy(body)
            return {"job_id": job_id}
        if path == f"{job.JOBS}/reset":
            self.jobs[body["job_id"]] = copy.deepcopy(body["new_settings"])
            return {}
        if path == f"{job.JOBS}/run-now":
            self.started.append(body)
            return {"run_id": self.new_id()}
        if path == f"{job.JOBS}/runs/get":
            return self.runs.get(query["run_id"], {"state": {"life_cycle_state": "PENDING"}, "tasks": []})
        if path == f"{job.JOBS}/runs/get-output":
            return self.outputs.get(query["run_id"], {})
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def notebook_bundle(scripts: Path, tmp_path: Path) -> Path:
    """A bundle rendered in a notebook (runtime workspace): the state file's path."""
    state = write_state(tmp_path, files={}, runtime="workspace", profile=None)
    result = render(scripts, "--state", str(state))
    assert result.returncode == 0, result.stderr
    return state


def test_job_settings_are_the_bundles_job_with_deployed_notebook_paths(notebook_bundle: Path):
    bundle = job.read_bundle(notebook_bundle.parent / "guardrail-eval")
    key, settings = job.job_settings(bundle, FILE_PATH)
    assert key == "guardrail_eval" and settings["name"] == "guardrail eval guardrail_eval"
    assert settings["max_concurrent_runs"] == 1 and settings["queue"] == {"enabled": True}
    assert settings["parameters"] == [{"name": "mode", "default": "sample"}, {"name": "run_id", "default": ""}]
    assert settings["tags"] == {"forge_bundle": "guardrail-eval"}
    tasks = {t["task_key"]: t for t in settings["tasks"]}
    assert list(tasks) == ["load_prompts", "run_eval"]
    for task_key, task in tasks.items():
        assert task["notebook_task"] == {
            "notebook_path": f"{FILE_PATH}/src/{task_key}", "source": "WORKSPACE",
            "base_parameters": {"catalog": "main", "schema": "guardrail_eval", "prefix": "guardrail_eval",
                                "config": f"{FILE_PATH}/src/eval_config.json"}}
    assert tasks["run_eval"]["depends_on"] == [{"task_key": "load_prompts"}]
    assert tasks["run_eval"]["max_retries"] == 0 and tasks["run_eval"]["disable_auto_optimization"] is True
    assert "${" not in json.dumps(settings)


@pytest.mark.parametrize("change, message", [
    (lambda j: j["tasks"][0].update(spark_python_task={"python_file": "src/x.py"}), "only notebook tasks"),
    (lambda j: j["tasks"][0]["notebook_task"].update(notebook_path="../elsewhere.py"), "must be src/<file>.py"),
    (lambda j: j["tasks"][0]["notebook_task"].update(notebook_path="src/../../x.py"), "must be src/<file>.py"),
    (lambda j: j.update(permissions=[{"group_name": "users", "level": "CAN_VIEW"}]),
     "permissions is applied by the CLI"),
    (lambda j: j.update(description="${resources.jobs.other.id}"), "${resources.jobs.other.id} can't be resolved"),
])
def test_job_settings_stop_on_what_only_the_cli_deploys(notebook_bundle: Path, change, message):
    bundle = job.read_bundle(notebook_bundle.parent / "guardrail-eval")
    change(bundle["resources"]["jobs"]["guardrail_eval"])
    with pytest.raises(SystemExit, match=re.escape(message)):
        job.job_settings(bundle, FILE_PATH)


def test_deploy_imports_the_notebooks_and_creates_then_updates_one_job(notebook_bundle: Path):
    w = FakeWorkspace()
    state, workdir = json.loads(notebook_bundle.read_text()), notebook_bundle.parent
    first = job.deploy(w, state, workdir)
    assert first["action"] == "created" and first["file_path"] == FILE_PATH and first["warnings"] == []
    assert first["url"] == f"https://example.cloud.databricks.com/jobs/{first['job_id']}"
    imported = {path: (body["format"], body.get("language")) for path, body in w.files.items()}
    assert imported == {f"{FILE_PATH}/src/load_prompts": ("SOURCE", "PYTHON"),
                        f"{FILE_PATH}/src/run_eval": ("SOURCE", "PYTHON"),
                        f"{FILE_PATH}/src/eval_config.json": ("AUTO", None)}
    config = json.loads(base64_text(w.files[f"{FILE_PATH}/src/eval_config.json"]["content"]))
    assert config["services"] == [{"name": SERVICE, "policies": EVAL_POLICIES}]
    second = job.deploy(w, state, workdir)
    assert second["action"] == "updated" and second["job_id"] == first["job_id"] and len(w.jobs) == 1


def base64_text(content: str) -> str:
    import base64

    return base64.b64decode(content).decode()


def test_deploy_leaves_other_jobs_of_the_same_name_alone(notebook_bundle: Path):
    w = FakeWorkspace()
    w.jobs[7] = {"name": "guardrail eval guardrail_eval", "tasks": []}  # from an earlier `bundle deploy`, untagged
    out = job.deploy(w, json.loads(notebook_bundle.read_text()), notebook_bundle.parent)
    assert out["action"] == "created" and out["job_id"] != 7 and w.jobs[7] == {"name": "guardrail eval guardrail_eval",
                                                                               "tasks": []}
    assert out["warnings"] == [
        "1 other job(s) are also named 'guardrail eval guardrail_eval': this deploy left them alone"]


def test_run_needs_the_latest_deploy_and_passes_only_and_params(notebook_bundle: Path):
    w = FakeWorkspace()
    state, workdir = json.loads(notebook_bundle.read_text()), notebook_bundle.parent
    with pytest.raises(SystemExit, match="deploy first"):
        job.start(w, state, workdir, "load_prompts", "")
    job.deploy(w, state, workdir)
    job.start(w, state, workdir, "run_eval", "mode=full, run_id=eval-full-202611120900")
    assert w.started[-1] == {"job_id": 101, "job_parameters": {"mode": "full", "run_id": "eval-full-202611120900"},
                             "only": ["run_eval"]}
    job.start(w, state, workdir, "", "")
    assert w.started[-1] == {"job_id": 101}

    notebook = workdir / "guardrail-eval" / "src" / "run_eval.py"
    notebook.write_text(notebook.read_text() + "\nprint('edited in the workspace')\n")
    with pytest.raises(SystemExit, match="The bundle changed since the last deploy"):
        job.start(w, state, workdir, "run_eval", "mode=sample")
    job.deploy(w, state, workdir)
    job.start(w, state, workdir, "run_eval", "mode=sample")
    assert "edited in the workspace" in base64_text(w.files[f"{FILE_PATH}/src/run_eval"]["content"])


@pytest.mark.parametrize("only, params, message", [
    ("chunk", "", "--only: ['chunk'] aren't tasks of the job"),
    ("", "mode", "name=value pairs"),
    ("", "mode=full,limit=5", "--params: ['limit'] aren't parameters of the job"),
])
def test_run_checks_its_tasks_and_parameters(notebook_bundle: Path, only, params, message):
    w = FakeWorkspace()
    state, workdir = json.loads(notebook_bundle.read_text()), notebook_bundle.parent
    job.deploy(w, state, workdir)
    with pytest.raises(SystemExit, match=re.escape(message)):
        job.start(w, state, workdir, only, params)


def test_status_reads_each_tasks_summary_and_wait_stops_at_the_end():
    w = FakeWorkspace()
    summary = {"table": "main.guardrail_eval.guardrail_eval_prompts", "prompts": 6886, "block": 1384, "allow": 5502}
    w.runs[5] = {"run_page_url": "https://example/run/5",
                 "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"},
                 "tasks": [{"task_key": "load_prompts", "run_id": 6,
                            "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}}]}
    w.outputs[6] = {"notebook_output": {"result": json.dumps(summary)}}
    status = job.run_status(w, 5)
    assert status["done"] and status["succeeded"] and status["url"] == "https://example/run/5"
    assert status["tasks"] == [{"task_key": "load_prompts", "state": "TERMINATED", "result": "SUCCESS", "message": None,
                                "output": summary}]

    w.runs[8] = {"state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED"},
                 "tasks": [{"task_key": "run_eval", "run_id": 9, "state": {"life_cycle_state": "TERMINATED",
                                                                           "result_state": "FAILED"}}]}
    w.outputs[9] = {"error": "SystemExit: main.guardrail_eval.guardrail_eval_prompts is empty"}
    failed = job.run_status(w, 8)
    assert failed["done"] and not failed["succeeded"] and "is empty" in failed["tasks"][0]["error"]

    clock = iter([0, 1000, 2000]).__next__  # 20 minutes is 1200 seconds: one poll, then it gives up
    slept = []
    pending = job.wait(w, 12, 20, sleep=slept.append, clock=lambda: clock() * 1.0)
    assert not pending["done"] and slept == [job.POLL_SECONDS]  # gave up after 20 minutes, still pending


def test_job_refuses_to_deploy_a_local_bundle(tmp_path: Path):
    with pytest.raises(SystemExit, match="runtime is local"):
        job.load_state(write_state(tmp_path))

