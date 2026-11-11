# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Check that you have the privileges this skill's changes need (forge-aig-create-model-service modules 00 and 02).

    uv run <skill>/scripts/check_access.py --state <workdir>/aig-model-services.json            # preflight
    uv run <skill>/scripts/check_access.py --state <workdir>/aig-model-services.json --config   # what the files name

"You" is the CLI profile's identity, the one the other scripts run as. Without --config (module 00, before the YAML
files exist) it checks the target schema for each part in `parts`:
- model_services: USE CATALOG on the catalog; USE SCHEMA and CREATE SERVICE on the schema; CREATE TABLE, for
  inference tables.
- service_policies: CREATE FUNCTION on the schema (SQL policies); EXECUTE on `default_evaluator` (LLM policies).
- grants: MANAGE or ownership on the catalog and schema, to grant USE CATALOG and USE SCHEMA.
With --config (module 02) it checks what the filled-in files name instead: each new service's schema, MANAGE on each
existing service it updates, EXECUTE on each destination model and model provider service and on each policy's
evaluator, CAN MANAGE on each provisioned-throughput endpoint, and the right to grant on each service in
permissions.yaml and, where someone listed lacks USE CATALOG or USE SCHEMA, on its catalog and schema.

A privilege counts as held when Unity Catalog grants it to you, to `account users`, or to a group you're in, on the
object or inherited from its catalog or schema; when you or your group owns the object; or, for granting, when you own
its schema or catalog or are a metastore admin. ALL PRIVILEGES counts for everything but MANAGE. A model is checked by
reading it, which needs EXECUTE. Each check is held, missing, or unknown (the grants couldn't be read). It prints JSON
and exits 1 when a required check is missing. It changes nothing and never grants.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

UC = "/api/2.1/unity-catalog"
OBJECTS = {"catalog": f"{UC}/catalogs", "schema": f"{UC}/schemas", "model_service": f"{UC}/model-services",
           "model_provider_service": f"{UC}/model-provider-services"}
SCIM_ME = "/api/2.0/preview/scim/v2/Me"
PARTS = ("model_services", "service_policies", "grants")
LLM_TYPES = ("block_unsafe_content", "block_jailbreak", "block_hallucination", "llm_judge")
NOT_IN_ALL = ("MANAGE", "READ_METADATA")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/aig-model-services.json")
    p.add_argument("--config", action="store_true", help="Check what the filled-in YAML files name")
    return p.parse_args()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("profile", "catalog", "schema", "parts") if not state.get(key)]
    if set(state.get("parts") or []) - set(PARTS):
        errors.append(f"parts={state['parts']!r}: any of {list(PARTS)}")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def name_of(e: Exception) -> str:
    return type(e).__name__


def get(client, path: str, query: dict | None = None) -> dict | None:
    try:
        return client.do("GET", path, query=query) if query else client.do("GET", path)
    except Exception as e:  # noqa: BLE001 - databricks.sdk.errors are matched by name, so tests need no SDK
        if name_of(e) == "NotFound" or getattr(e, "status_code", None) == 404:
            return None
        raise


def privilege(name) -> str:
    return str(name).strip().upper().replace(" ", "_")


def shown(p: str) -> str:
    return "GRANT (MANAGE or ownership)" if p == "GRANT" else p.replace("_", " ")


# ── What's needed ────────────────────────────────────────────────────────────────


class Needs:
    """Requirements, merged by (securable, name, privilege)."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def add(self, securable: str, name: str, needs: str, why: str, required: bool = True, held: str | None = None):
        key = (securable, name, needs)
        item = self.items.setdefault(key, {"securable": securable, "name": name, "needs": needs, "for": [],
                                           "required": False, "known": held})
        if why not in item["for"]:
            item["for"].append(why)
        item["required"] = item["required"] or required


def preflight_needs(state: dict) -> Needs:
    needs, catalog, schema = Needs(), state["catalog"], f"{state['catalog']}.{state['schema']}"
    parts = state["parts"]
    if "model_services" in parts:
        needs.add("catalog", catalog, "USE_CATALOG", "reach the target schema")
        needs.add("schema", schema, "USE_SCHEMA", "reach the target schema")
        needs.add("schema", schema, "CREATE_SERVICE", "create model services")
        needs.add("schema", schema, "CREATE_TABLE", "inference tables (only for services that log one)", required=False)
    if "service_policies" in parts:
        needs.add("schema", schema, "CREATE_FUNCTION", "SQL service policies (only if you use one)", required=False)
        evaluator = state.get("default_evaluator") or "system.ai.gpt-oss-20b"
        needs.add("model_service", evaluator, "EXECUTE", "LLM-based service policies (the default evaluator)",
                  required=False)
    if "grants" in parts:
        needs.add("catalog", catalog, "GRANT", "grant USE CATALOG (only to people who don't have it)", required=False)
        needs.add("schema", schema, "GRANT", "grant USE SCHEMA (only to people who don't have it)", required=False)
    return needs


def load(folder: Path, name: str) -> dict:
    path = folder / name
    if not path.is_file():
        raise SystemExit(f"{path} not found: render.py writes it (module 01)")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{path} isn't valid YAML: {e}") from None
    return data if isinstance(data, dict) else {}


def config_needs(client, state: dict, folder: Path) -> Needs:
    needs, parts = Needs(), state["parts"]
    services_cfg = load(folder, "model-services.yaml") if "model_services" in parts else {}
    policies_cfg = load(folder, "service-policies.yaml") if "service_policies" in parts else {}
    created = set()
    if services_cfg:
        catalog, schema = services_cfg.get("catalog"), f"{services_cfg.get('catalog')}.{services_cfg.get('schema')}"
        needs.add("catalog", catalog, "USE_CATALOG", "reach the services' schema")
        needs.add("schema", schema, "USE_SCHEMA", "reach the services' schema")
        defined = {p.get("name"): p for p in policies_cfg.get("service_policies") or [] if isinstance(p, dict)}
        for s in [x for x in services_cfg.get("model_services") or [] if isinstance(x, dict)]:
            full_name = f"{schema}.{s.get('name')}"
            try:
                existing = get(client, f"{OBJECTS['model_service']}/{full_name}")
            except Exception:  # noqa: BLE001 - it exists but can't be read: updating it needs MANAGE, checked below
                existing = {}
            if existing is None:
                created.add(full_name)
                needs.add("schema", schema, "CREATE_SERVICE", f"create {full_name}")
                if s.get("enable_inference_table", True):
                    needs.add("schema", schema, "CREATE_TABLE", f"{full_name}'s inference table")
            else:
                needs.add("model_service", full_name, "MANAGE", f"update {full_name}")
                if s.get("enable_inference_table", True) and not (existing.get("config") or {}).get("inference_table"):
                    needs.add("schema", schema, "CREATE_TABLE", f"{full_name}'s inference table")
            for d in [*(s.get("destinations") or []), *(s.get("fallbacks") or [])]:
                if not isinstance(d, dict) or not d.get("destination"):
                    continue
                target = str(d["destination"])
                if d.get("provider") == "databricks":
                    needs.add("model", target, "EXECUTE", f"route {full_name} to it")
                elif d.get("provider") == "provisioned_throughput":
                    needs.add("serving_endpoint", target, "CAN_MANAGE", f"route {full_name} to it")
                elif d.get("provider") == "external":
                    needs.add("model_provider_service", target, "EXECUTE", f"route {full_name} to it")
                    if len(target.split(".")) == 3:
                        needs.add("catalog", target.split(".")[0], "USE_CATALOG", f"reach {target}")
                        needs.add("schema", target.rsplit(".", 1)[0], "USE_SCHEMA", f"reach {target}")
            if "service_policies" in parts:
                for policy_name in s.get("service_policies") or []:
                    policy = defined.get(policy_name) or {}
                    if policy.get("type") == "sql":
                        needs.add("schema", schema, "CREATE_FUNCTION", f"the SQL policy {policy_name}")
                    elif policy.get("type") in LLM_TYPES:
                        evaluator = policy.get("evaluator") or policies_cfg.get("default_evaluator")
                        if evaluator:
                            needs.add("model_service", str(evaluator), "EXECUTE", f"run the policy {policy_name}")
    if "grants" in parts:
        permissions = load(folder, "permissions.yaml")
        entries = [e for section in ("groups", "users") for e in permissions.get(section) or [] if isinstance(e, dict)]
        for entry in entries:
            principal = str(entry.get("name") or "")
            for service in [str(s) for s in entry.get("model_services") or []]:
                if len(service.split(".")) != 3:
                    continue
                if service in created:
                    needs.add("model_service", service, "GRANT", f"grant {principal} access",
                              held="you'll own it: you create it")
                else:
                    needs.add("model_service", service, "GRANT", f"grant {principal} access")
                for securable, parent, wanted in (("catalog", service.split(".")[0], "USE_CATALOG"),
                                                  ("schema", service.rsplit(".", 1)[0], "USE_SCHEMA")):
                    have = effective(client, securable, parent, {principal})
                    if have is None or not covers(have, wanted):
                        needs.add(securable, parent, "GRANT", f"grant {principal} {shown(wanted)}")
    return needs


# ── Reading what you hold ────────────────────────────────────────────────────────


def read_effective(client, securable: str, name: str, principal: str | None = None) -> list[dict]:
    """Every effective privilege assignment on the object (one principal's, with principal), following pages."""
    found, token = [], None
    while True:
        query = {"max_results": 0, **({"principal": principal} if principal else {}),
                 **({"page_token": token} if token else {})}
        page = get(client, f"{UC}/effective-permissions/{securable}/{name}", query) or {}
        found += page.get("privilege_assignments") or []
        token = page.get("next_page_token")
        if not token:
            return found


def effective(client, securable: str, name: str, principals: set[str]) -> dict[str, list[str]] | None:
    """{privilege: [where it comes from]} for the principals; None when the grants can't be read."""
    try:
        assignments = read_effective(client, securable, name)
    except Exception:  # noqa: BLE001 - reading everyone's grants may need MANAGE; read each principal's instead
        assignments, read = [], False
        for principal in sorted(principals):
            try:
                assignments += read_effective(client, securable, name, principal)
                read = True
            except Exception:  # noqa: BLE001
                continue
        if not read:
            return None
    out: dict[str, list[str]] = {}
    for a in assignments:
        if a.get("principal") not in principals:
            continue
        for p in a.get("privileges") or []:
            source = f" (from {p['inherited_from_type'].lower()} {p['inherited_from_name']})" if p.get(
                "inherited_from_name") else ""
            out.setdefault(privilege(p.get("privilege")), []).append(f"granted to {a['principal']}{source}")
    return out


def covers(have: dict, wanted: str) -> bool:
    return wanted in have or ("ALL_PRIVILEGES" in have and wanted not in NOT_IN_ALL)


class Context:
    def __init__(self, client):
        self.client = client
        me = client.do("GET", SCIM_ME)
        self.user = me.get("userName") or ""
        self.groups = sorted({g.get("display") for g in me.get("groups") or [] if g.get("display")})
        self.principals = {self.user, "account users", *self.groups}
        self.owners: dict[tuple[str, str], str | None] = {}
        self._admin: bool | None = None

    def owner(self, securable: str, name: str) -> str | None:
        if (securable, name) not in self.owners:
            try:
                self.owners[(securable, name)] = (get(self.client, f"{OBJECTS[securable]}/{name}") or {}).get("owner")
            except Exception:  # noqa: BLE001
                self.owners[(securable, name)] = None
        return self.owners[(securable, name)]

    def metastore_admin(self) -> bool:
        if self._admin is None:
            try:
                assignment = get(self.client, f"{UC}/current-metastore-assignment") or {}
                metastore = get(self.client, f"{UC}/metastores/{assignment.get('metastore_id')}") or {}
                self._admin = metastore.get("owner") in self.principals
            except Exception:  # noqa: BLE001
                self._admin = False
        return self._admin


def check_model(ctx: Context, name: str) -> tuple[str, str]:
    try:
        found = get(ctx.client, f"{UC}/models/{name}")
    except Exception as e:  # noqa: BLE001
        if name_of(e) in ("PermissionDenied", "Unauthenticated") or getattr(e, "status_code", None) == 403:
            return "missing", "you can't read it, so you don't have EXECUTE on it"
        return "unknown", f"couldn't read it: {name_of(e)}"
    if found is None:
        return "missing", "no such model, or you can't see it (destinations are models, such as system.ai.databricks-*)"
    return "held", "you can read it, which needs EXECUTE"


def check_endpoint(ctx: Context, name: str) -> tuple[str, str]:
    try:
        endpoint = get(ctx.client, f"/api/2.0/serving-endpoints/{name}")
        if endpoint is None:
            return "missing", "no such serving endpoint"
        acl = get(ctx.client, f"/api/2.0/permissions/serving-endpoints/{endpoint.get('id')}") or {}
    except Exception as e:  # noqa: BLE001
        return "unknown", f"couldn't read its permissions: {name_of(e)}"
    for entry in acl.get("access_control_list") or []:
        who = entry.get("user_name") or entry.get("group_name") or entry.get("service_principal_name")
        levels = {p.get("permission_level") for p in entry.get("all_permissions") or []}
        if who in ctx.principals and "CAN_MANAGE" in levels:
            return "held", f"CAN MANAGE for {who}"
    return "missing", "you don't have CAN MANAGE on it"


def check(ctx: Context, item: dict) -> dict:
    securable, name, needs = item["securable"], item["name"], item["needs"]
    out = {"on": f"{securable.replace('_', ' ')} {name}", "needs": shown(needs), "for": item["for"],
           "required": item["required"]}
    if item.get("known"):
        return {**out, "status": "held", "via": item["known"]}
    if securable == "model":
        status, via = check_model(ctx, name)
        return {**out, "status": status, "via": via}
    if securable == "serving_endpoint":
        status, via = check_endpoint(ctx, name)
        return {**out, "status": status, "via": via}
    owner = ctx.owner(securable, name)
    if owner and owner in ctx.principals:
        return {**out, "status": "held", "via": f"{'you own' if owner == ctx.user else owner + ' owns'} it"}
    if needs == "GRANT":
        parents = [("schema", name.rsplit(".", 1)[0])] if securable not in ("catalog", "schema") else []
        parents += [("catalog", name.split(".")[0])] if securable != "catalog" else []
        for parent_type, parent in parents:
            parent_owner = ctx.owner(parent_type, parent)
            if parent_owner and parent_owner in ctx.principals:
                return {**out, "status": "held", "via": f"{parent_owner} owns its {parent_type} {parent}"}
        if ctx.metastore_admin():
            return {**out, "status": "held", "via": "you're a metastore admin"}
    have = effective(ctx.client, securable, name, ctx.principals)
    if have is None:
        return {**out, "status": "unknown", "via": "its grants can't be read: ask its owner to confirm"}
    wanted = "MANAGE" if needs == "GRANT" else needs
    if wanted in have:
        return {**out, "status": "held", "via": "; ".join(have[wanted])}
    if covers(have, wanted):
        return {**out, "status": "held", "via": "ALL PRIVILEGES: " + "; ".join(have["ALL_PRIVILEGES"])}
    return {**out, "status": "missing", "via": "not granted to you, account users, or your groups"}


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(profile=state["profile"]).api_client
    folder = args.state.resolve().parent / str(state.get("config_dir") or "ai-gateway")
    needs = config_needs(client, state, folder) if args.config else preflight_needs(state)
    ctx = Context(client)
    checks = [check(ctx, item) for item in needs.items.values()]
    missing = [c for c in checks if c["status"] == "missing" and c["required"]]
    report = {"identity": ctx.user, "groups": ctx.groups, "checks": checks,
              "missing": len(missing),
              "missing_optional": sum(c["status"] == "missing" and not c["required"] for c in checks),
              "unknown": sum(c["status"] == "unknown" for c in checks)}
    print(json.dumps(report, indent=2))
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
