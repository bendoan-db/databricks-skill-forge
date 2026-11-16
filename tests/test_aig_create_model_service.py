"""Checks for forge-aig-create-model-service's scripts: render.py, deploy_services.py, grant_access.py, check_access.py,
and evaluate.py. No workspace: FakeClient stands in for the REST API, keeping the model services, grants, and
functions it's told about, so a plan, an apply, and a second plan can run against it."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS_DIR, load_script

SKILL = "forge-aig-create-model-service"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
render = load_script(SKILL, "render")
deploy = load_script(SKILL, "deploy_services")
grant = load_script(SKILL, "grant_access")
access = load_script(SKILL, "check_access")
evaluate = load_script(SKILL, "evaluate")

STATE = {"profile": "DEFAULT", "target_schema": "main.ai_gateway", "catalog": "main", "schema": "ai_gateway",
         "parts": ["model_services", "service_policies", "grants"], "config_dir": "ai-gateway",
         "default_evaluator": "system.ai.gpt-oss-20b", "warehouse_id": "wh1", "completed": []}
SERVICES = {
    "catalog": "main", "schema": "ai_gateway",
    "rate_limits": {"requests_per_minute": 60, "tokens_per_minute": 100000},
    "model_services": [
        {"name": "team-chat", "description": "Chat for the team.",
         "destinations": [{"provider": "databricks", "destination": "system.ai.databricks-claude-sonnet-5-5"}],
         "fallbacks": [{"provider": "databricks", "destination": "system.ai.gpt-oss-120b"}],
         "enable_inference_table": True, "enable_rate_limits": True,
         "service_policies": ["jailbreak_detection", "pii_redaction", "block_codenames"]},
        {"name": "split", "description": "Split traffic.",
         "destinations": [
             {"provider": "databricks", "destination": "system.ai.databricks-claude-opus-5-5", "traffic_percentage": 70},
             {"provider": "databricks", "destination": "system.ai.databricks-claude-sonnet-5-5", "traffic_percentage": 30}],
         "enable_inference_table": False, "enable_rate_limits": True,
         "rate_limits": {"requests_per_minute": 120}, "service_policies": ["regulated_advice"]},
    ],
}
POLICIES = {
    "default_evaluator": "system.ai.gpt-oss-20b",
    "service_policies": [
        {"name": "jailbreak_detection", "type": "block_jailbreak"},
        {"name": "pii_redaction", "type": "detect_sensitive_data", "action": "redact", "categories": ["credit_card", "us_ssn"]},
        {"name": "regulated_advice", "type": "llm_judge", "mode": "log", "instruction": "Flag individual tax advice.\n"},
        {"name": "block_codenames", "type": "sql", "description": "No codenames.",
         "sql": "CASE WHEN CONTAINS(LOWER(CAST(COALESCE(event:context.message, '') AS STRING)), 'alpha') "
                "THEN to_variant_object(named_struct('result', 'DENY')) ELSE to_variant_object(named_struct('result', 'ALLOW')) END"},
    ],
}
PERMISSIONS = {
    "groups": [{"name": "data-science", "model_services": ["main.ai_gateway.team-chat", "main.ai_gateway.split"]},
               {"name": "gateway-admins", "model_services": ["main.ai_gateway.team-chat"], "privileges": ["EXECUTE", "MANAGE"]}],
    "users": [{"name": "first.last@example.com", "model_services": ["main.ai_gateway.split"]}],
}
MODELS = ["system.ai.databricks-claude-sonnet-5-5", "system.ai.databricks-claude-opus-5-5", "system.ai.gpt-oss-120b"]


class NotFound(Exception):
    """Stands in for databricks.sdk.errors.NotFound; the scripts match SDK errors by name."""


class PermissionDenied(Exception):
    pass


class TooManyRequests(Exception):
    pass


class FakeClient:
    """Answers like the REST API, from what it holds: objects by path, effective grants, SCIM principals."""

    def __init__(self):
        self.objects = {f"{deploy.SCHEMAS_API}/main.ai_gateway": {"full_name": "main.ai_gateway", "owner": "admins"},
                        f"{access.UC}/catalogs/main": {"name": "main", "owner": "admins"},
                        f"{deploy.API}/system.ai.gpt-oss-20b": {"name": "system.ai.gpt-oss-20b"},
                        **{f"{deploy.MODELS_API}/{m}": {"full_name": m} for m in MODELS}}
        self.grants: dict[tuple[str, str], dict[str, set[str]]] = {}  # (securable, name) -> principal -> privileges
        self.unreadable: set[tuple[str, str]] = set()
        self.groups = {"data-science": "Group", "gateway-admins": "Group", "local-team": "WorkspaceGroup"}
        self.users = {"first.last@example.com", "me@example.com"}
        self.me = {"userName": "me@example.com", "groups": [{"display": "ml-engineers"}]}
        self.gateway: list = []  # responses (or exceptions) for gateway calls, in order
        self.calls: list[tuple] = []

    def do(self, method, path, query=None, body=None):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if method == "GET":
            return self.get(path, query or {})
        if method == "POST" and path == deploy.API:
            name = f"{query['parent'].removeprefix('schemas/')}.{query['model_service_id']}"
            self.objects[f"{deploy.API}/{name}"] = {"name": name, "owner": self.me["userName"], **copy.deepcopy(body)}
            return {}
        if method == "PATCH" and path.startswith(deploy.API + "/"):
            service = self.objects[path]
            for field in query["update_mask"].split(","):
                *parents, last = field.split(".")
                src, dst = body, service
                for part in parents:
                    src, dst = (src or {}).get(part) or {}, dst.setdefault(part, {})
                if last in (src or {}):
                    dst[last] = copy.deepcopy(src[last])
                else:
                    dst.pop(last, None)
            return {}
        if method == "POST" and path == deploy.STATEMENTS_API:
            return {"statement_id": "s1", "status": {"state": "SUCCEEDED"}}
        if method == "PATCH" and path.startswith(grant.PERMISSIONS_API):
            securable, name = path.removeprefix(grant.PERMISSIONS_API + "/").split("/", 1)
            for change in body["changes"]:
                self.grants.setdefault((securable, name), {}).setdefault(change["principal"], set()).update(change["add"])
            return {}
        if method == "POST" and path == evaluate.GATEWAY:
            response = self.gateway.pop(0) if self.gateway else {"choices": [{"message": {"content": "A platform."}}]}
            if isinstance(response, Exception):
                raise response
            return response
        raise AssertionError(f"unexpected call {method} {path}")

    def get(self, path, query):
        if path.startswith(f"{access.UC}/effective-permissions/"):
            securable, name = path.removeprefix(f"{access.UC}/effective-permissions/").split("/", 1)
            if (securable, name) in self.unreadable:
                raise PermissionDenied(path)
            assignments = []
            for key, inherited in self.inherited(securable, name):
                for principal, privileges in self.grants.get(key, {}).items():
                    if query.get("principal") in (None, principal):
                        assignments.append({"principal": principal, "privileges": [
                            {"privilege": p, **inherited} for p in sorted(privileges)]})
            return {"privilege_assignments": assignments}
        if path == access.SCIM_ME:
            return copy.deepcopy(self.me)
        if path.startswith(grant.SCIM + "/"):
            resource = path.removeprefix(grant.SCIM + "/")
            value = query["filter"].split('"')[1]
            if resource == "Groups" and value in self.groups:
                return {"Resources": [{"displayName": value, "meta": {"resourceType": self.groups[value]}}]}
            if resource == "Users" and value in self.users:
                return {"Resources": [{"userName": value}]}
            return {"Resources": []}
        if path in self.objects:
            return copy.deepcopy(self.objects[path])
        raise NotFound(path)

    def inherited(self, securable, name):
        """The object, then its schema and catalog, as the effective-permissions API reports them."""
        yield (securable, name), {}
        if securable not in ("catalog", "schema"):
            yield ("schema", name.rsplit(".", 1)[0]), {"inherited_from_type": "SCHEMA",
                                                      "inherited_from_name": name.rsplit(".", 1)[0]}
        if securable != "catalog":
            yield ("catalog", name.split(".")[0]), {"inherited_from_type": "CATALOG", "inherited_from_name": name.split(".")[0]}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def workdir(tmp_path: Path, services=SERVICES, policies=POLICIES, permissions=PERMISSIONS, **state) -> Path:
    path = tmp_path / "aig-model-services.json"
    path.write_text(json.dumps(STATE | state))
    folder = tmp_path / "ai-gateway"
    folder.mkdir(exist_ok=True)
    for name, data in (("model-services.yaml", services), ("service-policies.yaml", policies),
                       ("permissions.yaml", permissions)):
        if data is not None:
            (folder / name).write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *args], capture_output=True, text=True, check=False)


# ── render.py ────────────────────────────────────────────────────────────────────


def test_render_writes_the_templates_once(tmp_path: Path):
    state = tmp_path / "aig-model-services.json"
    state.write_text(json.dumps(STATE))
    first = run("render.py", "--state", str(state))
    assert first.returncode == 0, first.stderr
    summary = json.loads(first.stdout)
    folder = tmp_path / "ai-gateway"
    assert sorted(Path(p).name for p in summary["written"]) == ["model-services.yaml", "permissions.yaml",
                                                                 "service-policies.yaml"]
    services = yaml.safe_load((folder / "model-services.yaml").read_text())
    assert (services["catalog"], services["schema"]) == ("main", "ai_gateway")
    assert services["model_services"][0]["service_policies"] == ["jailbreak_detection", "pii_redaction"]
    assert yaml.safe_load((folder / "service-policies.yaml").read_text())["default_evaluator"] == "system.ai.gpt-oss-20b"
    assert summary["to_fill"]["model-services.yaml"] == ["<service name>", "<what it's for and who uses it>"]
    assert summary["to_fill"]["permissions.yaml"] == ["<account group>", "main.ai_gateway.<service name>"]
    (folder / "permissions.yaml").write_text("groups: []\n")
    again = json.loads(run("render.py", "--state", str(state)).stdout)
    assert again["written"] == [] and len(again["kept"]) == 3  # the user's files are never replaced
    assert (folder / "permissions.yaml").read_text() == "groups: []\n"


def test_render_writes_only_the_parts_asked_for(tmp_path: Path):
    state = tmp_path / "aig-model-services.json"
    state.write_text(json.dumps(STATE | {"parts": ["model_services"]}))
    summary = json.loads(run("render.py", "--state", str(state)).stdout)
    assert [Path(p).name for p in summary["written"]] == ["model-services.yaml"]
    services = yaml.safe_load((tmp_path / "ai-gateway" / "model-services.yaml").read_text())
    assert services["model_services"][0]["service_policies"] == []
    state.write_text(json.dumps(STATE | {"parts": ["service_policies", "grants"]}))
    result = run("render.py", "--state", str(state))
    assert result.returncode == 1 and "service_policies needs model_services" in result.stderr


def test_filled_templates_pass_the_plan_checks():
    """The templates, once their <...> values are filled in, are valid input for the scripts."""
    state = STATE
    text = {name: render.render(name, state) for name in render.TEMPLATES.values()}
    filled = {name: yaml.safe_load(t.replace("<service name>", "team-chat").replace(
        "<what it's for and who uses it>", "Chat.").replace("<account group>", "data-science")) for name, t in text.items()}
    policies = filled["service-policies.yaml"]
    defined = {p["name"]: p for p in policies["service_policies"]}
    errors, _ = deploy.check_services(filled["model-services.yaml"], defined, policies_on=True)
    assert errors == []
    errors, _ = deploy.check_policies(policies, {"jailbreak_detection", "pii_redaction"})
    assert errors == []
    assert deploy.placeholders(filled["model-services.yaml"], "x") == []
    found, errors = grant.entries(filled["permissions.yaml"])
    assert errors == [] and found[0]["services"] == ["main.ai_gateway.team-chat"]


# ── deploy_services.py: the checks ──────────────────────────────────────────────


@pytest.mark.parametrize("change, expected", [
    (lambda s, p: s["model_services"][0].update(name="<service name>"), "is still '<service name>'"),
    (lambda s, p: s["model_services"][1]["destinations"][0].update(traffic_percentage=60), "adding up to 100"),
    (lambda s, p: s["model_services"][0]["destinations"][0].update(provider="openai"), "provider 'openai'"),
    (lambda s, p: s["model_services"][0]["fallbacks"][0].update(traffic_percentage=10), "take no traffic_percentage"),
    (lambda s, p: s["model_services"][0]["service_policies"].append("nope"), "'nope' isn't defined"),
    (lambda s, p: s["model_services"].append(copy.deepcopy(s["model_services"][0])), "listed more than once"),
    (lambda s, p: s.pop("rate_limits"), "enable_rate_limits needs rate_limits"),
    (lambda s, p: p["service_policies"][0].update(phases=["input", "output"]), "runs on ['input'] only"),
    (lambda s, p: p["service_policies"][1].pop("categories"), "categories is required"),
    (lambda s, p: p["service_policies"][1].update(categories=["passport"]), "unknown categories ['passport']"),
    (lambda s, p: p["service_policies"][2].update(instruction=" "), "needs instruction"),
    (lambda s, p: p["service_policies"][3].update(sql="ai_query('m', 'x')"), "uses ai_query"),
    (lambda s, p: p["service_policies"][3].update(sql="COALESCE(event:a, event:b, '')"), "COALESCE with more than two"),
    (lambda s, p: p.pop("default_evaluator"), "needs an evaluator"),
])
def test_plan_rejects_bad_files(change, expected):
    services, policies = copy.deepcopy(SERVICES), copy.deepcopy(POLICIES)
    change(services, policies)
    p = deploy.plan(FakeClient(), STATE, services, policies)
    assert any(expected in e for e in p["errors"]), p["errors"]
    assert p["services"] == []  # nothing is planned while a file has errors


def test_plan_checks_the_workspace():
    client = FakeClient()
    services = copy.deepcopy(SERVICES)
    services["model_services"][0]["destinations"][0]["destination"] = "system.ai.claude-sonnet-5-5"  # a service name
    policies = copy.deepcopy(POLICIES)
    policies["default_evaluator"] = "system.ai.databricks-gpt-oss-20b"  # a model, not a model service
    client.objects[f"{deploy.TABLES_API}/main.ai_gateway.team-chat_payload"] = {"full_name": "x"}
    errors = "\n".join(deploy.plan(client, STATE, services, policies)["errors"])
    assert ("destination system.ai.claude-sonnet-5-5 isn't a Unity Catalog model; system.ai.claude-sonnet-5-5 is a "
            "model service's name, and its model is system.ai.databricks-claude-sonnet-5-5") in errors
    assert "evaluator system.ai.databricks-gpt-oss-20b isn't a model service; evaluators are model services" in errors
    assert "inference table main.ai_gateway.team-chat_payload already exists" in errors
    assert not client.writes()


def test_policies_use_the_wire_format_the_ui_writes():
    """The format in unity-ai-gateway-utilities' skill: every option a string, redact sent as transform."""
    built = {p["name"]: deploy.build_policy(SERVICES, POLICIES, p["name"], i)
             for i, p in enumerate(POLICIES["service_policies"], 1)}
    assert built["jailbreak_detection"] == {
        "name": "jailbreak_detection", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "system.ai.block_jailbreak",
        "rank": 1, "options": {"phases": "pre_call", "dry_run": "false", "max_turns": "10",
                               "model_service": "model-services/system.ai.gpt-oss-20b"}}
    assert built["pii_redaction"]["options"] == {"phases": "pre_call,post_call", "dry_run": "false",
                                                 "action": "transform", "categories": "class.credit_card,class.us_ssn"}
    assert built["regulated_advice"]["handler"] == "system.ai.invoke_llm_judge"
    assert built["regulated_advice"]["options"] == {
        "phases": "pre_call", "dry_run": "true", "max_turns": "10", "model_service": "model-services/system.ai.gpt-oss-20b",
        "action": "block", "instruction": "Flag individual tax advice."}
    assert built["block_codenames"] == {"name": "block_codenames", "policy_type": "POLICY_TYPE_CUSTOM",
                                        "handler": "functions/main.ai_gateway.block_codenames", "rank": 4}
    statement = deploy.sql_function(SERVICES, POLICIES["service_policies"][3])
    assert statement.startswith("CREATE OR REPLACE FUNCTION `main`.`ai_gateway`.`block_codenames`(event VARIANT)\n")
    assert "COMMENT 'No codenames.'" in statement


# ── deploy_services.py: plan, apply, plan again ─────────────────────────────────


def test_create_then_nothing_left_to_do():
    client = FakeClient()
    p = deploy.plan(client, STATE, SERVICES, POLICIES)
    assert p["errors"] == [] and not client.writes()
    assert [(s["name"], s["action"]) for s in p["services"]] == [("main.ai_gateway.team-chat", "create"),
                                                                  ("main.ai_gateway.split", "create")]
    assert p["functions"] == [{"name": "main.ai_gateway.block_codenames", "action": "create or replace"}]
    result = deploy.apply(client, STATE, SERVICES, POLICIES, p, sleep=lambda _: None)
    assert result["failed"] == [] and len(result["applied"]) == 2
    created = client.objects[f"{deploy.API}/main.ai_gateway.team-chat"]
    assert created["config"]["inference_table"] == {"parent": "schemas/main.ai_gateway", "table_name_prefix": "team-chat"}
    assert created["config"]["rate_limits"] == [{"key": "RATE_LIMIT_KEY_USER_DEFAULT", "renewal_period":
                                                 "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", "requests": 60, "tokens": 100000}]
    assert [x["name"] for x in created["config"]["service_policies"]] == ["jailbreak_detection", "pii_redaction",
                                                                         "block_codenames"]
    split = client.objects[f"{deploy.API}/main.ai_gateway.split"]["config"]
    assert [d["traffic_percentage"] for d in split["routing"]["destinations"]] == [70, 30] and "inference_table" not in split
    statements = [c for c in client.calls if c[1] == deploy.STATEMENTS_API]
    assert len(statements) == 1 and statements[0][3]["warehouse_id"] == "wh1"

    again = deploy.plan(client, STATE, SERVICES, POLICIES)
    by_name = {s["name"]: s for s in again["services"]}
    assert by_name["main.ai_gateway.split"]["action"] == "unchanged"
    team = by_name["main.ai_gateway.team-chat"]  # its SQL policy's function is replaced, so the policy is re-attached
    assert team["changes"] == [] and "re-attach block_codenames" in team["policy_steps"] and team["matches"]
    assert all(s["matches"] for s in again["services"])


def test_update_sends_only_what_changed():
    client = FakeClient()
    deploy.apply(client, STATE, SERVICES, POLICIES, deploy.plan(client, STATE, SERVICES, POLICIES), sleep=lambda _: None)
    services = copy.deepcopy(SERVICES)
    services["model_services"][1]["rate_limits"] = {"requests_per_minute": 30}
    services["model_services"][1]["service_policies"] = []
    p = deploy.plan(client, STATE, services, POLICIES)
    split = next(s for s in p["services"] if s["name"] == "main.ai_gateway.split")
    assert split["action"] == "update" and split["changes"] == ["config.rate_limits"]
    assert split["policy_steps"] == ["detach regulated_advice"] and not split["matches"]
    before = len(client.calls)
    result = deploy.apply(client, STATE, services, POLICIES, p, sleep=lambda _: None)
    assert result["failed"] == []
    masks = [c[2]["update_mask"] for c in client.calls[before:] if c[0] == "PATCH" and c[1].endswith(".split")]
    assert masks == ["config.rate_limits", "config.service_policies"]
    assert client.objects[f"{deploy.API}/main.ai_gateway.split"]["config"]["service_policies"] == []


def test_policies_are_left_alone_without_the_part():
    client = FakeClient()
    deploy.apply(client, STATE, SERVICES, POLICIES, deploy.plan(client, STATE, SERVICES, POLICIES), sleep=lambda _: None)
    state = STATE | {"parts": ["model_services"]}
    services = copy.deepcopy(SERVICES)
    services["model_services"][1]["description"] = "Changed."
    p = deploy.plan(client, state, services, None)
    assert any("service_policies isn't in parts" in w for w in p["warnings"])
    before = len(client.calls)
    deploy.apply(client, state, services, None, p, sleep=lambda _: None)
    masks = [c[2]["update_mask"] for c in client.calls[before:] if c[0] == "PATCH"]
    assert masks == ["comment"]  # the policies attached earlier stay
    assert client.objects[f"{deploy.API}/main.ai_gateway.split"]["config"]["service_policies"]


def test_inference_table_cant_move():
    client = FakeClient()
    deploy.apply(client, STATE, SERVICES, POLICIES, deploy.plan(client, STATE, SERVICES, POLICIES), sleep=lambda _: None)
    client.objects[f"{deploy.API}/main.ai_gateway.team-chat"]["config"]["inference_table"]["table_name_prefix"] = "old"
    errors = "\n".join(deploy.plan(client, STATE, SERVICES, POLICIES)["errors"])
    assert "main.ai_gateway.team-chat: its inference table can't change after it's created" in errors


def test_a_failed_function_skips_only_its_services():
    client = FakeClient()
    p = deploy.plan(client, STATE, SERVICES, POLICIES)
    original = client.do

    def failing(method, path, query=None, body=None):
        if path == deploy.STATEMENTS_API:
            return {"statement_id": "s1", "status": {"state": "FAILED", "error": {"message": "PARSE_SYNTAX_ERROR"}}}
        return original(method, path, query=query, body=body)

    client.do = failing
    result = deploy.apply(client, STATE, SERVICES, POLICIES, p, sleep=lambda _: None)
    failed = {f["name"]: f["error"] for f in result["failed"]}
    assert "PARSE_SYNTAX_ERROR" in failed["main.ai_gateway.block_codenames"]
    assert "skipped" in failed["main.ai_gateway.team-chat"]
    assert [a["name"] for a in result["applied"]] == ["main.ai_gateway.split"]


# ── grant_access.py ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("change, expected", [
    (lambda p: p["groups"][0].update(name="<account group>"), "is still '<account group>'"),
    (lambda p: p["groups"][0]["model_services"].append("main.<service name>"), "still has a placeholder"),
    (lambda p: p["groups"][0]["model_services"].append("team-chat"), "isn't a three-part name"),
    (lambda p: p["groups"][0].update(privileges=["QUERY"]), "unknown privileges ['QUERY']"),
    (lambda p: p["users"][0].update(name="someone"), "a user's email, or a service principal's application ID"),
])
def test_grant_entries_are_checked(change, expected):
    permissions = copy.deepcopy(PERMISSIONS)
    change(permissions)
    assert any(expected in e for e in grant.entries(permissions)[1])


def test_grants_plan_only_whats_missing():
    client = FakeClient()
    deploy.apply(client, STATE, SERVICES, POLICIES, deploy.plan(client, STATE, SERVICES, POLICIES), sleep=lambda _: None)
    client.grants[("catalog", "main")] = {"data-science": {"USE_CATALOG"}}
    client.grants[("schema", "main.ai_gateway")] = {"gateway-admins": {"ALL_PRIVILEGES"}}
    p = grant.plan(client, STATE, PERMISSIONS, SERVICES)
    assert p["errors"] == [] and p["changes"]
    plan = {(g["principal"], g["securable"], g["name"]): g for g in p["grants"]}
    assert plan[("data-science", "catalog", "main")]["status"] == "ok"
    assert plan[("data-science", "schema", "main.ai_gateway")]["add"] == ["USE_SCHEMA"]
    admins = plan[("gateway-admins", "model_service", "main.ai_gateway.team-chat")]
    assert admins["add"] == ["MANAGE"]  # ALL PRIVILEGES, inherited from the schema, covers EXECUTE but not MANAGE
    result = grant.apply(client, p)
    assert result["failed"] == [] and result["not_visible_yet"] == []
    assert not grant.plan(client, STATE, PERMISSIONS, SERVICES)["changes"]


def test_grants_wait_for_the_services_a_deploy_creates():
    client = FakeClient()
    p = grant.plan(client, STATE, PERMISSIONS, SERVICES)
    statuses = {g["status"] for g in p["grants"] if g["securable"] == "model_service"}
    assert p["errors"] == [] and statuses == {"after deploy"}
    result = grant.apply(client, p)
    assert any("run deploy_services.py --apply first" in f["error"] for f in result["failed"])
    elsewhere = {"groups": [{"name": "data-science", "model_services": ["other.schema.nope"]}]}
    assert "other.schema.nope doesn't exist, and model-services.yaml doesn't create it" in grant.plan(
        client, STATE, elsewhere, SERVICES)["errors"]


def test_workspace_local_groups_cant_get_grants():
    client = FakeClient()
    permissions = {"groups": [{"name": "local-team", "model_services": ["main.ai_gateway.team-chat"]},
                              {"name": "not-here", "model_services": ["main.ai_gateway.team-chat"]}]}
    p = grant.plan(client, STATE, permissions, SERVICES)
    assert any("local-team is workspace-local" in e for e in p["errors"])
    assert any("not-here isn't in this workspace" in w for w in p["warnings"])


# ── check_access.py ──────────────────────────────────────────────────────────────


def checks(client, needs) -> dict[tuple[str, str], dict]:
    ctx = access.Context(client)
    return {(c["on"], c["needs"]): c for c in (access.check(ctx, item) for item in needs.items.values())}


def test_preflight_checks_the_target_schema():
    client = FakeClient()
    client.grants[("catalog", "main")] = {"ml-engineers": {"USE_CATALOG", "USE_SCHEMA"}}
    client.grants[("schema", "main.ai_gateway")] = {"me@example.com": {"CREATE_SERVICE"}}
    client.grants[("model_service", "system.ai.gpt-oss-20b")] = {"account users": {"EXECUTE"}}
    found = checks(client, access.preflight_needs(STATE))
    assert found[("schema main.ai_gateway", "USE SCHEMA")]["via"] == "granted to ml-engineers (from catalog main)"
    assert found[("schema main.ai_gateway", "CREATE SERVICE")]["status"] == "held"
    assert found[("schema main.ai_gateway", "CREATE TABLE")]["status"] == "missing"
    assert not found[("schema main.ai_gateway", "CREATE TABLE")]["required"]  # only for services with an inference table
    assert found[("model service system.ai.gpt-oss-20b", "EXECUTE")]["via"] == "granted to account users"
    assert found[("catalog main", "GRANT (MANAGE or ownership)")]["status"] == "missing"
    names = {k[1] for k in checks(client, access.preflight_needs(STATE | {"parts": ["grants"]}))}
    assert names == {"GRANT (MANAGE or ownership)"}


def test_ownership_and_unreadable_grants():
    client = FakeClient()
    client.objects[f"{access.UC}/catalogs/main"]["owner"] = "ml-engineers"
    client.unreadable.add(("schema", "main.ai_gateway"))
    found = checks(client, access.preflight_needs(STATE))
    assert found[("catalog main", "USE CATALOG")]["via"] == "ml-engineers owns it"
    assert found[("schema main.ai_gateway", "GRANT (MANAGE or ownership)")]["via"] == "ml-engineers owns its catalog main"
    assert found[("schema main.ai_gateway", "CREATE SERVICE")]["status"] == "unknown"


def test_config_checks_what_the_files_name(tmp_path: Path):
    client = FakeClient()
    state = workdir(tmp_path)
    needs = access.config_needs(client, json.loads(state.read_text()), tmp_path / "ai-gateway")
    found = checks(client, needs)
    assert found[("model system.ai.databricks-claude-opus-5-5", "EXECUTE")]["via"] == "you can read it, which needs EXECUTE"
    assert found[("schema main.ai_gateway", "CREATE FUNCTION")]["for"] == ["the SQL policy block_codenames"]
    grants_on = found[("model service main.ai_gateway.team-chat", "GRANT (MANAGE or ownership)")]
    assert grants_on["status"] == "held" and grants_on["via"] == "you'll own it: you create it"
    assert found[("catalog main", "GRANT (MANAGE or ownership)")]["required"]  # nobody listed has USE CATALOG yet
    client.objects[f"{deploy.API}/main.ai_gateway.split"] = {"name": "main.ai_gateway.split", "owner": "someone-else"}
    client.unreadable.add(("model_service", "main.ai_gateway.split"))
    found = checks(client, access.config_needs(client, json.loads(state.read_text()), tmp_path / "ai-gateway"))
    assert found[("model service main.ai_gateway.split", "MANAGE")]["status"] == "unknown"  # existing, not ours
    del client.objects[f"{deploy.MODELS_API}/system.ai.databricks-claude-opus-5-5"]
    assert checks(client, needs)[("model system.ai.databricks-claude-opus-5-5", "EXECUTE")]["status"] == "missing"


# ── evaluate.py ──────────────────────────────────────────────────────────────────


def test_a_blocked_answer_counts_and_a_failed_policy_doesnt():
    client = FakeClient()
    client.gateway = [{"choices": [{"finish_reason": "content_filter"}],
                       "databricks_service_policy": {"name": "jailbreak_detection", "reason": "jailbreak"}},
                      TooManyRequests("429"), {"choices": [{"message": {"content": "Hi"}}]},
                      RuntimeError("Input policy 'regulated_advice' failed to evaluate")]
    blocked = evaluate.ask(client, "main.ai_gateway.team-chat", "hi", sleep=lambda _: None)
    assert blocked["answered"] and blocked["blocked_by"] == "jailbreak_detection"
    retried = evaluate.ask(client, "main.ai_gateway.split", "hi", sleep=lambda _: None)
    assert retried["answered"] and retried["error"] is None
    failed = evaluate.ask(client, "main.ai_gateway.split", "hi", sleep=lambda _: None)
    assert not failed["answered"] and failed["blocked_by"] == "regulated_advice"


def test_score_gates_each_part():
    client = FakeClient()
    deploy.apply(client, STATE, SERVICES, POLICIES, deploy.plan(client, STATE, SERVICES, POLICIES), sleep=lambda _: None)
    services_plan = deploy.summary(deploy.plan(client, STATE, SERVICES, POLICIES))
    grants_plan = grant.plan(client, STATE, PERMISSIONS, SERVICES)
    answers = [evaluate.ask(client, s["name"], "hi") for s in services_plan["services"]]
    report = evaluate.score(STATE, services_plan, grants_plan, answers)
    assert report["metrics"] == {"config_match_rate": 1.0, "answer_rate": 1.0, "grant_rate": 0.0}
    assert report["below_threshold"] == ["grant_rate"] and not report["passed"]
    grant.apply(client, grants_plan)
    report = evaluate.score(STATE, services_plan, grant.plan(client, STATE, PERMISSIONS, SERVICES), answers)
    assert report["passed"] and set(report["thresholds"]) == set(evaluate.THRESHOLDS)
    grants_only = evaluate.score(STATE | {"parts": ["grants"]}, None, grant.plan(client, STATE, PERMISSIONS, SERVICES), [])
    assert set(grants_only["metrics"]) == {"grant_rate"} and grants_only["passed"]
    broken = evaluate.score(STATE, {"errors": ["model-services.yaml not found"]}, None, [])
    assert broken["metrics"]["config_match_rate"] == 0.0 and not broken["passed"]


def test_scripts_explain_a_missing_state_file(tmp_path: Path):
    for script in ("render.py", "deploy_services.py", "grant_access.py", "check_access.py", "evaluate.py"):
        result = run(script, "--state", str(tmp_path / "aig-model-services.json"))
        assert result.returncode == 1 and "Module 00 writes it" in result.stderr, script


# ── Genie Code in a notebook ─────────────────────────────────────────────────────


@pytest.mark.parametrize("module", [render, deploy, grant, access])
def test_scripts_need_a_profile_only_locally(tmp_path: Path, module):
    path = tmp_path / "aig-model-services.json"
    path.write_text(json.dumps({**STATE, "runtime": "workspace", "profile": None}))
    assert module.load_state(path)["runtime"] == "workspace"
    path.write_text(json.dumps({**STATE, "profile": None}))
    with pytest.raises(SystemExit, match="profile is missing or empty: locally"):
        module.load_state(path)
    path.write_text(json.dumps({**STATE, "runtime": "notebook"}))
    with pytest.raises(SystemExit, match="runtime='notebook'"):
        module.load_state(path)


def test_evaluate_runs_the_plans_in_its_own_process(tmp_path: Path):
    """In a notebook only that process is signed in, so evaluate.py runs deploy_services.py and grant_access.py with
    runpy, not a subprocess, and still reads their JSON and their exit messages."""
    state = workdir(tmp_path, services=None, runtime="workspace", profile=None)
    argv = list(sys.argv)
    plan = evaluate.run_plan("deploy_services.py", state)
    assert plan == {"errors": [f"model-services.yaml not found in {tmp_path / 'ai-gateway'}: render.py writes it "
                               "(module 01)"]}
    missing = evaluate.run_plan("grant_access.py", tmp_path / "elsewhere.json")
    assert "Module 00 writes it" in missing["errors"][0]
    assert sys.argv == argv  # restored for the caller

