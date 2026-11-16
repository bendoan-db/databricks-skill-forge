# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Plan, then create or update, the model services in model-services.yaml and their service policies (modules 02-04).

    uv run <skill>/scripts/deploy_services.py --state <workdir>/aig-model-services.json            # check and plan
    uv run <skill>/scripts/deploy_services.py --state <workdir>/aig-model-services.json --apply    # make the changes

Reads model-services.yaml and service-policies.yaml in <workdir>/<config_dir>/, ported from unity-ai-gateway-utilities'
deploy_model_service notebook. The plan checks the files first: names, providers, traffic that adds up to 100, policies
that are defined and valid, and <...> placeholders left unfilled. Then it reads the workspace, read-only: the schema,
each existing service, each destination model (reading a Unity Catalog model needs EXECUTE), each endpoint, model
provider service, and policy evaluator, and each new service's inference table. It prints each service as create,
update (with the fields that differ), or unchanged, with its policies to attach, update, keep, or detach. It exits 1
on any error, and --apply then changes nothing.

--apply creates or replaces a SQL function for each `sql` policy a service uses (on `warehouse_id`), creates or
updates each service with only the fields that differ, sets its policies, and reads it back to check it matches. A
failed service doesn't stop the others; the run exits 1 and lists them. Nothing is deleted or revoked. With
`service_policies` not in `parts`, policies already on a service are left as they are.

Every call is REST, as the CLI profile locally or as the notebook's user in Genie Code (runtime workspace): the typed
CLI and SDK drop config.service_policies, and the SDK's Privilege enum lacks CREATE_SERVICE. Attaching policies by API
isn't documented (the docs show the UI); the source uses the PATCH with update_mask=config.service_policies, as the UI
does. forge-aig-evaluate-guardrails creates the services it measures through this script.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

API = "/api/2.1/unity-catalog/model-services"
MODELS_API = "/api/2.1/unity-catalog/models"
PROVIDERS_API = "/api/2.1/unity-catalog/model-provider-services"
TABLES_API = "/api/2.1/unity-catalog/tables"
SCHEMAS_API = "/api/2.1/unity-catalog/schemas"
ENDPOINTS_API = "/api/2.0/serving-endpoints"
STATEMENTS_API = "/api/2.0/sql/statements"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # catalog, schema, and model service names
POLICY_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")  # a SQL policy's name is its function's name
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
PARTS = ("model_services", "service_policies", "grants")
PROVIDERS = ("databricks", "provisioned_throughput", "external")
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
MAX_DESTINATIONS = 10
BUILTIN_TYPES = ("block_unsafe_content", "block_jailbreak", "block_hallucination", "detect_sensitive_data")
POLICY_TYPES = (*BUILTIN_TYPES, "llm_judge", "sql")
LLM_TYPES = ("block_unsafe_content", "block_jailbreak", "block_hallucination", "llm_judge")
DEFAULT_PHASES = {"block_jailbreak": ["input"], "block_hallucination": ["output"], "llm_judge": ["input"]}
ONLY_PHASES = {"block_jailbreak": ["input"], "block_hallucination": ["output"]}
PHASES = {"input": "pre_call", "output": "post_call"}
SENSITIVE_DATA_ACTIONS = {"block": "block", "redact": "transform"}  # the API calls the UI's "redact" transform
SENSITIVE_CATEGORIES = ("email_address", "ip_address", "mac_address", "vin", "credit_card", "iban_code",
                        "phone_number", "us_ssn", "us_itin", "us_passport", "us_bank_number", "uk_nhs", "uk_nino",
                        "in_pan", "in_aadhaar")
POLICY_KEYS = {"name", "type", "phases", "mode", "evaluator", "max_turns", "rank", "action", "categories",
               "instruction", "sql", "description"}
SERVICE_KEYS = {"name", "description", "destinations", "fallbacks", "enable_inference_table", "enable_rate_limits",
                "rate_limits", "service_policies"}
DESTINATION_KEYS = {"provider", "destination", "traffic_percentage", "external_model", "native_api_types"}
UPDATE_FIELDS = ("comment", "config.routing.destinations", "config.routing.fallback.destinations",
                 "config.rate_limits", "config.inference_table")
UNSUPPORTED_SQL = [  # SQL policies are transpiled to CEL; these fail at attach time (the source's findings)
    (re.compile(r"\bai_query\s*\(", re.I), "ai_query"),
    (re.compile(r"\bselect\b", re.I), "a subquery"),
    (re.compile(r"\bbetween\b", re.I), "BETWEEN"),
    (re.compile(r"\b(count|sum|avg|min|max|collect_list|collect_set)\s*\(", re.I), "an aggregate"),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/aig-model-services.json")
    p.add_argument("--apply", action="store_true", help="Create or update the services and set their policies")
    return p.parse_args()


# ── Reading the files ────────────────────────────────────────────────────────────


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = runtime_errors(state) + [f"{key} is missing or empty" for key in ("config_dir", "parts")
                                      if not state.get(key)]
    if set(state.get("parts") or []) - set(PARTS):
        errors.append(f"parts={state['parts']!r}: any of {list(PARTS)}")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def load_yaml(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, f"{path.name} not found in {path.parent}: render.py writes it (module 01)"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        return None, f"{path.name} isn't valid YAML: {e}"
    if not isinstance(data, dict):
        return None, f"{path.name}: the top level must be a mapping"
    return data, None


def placeholders(value, where: str) -> list[str]:
    """`<...>` values the user hasn't filled in yet."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in placeholders(v, f"{where}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in placeholders(v, f"{where}[{i}]")]
    if isinstance(value, str) and PLACEHOLDER_RE.search(value):
        return [f"{where} is still {value!r}: fill it in"]
    return []


def call_args(sql: str, function: str) -> list[int]:
    """The number of arguments of each call to `function` in sql (top-level commas, parentheses balanced)."""
    counts = []
    for match in re.finditer(rf"\b{function}\s*\(", sql, re.I):
        depth, commas, quoted = 1, 0, None
        for ch in sql[match.end():]:
            if quoted:
                quoted = None if ch == quoted else quoted
            elif ch in "'\"":
                quoted = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            elif ch == "," and depth == 1:
                commas += 1
        counts.append(commas + 1)
    return counts


# ── Checking the files ───────────────────────────────────────────────────────────


def check_destination(d, where: str, primary: bool) -> list[str]:
    if not isinstance(d, dict):
        return [f"{where}: a mapping with provider and destination"]
    errors = [f"{where}: unknown setting {k!r}" for k in sorted(set(d) - DESTINATION_KEYS)]
    if d.get("provider") not in PROVIDERS:
        errors.append(f"{where}: provider {d.get('provider')!r}; use one of {', '.join(PROVIDERS)}")
    if not d.get("destination"):
        errors.append(f"{where}: destination is missing")
    elif d.get("provider") in ("databricks", "external") and len(str(d["destination"]).split(".")) != 3:
        errors.append(f"{where}: destination {d['destination']!r} must be a three-part name (catalog.schema.name)")
    if d.get("provider") == "external" and not d.get("external_model"):
        errors.append(f"{where}: an external destination needs external_model, the provider's model name")
    if "traffic_percentage" in d and not primary:
        errors.append(f"{where}: fallbacks are tried in order and take no traffic_percentage")
    return errors


def check_services(cfg: dict, policies: dict, policies_on: bool) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for key in ("catalog", "schema"):
        if not cfg.get(key) or not NAME_RE.match(str(cfg[key])):
            errors.append(f"model-services.yaml: {key} {cfg.get(key)!r} must be a Unity Catalog name")
    services = cfg.get("model_services")
    if not isinstance(services, list) or not services:
        return errors + ["model-services.yaml: model_services must list at least one service"], warnings
    names = [s.get("name") for s in services if isinstance(s, dict)]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"model-services.yaml: service {name!r} is listed more than once")
    for i, s in enumerate(services):
        where = f"model-services.yaml: model_services[{i}]" + (f" ({s.get('name')})" if isinstance(s, dict) else "")
        if not isinstance(s, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown setting {k!r}" for k in sorted(set(s) - SERVICE_KEYS)]
        if not s.get("name") or not NAME_RE.match(str(s["name"])):
            errors.append(f"{where}: name {s.get('name')!r}: letters, digits, '_' and '-'")
        destinations = s.get("destinations")
        if not isinstance(destinations, list) or not destinations:
            errors.append(f"{where}: destinations must list at least one model")
            destinations = []
        if len(destinations) > MAX_DESTINATIONS:
            errors.append(f"{where}: at most {MAX_DESTINATIONS} destinations")
        for j, d in enumerate(destinations):
            errors += check_destination(d, f"{where} destinations[{j}]", primary=True)
        if len(destinations) > 1:
            shares = [d.get("traffic_percentage") for d in destinations if isinstance(d, dict)]
            if any(not isinstance(x, int) for x in shares) or sum(x for x in shares if isinstance(x, int)) != 100:
                errors.append(f"{where}: with several destinations, each needs traffic_percentage, adding up to 100")
        for j, d in enumerate(s.get("fallbacks") or []):
            errors += check_destination(d, f"{where} fallbacks[{j}]", primary=False)
        if s.get("enable_rate_limits", True):
            limits = s.get("rate_limits", cfg.get("rate_limits"))
            if not isinstance(limits, dict) or not ({"requests_per_minute", "tokens_per_minute"} & set(limits)):
                errors.append(f"{where}: enable_rate_limits needs rate_limits (here or at the top) with "
                              "requests_per_minute or tokens_per_minute")
            else:
                for k, v in limits.items():
                    if k not in ("requests_per_minute", "tokens_per_minute") or not isinstance(v, int) or v < 0:
                        errors.append(f"{where}: rate_limits.{k}={v!r}: requests_per_minute or tokens_per_minute, "
                                      "a whole number (0 denies every request)")
        listed = s.get("service_policies") or []
        if not isinstance(listed, list):
            errors.append(f"{where}: service_policies must be a list of policy names")
            listed = []
        if listed and not policies_on:
            warnings.append(f"{where}: lists service_policies, but service_policies isn't in parts, so they're ignored")
        elif policies_on:
            for name in listed:
                if name not in policies:
                    errors.append(f"{where}: service policy {name!r} isn't defined in service-policies.yaml")
            for name in sorted({n for n in listed if listed.count(n) > 1}):
                errors.append(f"{where}: service policy {name!r} is listed more than once")
    return errors, warnings


def check_policies(cfg: dict, used: set[str]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    entries = cfg.get("service_policies") or []
    if not isinstance(entries, list):
        return ["service-policies.yaml: service_policies must be a list"], warnings
    names = [p.get("name") for p in entries if isinstance(p, dict)]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"service-policies.yaml: policy {name!r} is defined more than once")
    for i, p in enumerate(entries):
        where = f"service-policies.yaml: service_policies[{i}]" + (f" ({p.get('name')})" if isinstance(p, dict) else "")
        if not isinstance(p, dict):
            errors.append(f"{where}: a mapping")
            continue
        errors += [f"{where}: unknown setting {k!r}" for k in sorted(set(p) - POLICY_KEYS)]
        kind = p.get("type")
        if not p.get("name") or not POLICY_NAME_RE.match(str(p["name"])):
            errors.append(f"{where}: name {p.get('name')!r}: letters, digits, and '_'")
        if kind not in POLICY_TYPES:
            errors.append(f"{where}: type {kind!r}; use one of {', '.join(POLICY_TYPES)}")
            continue
        phases = p.get("phases", DEFAULT_PHASES.get(kind, ["input", "output"]))
        if not isinstance(phases, list) or not phases or set(phases) - set(PHASES):
            errors.append(f"{where}: phases {phases!r}: [input], [output], or [input, output]")
        elif kind in ONLY_PHASES and phases != ONLY_PHASES[kind]:
            errors.append(f"{where}: {kind} runs on {ONLY_PHASES[kind]} only")
        if p.get("mode", "block") not in ("block", "log"):
            errors.append(f"{where}: mode {p.get('mode')!r}: block or log")
        if "rank" in p and (not isinstance(p["rank"], int) or p["rank"] < 1):
            errors.append(f"{where}: rank must be a whole number from 1")
        if "max_turns" in p and (not isinstance(p["max_turns"], int) or p["max_turns"] < 1):
            errors.append(f"{where}: max_turns must be a whole number from 1")
        if kind == "detect_sensitive_data":
            if p.get("action", "block") not in SENSITIVE_DATA_ACTIONS:
                errors.append(f"{where}: action {p.get('action')!r}: block or redact")
            categories = p.get("categories")
            if not isinstance(categories, list) or not categories:
                errors.append(f"{where}: categories is required: any of {', '.join(SENSITIVE_CATEGORIES)}")
            else:
                unknown = [c for c in categories if str(c).removeprefix("class.") not in SENSITIVE_CATEGORIES]
                if unknown:
                    errors.append(f"{where}: unknown categories {unknown}; use any of "
                                  f"{', '.join(SENSITIVE_CATEGORIES)}")
        if kind == "llm_judge" and not str(p.get("instruction") or "").strip():
            errors.append(f"{where}: an llm_judge policy needs instruction: what to flag and what to allow")
        if kind in LLM_TYPES and not (p.get("evaluator") or cfg.get("default_evaluator")):
            errors.append(f"{where}: needs an evaluator, or default_evaluator at the top")
        if kind == "sql":
            sql = str(p.get("sql") or "")
            if not sql.strip():
                errors.append(f"{where}: a sql policy needs sql, an expression returning ALLOW or DENY")
            for pattern, what in UNSUPPORTED_SQL:
                if pattern.search(sql):
                    errors.append(f"{where}: uses {what}, which a SQL policy can't (it can't be transpiled to CEL)")
            for function in ("coalesce", "concat"):
                if any(n > 2 for n in call_args(sql, function)):
                    errors.append(f"{where}: {function.upper()} with more than two arguments can't be transpiled to "
                                  "CEL; nest two-argument calls")
            if "to_variant_object" not in sql:
                warnings.append(f"{where}: sql should return to_variant_object(named_struct('result', ...))")
        if p.get("name") and p["name"] not in used:
            warnings.append(f"{where}: no model service lists it, so it isn't used")
    return errors, warnings


# ── What the API takes ───────────────────────────────────────────────────────────


def build_destination(d: dict) -> dict:
    provider, target = d["provider"], d["destination"]
    if provider == "databricks":
        out = {"name": target, "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
               "pay_per_token_config": {"model": f"models/{target}"}}
    elif provider == "provisioned_throughput":
        out = {"name": target, "destination_type": "DESTINATION_TYPE_PROVISIONED_THROUGHPUT_FOUNDATION_MODEL",
               "provisioned_throughput_config": {"model_serving_endpoint": f"serving-endpoints/{target}"}}
    else:
        model_target = {"model": d["external_model"]}
        if d.get("native_api_types"):
            model_target["native_api_types"] = list(d["native_api_types"])
        out = {"name": f"{target}.{d['external_model']}",
               "destination_type": "DESTINATION_TYPE_EXTERNAL_FOUNDATION_MODEL",
               "external_model_config": {"model_provider_service": f"model-provider-services/{target}",
                                         "target": model_target}}
    if "traffic_percentage" in d:
        out["traffic_percentage"] = d["traffic_percentage"]
    return out


def build_rate_limits(limits: dict) -> list[dict]:
    limit = {"key": "RATE_LIMIT_KEY_USER_DEFAULT", "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE"}
    if "requests_per_minute" in limits:
        limit["requests"] = limits["requests_per_minute"]
    if "tokens_per_minute" in limits:
        limit["tokens"] = limits["tokens_per_minute"]
    return [limit]


def build_service(cfg: dict, s: dict) -> dict:
    """The create or update body for one service, without its policies."""
    routing = {"destinations": [build_destination(d) for d in s["destinations"]]}
    if s.get("fallbacks"):
        routing["fallback"] = {"destinations": [build_destination(d) for d in s["fallbacks"]]}
    config = {"routing": routing}
    if s.get("enable_inference_table", True):  # logs to <catalog>.<schema>.<service name>_payload
        config["inference_table"] = {"parent": f"schemas/{cfg['catalog']}.{cfg['schema']}",
                                     "table_name_prefix": s["name"]}
    if s.get("enable_rate_limits", True):
        config["rate_limits"] = build_rate_limits(s.get("rate_limits", cfg.get("rate_limits")))
    return {"comment": str(s.get("description") or ""), "config": config}


def build_policy(cfg: dict, policies_cfg: dict, name: str, position: int) -> dict:
    """One service policy as the API takes it. Every option value is a string."""
    p = next(x for x in policies_cfg.get("service_policies") or [] if x.get("name") == name)
    kind, rank = p["type"], p.get("rank", position)
    if kind == "sql":
        return {"name": name, "policy_type": "POLICY_TYPE_CUSTOM",
                "handler": f"functions/{cfg['catalog']}.{cfg['schema']}.{name}", "rank": rank}
    phases = p.get("phases", DEFAULT_PHASES.get(kind, ["input", "output"]))
    options = {"phases": ",".join(PHASES[x] for x in phases), "dry_run": "true" if p.get("mode") == "log" else "false"}
    if kind == "detect_sensitive_data":
        options["action"] = SENSITIVE_DATA_ACTIONS[p.get("action", "block")]
        options["categories"] = ",".join(f"class.{str(c).removeprefix('class.')}" for c in p["categories"])
    else:
        options["max_turns"] = str(p.get("max_turns", 10))
        options["model_service"] = f"model-services/{p.get('evaluator') or policies_cfg.get('default_evaluator')}"
        if kind == "llm_judge":
            options["action"] = "block"
            options["instruction"] = str(p["instruction"]).strip()
    handler = "system.ai.invoke_llm_judge" if kind == "llm_judge" else f"system.ai.{kind}"
    return {"name": name, "policy_type": "POLICY_TYPE_BUILTIN", "handler": handler, "rank": rank, "options": options}


def sql_function(cfg: dict, p: dict) -> str:
    name = ".".join(f"`{part}`" for part in (cfg["catalog"], cfg["schema"], p["name"]))
    comment = str(p.get("description") or "").replace("\\", "\\\\").replace("'", "\\'")
    return (f"CREATE OR REPLACE FUNCTION {name}(event VARIANT)\nRETURNS VARIANT\nLANGUAGE SQL\nCOMMENT '{comment}'\n"
            f"RETURN {str(p['sql']).strip()}")


# ── Comparing with the workspace ─────────────────────────────────────────────────


def live(items) -> list[dict]:
    return [x for x in items or [] if isinstance(x, dict) and not x.get("is_deleted")]


def subset(want, have) -> bool:
    """True when every field in want is in have with the same value; the API adds fields of its own."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(k in have and subset(v, have[k]) for k, v in want.items())
    if isinstance(want, list):
        return (isinstance(have, list) and len(want) == len(have)
                and all(subset(w, h) for w, h in zip(want, have, strict=True)))
    return want == have


def changed_fields(body: dict, current: dict) -> list[str]:
    """The update-mask paths whose values differ from the service as read."""
    want, have = body["config"], current.get("config") or {}
    pairs = {
        "comment": (body["comment"], current.get("comment") or ""),
        "config.routing.destinations": (want["routing"]["destinations"],
                                        live((have.get("routing") or {}).get("destinations"))),
        "config.routing.fallback.destinations": (
            (want["routing"].get("fallback") or {}).get("destinations") or [],
            live(((have.get("routing") or {}).get("fallback") or {}).get("destinations"))),
        "config.rate_limits": (want.get("rate_limits") or [], have.get("rate_limits") or []),
        "config.inference_table": (want.get("inference_table"), have.get("inference_table") or None),
    }
    return [field for field in UPDATE_FIELDS
            if not (pairs[field][0] == pairs[field][1] or (pairs[field][0] and subset(*pairs[field])))]


def same_policy(want: dict, have: dict) -> bool:
    keys = ("policy_type", "rank") if want["policy_type"] == "POLICY_TYPE_CUSTOM" else (
        "policy_type", "handler", "rank", "options")
    return all(have.get(k) == want[k] for k in keys)  # a custom policy's handler may read back as its function's id


def policy_steps(want: list[dict], current: dict | None) -> list[str]:
    have = {x["name"]: x for x in live(((current or {}).get("config") or {}).get("service_policies"))}
    steps = [f"attach {p['name']}" if p["name"] not in have else f"keep {p['name']}" if same_policy(p, have[p["name"]])
             else f"update {p['name']}" for p in want]
    wanted = {p["name"] for p in want}
    return steps + [f"detach {name}" for name in have if name not in wanted]


def get(client, path: str, query: dict | None = None) -> dict | None:
    """GET, with None for a resource that doesn't exist."""
    try:
        return client.do("GET", path, query=query) if query else client.do("GET", path)
    except Exception as e:  # noqa: BLE001 - databricks.sdk.errors are matched by name, so tests need no SDK
        if type(e).__name__ == "NotFound" or getattr(e, "status_code", None) == 404:
            return None
        raise


def lookup(client, path: str) -> tuple[dict | None, str | None]:
    """(the resource, None), (None, None) when it doesn't exist, or (None, the error) when it can't be read."""
    try:
        return get(client, path), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def workspace_errors(client, cfg: dict, policies_cfg: dict, policy_names: set[str], current: dict) -> list[str]:
    """Read-only checks of what the files name: the schema, destinations, endpoints, provider services, evaluators."""
    errors = []
    schema, problem = lookup(client, f"{SCHEMAS_API}/{cfg['catalog']}.{cfg['schema']}")
    if schema is None:
        errors.append(f"schema {cfg['catalog']}.{cfg['schema']} "
                      + (f"can't be read: {problem}" if problem else "doesn't exist"))
    seen = set()
    for s in cfg["model_services"]:
        for d in [*s.get("destinations", []), *(s.get("fallbacks") or [])]:
            key = (d["provider"], d["destination"])
            if key in seen:
                continue
            seen.add(key)
            if d["provider"] == "databricks":
                found, problem = lookup(client, f"{MODELS_API}/{d['destination']}")
                if found is None:
                    hint = ""
                    catalog, schema_name, model = d["destination"].split(".")
                    if not model.startswith("databricks-") and lookup(
                            client, f"{MODELS_API}/{catalog}.{schema_name}.databricks-{model}")[0] is not None:
                        hint = (f"; {d['destination']} is a model service's name, and its model is "
                                f"{catalog}.{schema_name}.databricks-{model}")
                    reason = (f"can't be read (you need EXECUTE on it): {problem}" if problem
                              else "isn't a Unity Catalog model")
                    errors.append(f"destination {d['destination']} {reason}{hint}")
            elif d["provider"] == "provisioned_throughput":
                found, problem = lookup(client, f"{ENDPOINTS_API}/{d['destination']}")
                if found is None:
                    errors.append(f"serving endpoint {d['destination']} "
                                  + (f"can't be read: {problem}" if problem else "doesn't exist"))
            else:
                found, problem = lookup(client, f"{PROVIDERS_API}/{d['destination']}")
                if found is None:
                    errors.append(f"model provider service {d['destination']} "
                                  + (f"can't be read: {problem}" if problem else "doesn't exist: create it first"))
    evaluators = set()
    for p in policies_cfg.get("service_policies") or []:
        if p.get("name") in policy_names and p.get("type") in LLM_TYPES:
            evaluators.add(p.get("evaluator") or policies_cfg.get("default_evaluator"))
    for evaluator in sorted(e for e in evaluators if e):
        found, problem = lookup(client, f"{API}/{evaluator}")
        if found is None:
            hint = ""
            if evaluator.split(".")[-1].startswith("databricks-"):
                hint = "; evaluators are model services, so drop the model's databricks- prefix"
            errors.append(f"evaluator {evaluator} " + (f"can't be read: {problem}" if problem else
                                                       "isn't a model service") + hint)
    for s in cfg["model_services"]:
        full_name = f"{cfg['catalog']}.{cfg['schema']}.{s['name']}"
        existing = current.get(full_name)
        if s.get("enable_inference_table", True):
            table = f"{cfg['catalog']}.{cfg['schema']}.{s['name']}_payload"
            have = ((existing or {}).get("config") or {}).get("inference_table")
            if existing is None and get_quietly(client, f"{TABLES_API}/{table}") is not None:
                errors.append(f"{full_name}: its inference table {table} already exists, and creating the service "
                              "would fail. Set enable_inference_table: false, or rename the service")
            elif have and not subset(build_service(cfg, s)["config"]["inference_table"], have):
                errors.append(f"{full_name}: its inference table can't change after it's created ({have}). Set "
                              "enable_inference_table to match, or delete and recreate the service (a separate "
                              "confirmation naming it)")
    return errors


def get_quietly(client, path: str) -> dict | None:
    try:
        return get(client, path)
    except Exception:  # noqa: BLE001 - a table that can't be read can't be checked; creating the service reports it
        return None


def plan(client, state: dict, cfg: dict, policies_cfg: dict | None) -> dict:
    """Everything --apply would do, and every problem found. Reads only."""
    policies_on = "service_policies" in state["parts"]
    policies_cfg = policies_cfg or {}
    defined = {p.get("name"): p for p in policies_cfg.get("service_policies") or [] if isinstance(p, dict)}
    errors, warnings = check_services(cfg, defined, policies_on)
    used = {n for s in cfg.get("model_services") or [] if isinstance(s, dict) for n in s.get("service_policies") or []}
    if policies_on:
        e, w = check_policies(policies_cfg, used)
        errors, warnings = errors + e, warnings + w
    errors += placeholders(cfg, "model-services.yaml") + (placeholders(policies_cfg, "service-policies.yaml")
                                                          if policies_on else [])
    out = {"schema": f"{cfg.get('catalog')}.{cfg.get('schema')}", "errors": errors, "warnings": warnings,
           "functions": [], "services": [], "changes": False}
    if errors:
        return out
    current, read_errors = {}, []
    for s in cfg["model_services"]:
        full_name = f"{cfg['catalog']}.{cfg['schema']}.{s['name']}"
        found, problem = lookup(client, f"{API}/{full_name}")
        current[full_name] = found
        if problem:
            read_errors.append(f"{full_name} can't be read (to update it you need MANAGE on it): {problem}")
    out["errors"] = read_errors + workspace_errors(client, cfg, policies_cfg, used if policies_on else set(), current)
    if out["errors"]:
        return out
    sql_used = sorted(n for n in used if policies_on and defined[n]["type"] == "sql")
    out["functions"] = [{"name": f"{cfg['catalog']}.{cfg['schema']}.{n}", "action": "create or replace"}
                        for n in sql_used]
    if sql_used and not state.get("warehouse_id"):
        out["errors"].append("sql policies need warehouse_id in the state file, the SQL warehouse that creates them")
    for s in cfg["model_services"]:
        full_name = f"{cfg['catalog']}.{cfg['schema']}.{s['name']}"
        body, existing = build_service(cfg, s), current[full_name]
        policies = [build_policy(cfg, policies_cfg, n, i) for i, n in enumerate(s.get("service_policies") or [], 1)
                    ] if policies_on else []
        entry = {"name": full_name, "action": "create" if existing is None else "update", "body": body,
                 "changes": list(UPDATE_FIELDS) if existing is None else changed_fields(body, existing),
                 "policies": policies if policies_on else None, "policy_steps": []}
        if existing is not None and not entry["changes"]:
            entry["action"] = "unchanged"
        if policies_on:
            steps = policy_steps(policies, existing)
            # CREATE OR REPLACE can give a SQL policy's function a new id, so its policy is attached again every time
            entry["policy_steps"] = [f"re-attach {step[5:]}" if step.startswith("keep ") and step[5:] in sql_used
                                     else step for step in steps]
        # matches: the service is already what the files describe (re-attaching a SQL policy isn't a difference)
        entry["matches"] = existing is not None and not entry["changes"] and all(
            step.startswith(("keep ", "re-attach ")) for step in entry["policy_steps"])
        if entry["action"] == "unchanged" and any(not step.startswith("keep ") for step in entry["policy_steps"]):
            entry["action"] = "update"
        clears_table = "config.inference_table" in entry["changes"] and not body["config"].get("inference_table")
        if existing is not None and clears_table:
            warnings.append(f"{full_name}: the update clears config.inference_table; the table itself stays")
        out["services"].append(entry)
    out["changes"] = bool(out["functions"]) or any(e["action"] != "unchanged" for e in out["services"])
    return out


# ── Applying ─────────────────────────────────────────────────────────────────────


def run_sql(client, warehouse_id: str, statement: str, sleep=time.sleep) -> None:
    """Run one statement on the SQL warehouse, and raise unless it succeeds."""
    response = client.do("POST", STATEMENTS_API, body={"warehouse_id": warehouse_id, "statement": statement,
                                                       "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"})
    for _ in range(60):
        status = (response.get("status") or {})
        state = status.get("state")
        if state == "SUCCEEDED":
            return
        if state in ("FAILED", "CANCELED", "CLOSED"):
            raise RuntimeError(f"{state}: {(status.get('error') or {}).get('message', '')}"[:500])
        sleep(5)
        response = client.do("GET", f"{STATEMENTS_API}/{response['statement_id']}")
    raise RuntimeError("the statement didn't finish in 5 minutes")


def verify(entry: dict, service: dict) -> list[str]:
    """What doesn't match the plan in the service as read back."""
    wrong = [f"{field} doesn't match" for field in changed_fields(entry["body"], service)]
    if entry["policies"] is not None:
        have = {x["name"]: x for x in live((service.get("config") or {}).get("service_policies"))}
        want = {x["name"]: x for x in entry["policies"]}
        wrong += [f"service policy {n} doesn't match" for n in sorted(
            {n for n in want if n not in have or not same_policy(want[n], have[n])} | (have.keys() - want.keys()))]
    return wrong


def apply(client, state: dict, cfg: dict, policies_cfg: dict | None, p: dict, sleep=time.sleep) -> dict:
    result = {"functions": [], "applied": [], "unchanged": [], "failed": []}
    broken = set()
    for f in p["functions"]:
        name = f["name"].rsplit(".", 1)[1]
        policy = next(x for x in (policies_cfg or {}).get("service_policies") or [] if x.get("name") == name)
        try:
            run_sql(client, state["warehouse_id"], sql_function(cfg, policy), sleep=sleep)
            result["functions"].append(f["name"])
        except Exception as e:  # noqa: BLE001 - the services that use it fail; the others go on
            broken.add(name)
            result["failed"].append({"name": f["name"], "error": f"creating the function: {e}"[:500]})
    for entry in p["services"]:
        name = entry["name"]
        if entry["action"] == "unchanged":
            result["unchanged"].append(name)
            continue
        uses = {x["name"] for x in entry["policies"] or [] if x["policy_type"] == "POLICY_TYPE_CUSTOM"}
        if uses & broken:
            result["failed"].append({"name": name, "error": f"skipped: its SQL policy {sorted(uses & broken)} failed"})
            continue
        catalog, schema, service_id = name.split(".")
        try:
            if entry["action"] == "create":
                client.do("POST", API, query={"parent": f"schemas/{catalog}.{schema}", "model_service_id": service_id},
                          body=entry["body"])
            elif entry["changes"]:
                client.do("PATCH", f"{API}/{name}", query={"update_mask": ",".join(entry["changes"])},
                          body=entry["body"])
            if entry["policies"] is not None and (any(not s.startswith("keep ") for s in entry["policy_steps"])):
                client.do("PATCH", f"{API}/{name}", query={"update_mask": "config.service_policies"},
                          body={"config": {"service_policies": entry["policies"]}})
            service = client.do("GET", f"{API}/{name}")
            if wrong := verify(entry, service):
                raise RuntimeError("read back after the change: " + "; ".join(wrong))
            result["applied"].append({"name": name, "action": entry["action"], "changes": entry["changes"],
                                      "policies": entry["policy_steps"]})
        except Exception as e:  # noqa: BLE001 - one service's failure doesn't stop the others
            result["failed"].append({"name": name, "error": f"{type(e).__name__}: {e}"[:500]})
    return result


def summary(p: dict) -> dict:
    """The plan as printed: without the request bodies."""
    services = [{k: v for k, v in e.items() if k not in ("body", "policies")} for e in p["services"]]
    return {**{k: v for k, v in p.items() if k != "services"}, "services": services}


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    if "model_services" not in state["parts"]:
        raise SystemExit("parts has no model_services: there's nothing for this script to do (grant_access.py grants)")
    folder = args.state.resolve().parent / state["config_dir"]
    cfg, problem = load_yaml(folder / "model-services.yaml")
    policies_cfg, policy_problem = (load_yaml(folder / "service-policies.yaml") if "service_policies" in state["parts"]
                                    else (None, None))
    if problem or policy_problem:
        print(json.dumps({"errors": [x for x in (problem, policy_problem) if x]}, indent=2))
        sys.exit(1)
    client = workspace_client(state).api_client
    p = plan(client, state, cfg, policies_cfg)
    if p["errors"] or not args.apply:
        print(json.dumps(summary(p), indent=2))
        sys.exit(1 if p["errors"] else 0)
    result = apply(client, state, cfg, policies_cfg, p)
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
