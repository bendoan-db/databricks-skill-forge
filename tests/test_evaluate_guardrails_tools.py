"""Checks for forge-evaluate-guardrails' local tools and bundle template: guardrail_service.py, evaluate.py, render.py,
and scripts/databricks.yml. No workspace: the REST client is a stand-in. The job notebooks are tested in
test_evaluate_guardrails.py."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS_DIR, load_script

SKILL = "forge-evaluate-guardrails"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
service = load_script(SKILL, "guardrail_service")
evaluate = load_script(SKILL, "evaluate")
render_module = load_script(SKILL, "render")

STATE = {
    "profile": "DEFAULT",
    "catalog": "main",
    "schema": "guardrail_eval",
    "prefix": "guardrail_eval",
    "name": "guardrail-eval",
    "warehouse_id": "abc123",
    "dataset": {"source": "default"},
    "model_service": "main.guardrail_eval.guardrail_eval_model",
    "base_model_service": "system.ai.claude-sonnet-5-5",
    "guardrails": ["sensitive_data", "unsafe_content", "jailbreak"],
    "evaluators": {"unsafe_content": "system.ai.gemma-3-12b", "jailbreak": "system.ai.gpt-oss-20b"},
    "concurrency": 8,
    "max_tokens": 64,
    "sample_size": 100,
    "settle_minutes": 5,
    "experiment": "/Users/someone@example.com/guardrail-eval",
    "targets": {"recall": 0.9, "fp_rate": 0.1},
    "completed": [],
}
DESTINATIONS = [{"name": "claude", "model": "models/system.ai.claude-sonnet-5-5", "is_deleted": False}]
BASE = {"name": "system.ai.claude-sonnet-5-5", "config": {"routing": {"destinations": DESTINATIONS}}}
CATEGORIES = ("class.email_address,class.ip_address,class.mac_address,class.vin,class.credit_card,class.iban_code,"
              "class.phone_number,class.us_ssn,class.us_itin,class.us_passport,class.us_bank_number,class.uk_nhs,"
              "class.uk_nino,class.in_pan,class.in_aadhaar")


# --- guardrail_service.py --------------------------------------------------------------------------------

def test_policy_entries_are_the_built_ins_in_rank_order():
    entries = service.policy_entries(STATE)
    assert entries == [
        {"name": "sensitive_data_detection", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "system.ai.detect_sensitive_data",
         "rank": 1, "options": {"action": "block", "phases": "pre_call,post_call", "dry_run": "false", "categories": CATEGORIES}},
        {"name": "unsafe_content_detection", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "system.ai.block_unsafe_content",
         "rank": 2, "options": {"phases": "pre_call,post_call", "dry_run": "false", "max_turns": "10",
                                "model_service": "model-services/system.ai.gemma-3-12b"}},
        {"name": "jailbreak_detection", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "system.ai.block_jailbreak",
         "rank": 3, "options": {"phases": "pre_call", "dry_run": "false", "max_turns": "10",
                                "model_service": "model-services/system.ai.gpt-oss-20b"}},
    ]
    assert all(isinstance(v, str) for e in entries for v in e["options"].values())  # the API takes strings only
    assert len(entries[0]["options"]["categories"].split(",")) == 15 and " " not in entries[0]["options"]["categories"]
    only = service.policy_entries({**STATE, "guardrails": ["jailbreak", "sensitive_data"]})
    assert [e["name"] for e in only] == ["sensitive_data_detection", "jailbreak_detection"]  # rank order, not list order


def service_with(policies, destinations=DESTINATIONS, comment=None):
    return {"name": STATE["model_service"], "comment": comment or service.COMMENT.format(base=STATE["base_model_service"]),
            "config": {"routing": {"destinations": destinations}, "service_policies": policies}}


def test_plan_creates_a_missing_service():
    p = service.plan(STATE, None, BASE)
    assert not p["exists"] and p["routing_changed"] and p["policies_changed"]
    assert p["steps"][0] == "create main.guardrail_eval.guardrail_eval_model, routed like system.ai.claude-sonnet-5-5"
    assert p["destinations"] == [{"name": "claude", "model": "models/system.ai.claude-sonnet-5-5"}]  # is_deleted dropped
    assert [s.split()[0] for s in p["steps"][1:]] == ["attach", "attach", "attach"]


def test_plan_keeps_a_matching_service_and_detaches_a_stray_policy():
    entries = service.policy_entries(STATE)
    p = service.plan(STATE, service_with(entries), BASE)
    assert not (p["routing_changed"] or p["comment_changed"] or p["policies_changed"])
    assert all(s.startswith("keep") for s in p["steps"])
    stray = {"name": "custom_judge", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "system.ai.invoke_llm_judge", "rank": 5}
    deleted = {**stray, "name": "old", "is_deleted": True}
    p = service.plan(STATE, service_with([*entries, stray, deleted]), BASE)
    assert p["policies_changed"] and "detach custom_judge (not a guardrail this evaluation attaches)" in p["steps"]
    assert not any("old" in s for s in p["steps"])  # a deleted policy isn't live


def test_plan_reports_routing_drift_and_policy_updates():
    entries = service.policy_entries(STATE)
    changed = [dict(entries[0], rank=4), *entries[1:]]
    p = service.plan(STATE, service_with(changed, destinations=[{"name": "other", "model": "models/x"}]), BASE)
    assert p["routing_changed"] and "update main.guardrail_eval.guardrail_eval_model's routing to match " \
        "system.ai.claude-sonnet-5-5" in p["steps"]
    assert "update sensitive_data_detection" in p["steps"]


def test_plan_needs_a_readable_base():
    with pytest.raises(ValueError, match="isn't a model service you can read"):
        service.plan(STATE, None, None)


class NotFound(Exception):
    """Stands in for databricks.sdk.errors.NotFound, which guardrail_service matches by name."""


class FakeClient:
    """Stands in for WorkspaceClient().api_client: records calls, and applies them to the services it holds."""

    def __init__(self, services: dict):
        self.services = services
        self.calls = []

    def do(self, method, path, query=None, body=None):
        body = json.loads(json.dumps(body)) if body is not None else None  # the caller's body, as sent
        self.calls.append((method, path, query, body))
        api = service.API
        if method == "GET":
            name = path.removeprefix(api + "/")
            if name not in self.services:
                raise NotFound(name)
            return json.loads(json.dumps(self.services[name]))
        if method == "POST" and path == api:
            name = f"{query['parent'].removeprefix('schemas/')}.{query['model_service_id']}"
            self.services[name] = {"name": name, "update_time": "2026-11-02T00:00:00Z", **json.loads(json.dumps(body))}
            return self.services[name]
        if method == "PATCH":
            name = path.removeprefix(api + "/")
            config = self.services[name].setdefault("config", {})
            if query["update_mask"] == "config.service_policies":
                config["service_policies"] = body["config"]["service_policies"]
            else:
                self.services[name]["comment"] = body["comment"]
                config["routing"] = body["config"]["routing"]
            return self.services[name]
        raise AssertionError(f"unexpected call {method} {path}")


def test_apply_creates_then_attaches_then_reads_back():
    client = FakeClient({"system.ai.claude-sonnet-5-5": BASE})
    current, base = service.read(client, STATE)
    assert current is None and base == BASE
    p = service.plan(STATE, current, base)
    client.calls.clear()
    result = service.apply(client, p)
    assert [(m, path, q) for m, path, q, _ in client.calls] == [
        ("POST", service.API, {"parent": "schemas/main.guardrail_eval", "model_service_id": "guardrail_eval_model"}),
        ("PATCH", f"{service.API}/main.guardrail_eval.guardrail_eval_model", {"update_mask": "config.service_policies"}),
        ("GET", f"{service.API}/main.guardrail_eval.guardrail_eval_model", None),
    ]
    created = {"destinations": [{"name": "claude", "model": DESTINATIONS[0]["model"]}]}
    assert client.calls[0][3] == {"comment": p["comment"], "config": {"routing": created}}
    assert client.calls[1][3] == {"config": {"service_policies": service.policy_entries(STATE)}}
    assert [x["name"] for x in result["config"]["service_policies"]] == [
        "sensitive_data_detection", "unsafe_content_detection", "jailbreak_detection"]


def test_apply_updates_routing_and_skips_unchanged_policies():
    entries = service.policy_entries(STATE)
    client = FakeClient({"system.ai.claude-sonnet-5-5": BASE,
                         STATE["model_service"]: service_with(entries, destinations=[{"name": "old", "model": "models/x"}])})
    p = service.plan(STATE, *service.read(client, STATE))
    client.calls.clear()
    service.apply(client, p)
    assert [(m, q) for m, _, q, _ in client.calls] == [
        ("PATCH", {"update_mask": "comment,config.routing.destinations"}), ("GET", None)]


def test_verify_names_the_policies_that_differ():
    p = service.plan(STATE, None, BASE)
    entries = service.policy_entries(STATE)
    wrong = [entries[0], dict(entries[1], options={**entries[1]["options"], "dry_run": "true"}),
             {"name": "extra", "rank": 9}]
    with pytest.raises(RuntimeError, match=r"\['extra', 'jailbreak_detection', 'unsafe_content_detection'\]"):
        service.verify(p, service_with(wrong))
    service.verify(p, service_with(entries))


def gateway_error(name: str, message: str, status: int) -> Exception:
    """An exception named like the SDK's (guardrail_service matches error classes by name), with a status code."""
    error = type(name, (Exception,), {})(message)
    error.status_code = status
    return error


class GatewayClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def do(self, method, path, query=None, body=None):
        assert (method, path) == ("POST", service.GATEWAY) and body["messages"][0]["role"] == "user"
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


ALLOWED = {"choices": [{"message": {"content": "Paris."}, "finish_reason": "stop"}]}
BLOCKED = {"choices": [{"message": {"content": "Blocked by policy."}, "finish_reason": "content_filter"}],
           "databricks_service_policy": {"name": "sensitive_data_detection", "action": "deny", "phase": "pre_call",
                                         "reason": "Detected sensitive data (US_SSN)"}}


def test_call_reads_a_block_and_a_pass():
    out = service.call(GatewayClient(BLOCKED), "m", "p", 16)
    assert out["blocked"] and out["policy"] == "sensitive_data_detection" and out["phase"] == "pre_call"
    out = service.call(GatewayClient(ALLOWED), "m", "p", 16)
    assert not out["blocked"] and out["content"] == "Paris." and out["http"] == 200


def test_call_fails_closed_retries_rate_limits_and_records_errors():
    failed = gateway_error("InternalError", "Input policy 'jailbreak_detection' failed to evaluate", 500)
    client = GatewayClient(failed)
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert out["blocked"] and out["policy"] == "jailbreak_detection" and client.calls == 1  # not retried
    limited = gateway_error("InternalError", "Input policy 'jailbreak_detection' failed to evaluate: 429 rate limit", 500)
    client = GatewayClient(limited, ALLOWED)
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert not out["blocked"] and out["error"] is None and client.calls == 2  # a rate limit is retried
    client = GatewayClient(gateway_error("PermissionDenied", "no EXECUTE", 403))
    out = service.call(client, "m", "p", 16, sleep=lambda s: None)
    assert not out["blocked"] and out["http"] == 403 and "no EXECUTE" in out["error"] and client.calls == 1


def test_check_outcomes():
    ok = {"blocked": False, "error": None}
    ssn = {"blocked": True, "policy": "sensitive_data_detection", "error": None}
    assert service.check_outcomes(STATE, ok, ssn) == []
    assert "wait, then run --check again" in service.check_outcomes(STATE, ok, {"blocked": False, "error": None})[0]
    other = dict(ssn, policy="unsafe_content_detection")
    assert "blocked by unsafe_content_detection" in service.check_outcomes(STATE, ok, other)[0]
    assert "benign prompt was blocked" in service.check_outcomes(STATE, dict(ssn), ssn)[0]
    assert service.check_outcomes({**STATE, "guardrails": ["jailbreak"]}, ok, None) == []


def test_state_errors():
    assert service.state_errors(STATE) == []
    errors = service.state_errors({**STATE, "model_service": "svc", "evaluators": {}, "guardrails": ["jailbreak"]})
    assert any("model_service" in e for e in errors) and any("evaluators.jailbreak" in e for e in errors)


# --- evaluate.py -----------------------------------------------------------------------------------------

def metric(breakdown, group, **values):
    row = {"run_id": "eval-full-202611021200", "mode": "full", "breakdown": breakdown, "dataset": "all", "group_name": group}
    row.update(dict.fromkeys(evaluate.NUMERIC))
    row.update(values)
    return row


ROWS = [
    metric("overall", "all", n=1000, tp=450, fn=50, fp=40, tn=455, errors=5, recall=0.9, precision=0.918, fp_rate=0.081,
           f1=0.909, accuracy=0.905, latency_p50_ms=1300.0, latency_p95_ms=5000.0),
    metric("dataset", "aegis2", n=400, recall=0.95, fp_rate=0.05, errors=2),
    metric("category", "Violence", dataset="aegis2", n=50, recall=0.98),
    metric("category", "Hate/Identity Hate", dataset="aegis2", n=40, recall=0.8),
    metric("policy", "unsafe_content_detection", blocked=300, tp=290, fp=10),
]


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


def test_report():
    text = evaluate.report(ROWS, {"recall": 0.95})
    assert "Run eval-full-202611021200 (full): 1000 prompts, 5 errors, scored rate 99.5%" in text
    assert "| aegis2 | 400 | 95.0% |" in text and "| unsafe_content_detection | 300 | 10 |" in text
    assert text.index("Hate/Identity Hate") < text.index("Violence")  # lowest recall first
    assert "| recall | ≥ 95.0% | 90.0% | no |" in text


def test_typed_metrics_rows():
    row = evaluate.typed({"n": "10", "recall": "0.5", "fp": None, "group_name": "x"})
    assert row == {"n": 10, "recall": 0.5, "fp": None, "group_name": "x"}


# --- render.py and the bundle template -------------------------------------------------------------------

NOTEBOOK = "# Databricks notebook source\n# MAGIC %md\n# MAGIC # {title}\n\n# COMMAND ----------\n\nprint('{title}')\n"


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


def write_state(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "work" / "guardrail-eval.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({**STATE, **overrides}))
    return path


def render(scripts: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(scripts / "render.py"), *args], capture_output=True, text=True,
                          check=False)


def test_render_writes_the_bundle_and_config(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path)
    result = render(scripts, "--state", str(state))
    assert result.returncode == 0, result.stderr
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
    assert config.pop("_rendered_by").startswith("forge-evaluate-guardrails render.py from guardrail-eval.json (sha256:")
    assert config == {"dataset": {"source": "default"}, "model_service": "main.guardrail_eval.guardrail_eval_model",
                      "policies": ["sensitive_data_detection", "unsafe_content_detection", "jailbreak_detection"],
                      "concurrency": 8, "max_tokens": 64, "sample_size": 100, "settle_minutes": 5,
                      "experiment": "/Users/someone@example.com/guardrail-eval"}
    copy = (bundle / "src" / "run_eval.py").read_text().splitlines()
    assert copy[0] == "# Databricks notebook source" and copy[1].startswith("# Copied from forge-evaluate-guardrails")
    assert set(json.loads(render(scripts, "--state", str(state)).stdout)["unchanged"]) == {
        str(bundle / "databricks.yml"), str(bundle / "src" / "eval_config.json"), str(bundle / "src" / "load_prompts.py"),
        str(bundle / "src" / "run_eval.py")}


def test_render_protects_a_hand_edited_config_and_keeps_an_edited_notebook(scripts: Path, tmp_path: Path):
    state = write_state(tmp_path)
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
    ({"model_service": "svc"}, "model_service="),
    ({"experiment": "guardrail-eval"}, "experiment="),
    ({"guardrails": []}, "guardrails is missing"),
    ({"guardrails": ["toxicity"]}, "guardrails="),
    ({"evaluators": {"unsafe_content": "system.ai.gemma-3-12b"}}, "evaluators.jailbreak"),
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
    result = render(scripts, "--state", str(write_state(tmp_path, **change)))
    assert result.returncode == 1 and message in result.stderr, result.stderr


def test_render_accepts_a_user_dataset(scripts: Path, tmp_path: Path):
    dataset = {"source": "file", "path": "/Volumes/main/guardrail_eval/inbox/prompts.jsonl", "format": "jsonl",
               "prompt_column": "text", "label_column": "verdict", "block_values": ["unsafe", 1],
               "category_column": "topic", "name": "support_tickets"}
    state = write_state(tmp_path, dataset=dataset, guardrails=["sensitive_data"], evaluators={})
    assert render(scripts, "--state", str(state)).returncode == 0
    config = json.loads((state.parent / "guardrail-eval" / "src" / "eval_config.json").read_text())
    assert config["dataset"] == dataset and config["policies"] == ["sensitive_data_detection"]


def test_render_lists_every_problem_at_once():
    errors = render_module.dataset_errors({"source": "table", "table": "a.b.c"})
    assert len(errors) == 3  # prompt_column, label_column, block_values
