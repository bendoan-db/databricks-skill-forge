# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Plan, then grant, the access in permissions.yaml (forge-aig-create-model-service modules 02-04).

    uv run <skill>/scripts/grant_access.py --state <workdir>/aig-model-services.json            # check and plan
    uv run <skill>/scripts/grant_access.py --state <workdir>/aig-model-services.json --apply    # grant what's missing

Ported from unity-ai-gateway-utilities' grant_permissions notebook. Each entry under `groups` and `users` names a
principal (an account-level group, a user's email, or a service principal's application ID), the model services it
gets (three-part names), and `privileges` (default EXECUTE). Querying also needs USE CATALOG and USE SCHEMA on each
service's catalog and schema, which are granted too.

The plan checks the file, looks each principal up (a workspace-local group can't receive Unity Catalog grants), reads
each service, and reads the principal's effective privileges, so only what's missing is granted. A service that
model-services.yaml creates but that doesn't exist yet is planned "after deploy". --apply adds the missing privileges
and reads them back. It only adds: taking an entry out of the file revokes nothing; a revoke is done by hand, after a
confirmation that names the principal, the privilege, and the service (module 03).
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

API = "/api/2.1/unity-catalog/model-services"
EFFECTIVE_API = "/api/2.1/unity-catalog/effective-permissions"
PERMISSIONS_API = "/api/2.1/unity-catalog/permissions"
SCIM = "/api/2.0/preview/scim/v2"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
APPLICATION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
PARTS = ("model_services", "service_policies", "grants")
PRIVILEGES = ("EXECUTE", "READ_METADATA", "APPLY_TAG", "MANAGE", "ALL_PRIVILEGES")
NOT_IN_ALL = ("MANAGE", "READ_METADATA")  # ALL PRIVILEGES on a model service covers every privilege but these
ENTRY_KEYS = {"name", "model_services", "privileges"}
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
    p.add_argument("--state", required=True, type=Path, help="<workdir>/aig-model-services.json")
    p.add_argument("--apply", action="store_true", help="Grant the missing privileges")
    return p.parse_args()


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
    return (data, None) if isinstance(data, dict) else (None, f"{path.name}: the top level must be a mapping")


def privilege(name) -> str:
    """EXECUTE, or `READ METADATA` written with a space, as the API spells it."""
    return str(name).strip().upper().replace(" ", "_")


def kind_of(section: str, name: str) -> str:
    if section == "groups":
        return "group"
    return "user" if "@" in name else "service_principal"


def entries(cfg: dict) -> tuple[list[dict], list[str]]:
    """[{principal, kind, services, privileges}] from the file, and every problem with it."""
    out, errors = [], []
    for key in sorted(set(cfg) - {"groups", "users"}):
        errors.append(f"permissions.yaml: unknown top-level key {key!r}; use groups and users")
    for section in ("groups", "users"):
        items = cfg.get(section) or []
        if not isinstance(items, list):
            errors.append(f"permissions.yaml: {section} must be a list")
            continue
        for i, entry in enumerate(items):
            where = f"permissions.yaml: {section}[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{where}: a mapping with name and model_services")
                continue
            name = str(entry.get("name") or "").strip()
            where += f" ({name})" if name else ""
            errors += [f"{where}: unknown setting {k!r}" for k in sorted(set(entry) - ENTRY_KEYS)]
            if not name:
                errors.append(f"{where}: name is missing")
            elif PLACEHOLDER_RE.search(name):
                errors.append(f"{where}: name is still {name!r}: fill it in")
            elif section == "users" and "@" not in name and not APPLICATION_ID_RE.match(name):
                errors.append(f"{where}: a user's email, or a service principal's application ID")
            services = entry.get("model_services")
            if not isinstance(services, list) or not services:
                errors.append(f"{where}: model_services must list at least one service")
                services = []
            for service in services:
                parts = str(service).split(".")
                if PLACEHOLDER_RE.search(str(service)):
                    errors.append(f"{where}: model service {service!r} still has a placeholder: fill it in")
                elif len(parts) != 3 or not all(NAME_RE.match(p) for p in parts):
                    errors.append(f"{where}: {service!r} isn't a three-part name (catalog.schema.service)")
            privileges = [privilege(p) for p in entry.get("privileges") or ["EXECUTE"]]
            if unknown := [p for p in privileges if p not in PRIVILEGES]:
                errors.append(f"{where}: unknown privileges {unknown}; use any of {', '.join(PRIVILEGES)}")
            out.append({"principal": name, "kind": kind_of(section, name), "services": [str(s) for s in services],
                        "privileges": list(dict.fromkeys(privileges))})
    if not out and not errors:
        errors.append("permissions.yaml lists no groups or users")
    seen: dict[str, str] = {}
    for e in out:
        if e["principal"] in seen and seen[e["principal"]] != e["kind"]:
            errors.append(f"permissions.yaml: {e['principal']} is listed as both a group and a user")
        seen[e["principal"]] = e["kind"]
    return out, errors


def get(client, path: str, query: dict | None = None) -> dict | None:
    try:
        return client.do("GET", path, query=query) if query else client.do("GET", path)
    except Exception as e:  # noqa: BLE001 - databricks.sdk.errors are matched by name, so tests need no SDK
        if type(e).__name__ == "NotFound" or getattr(e, "status_code", None) == 404:
            return None
        raise


def find_principal(client, kind: str, name: str) -> tuple[str | None, str | None]:
    """(error, warning) for one principal, from the workspace's SCIM API."""
    resource, field = {"group": ("Groups", "displayName"), "user": ("Users", "userName"),
                       "service_principal": ("ServicePrincipals", "applicationId")}[kind]
    try:
        found = (get(client, f"{SCIM}/{resource}", {"filter": f'{field} eq "{name}"'}) or {}).get("Resources") or []
    except Exception as e:  # noqa: BLE001
        return None, f"{name}: couldn't look it up ({type(e).__name__}); the grant reports it if it doesn't exist"
    if not found:
        label = {"group": "group", "user": "user", "service_principal": "service principal"}[kind]
        return None, (f"{label} {name} isn't in this workspace. An account-level {label} that isn't assigned here can "
                      "still get the grant, but it needs workspace access to query a service")
    if kind == "group" and (found[0].get("meta") or {}).get("resourceType") == "WorkspaceGroup":
        return (f"group {name} is workspace-local, and Unity Catalog grants need an account-level group. Use an "
                "account group with the same members"), None
    return None, None


def effective(client, securable: str, name: str, principal: str) -> set[str] | None:
    """The principal's privileges on the object, granted or inherited; None when they can't be read."""
    try:
        found = get(client, f"{EFFECTIVE_API}/{securable}/{name}", {"principal": principal}) or {}
    except Exception:  # noqa: BLE001 - unknown: the plan grants everything, and a grant that's already there is a no-op
        return None
    return {privilege(p.get("privilege")) for a in found.get("privilege_assignments") or []
            if a.get("principal") == principal for p in a.get("privileges") or []}


def held(have: set[str], wanted: str) -> bool:
    return wanted in have or ("ALL_PRIVILEGES" in have and wanted not in NOT_IN_ALL)


def plan(client, state: dict, cfg: dict, services_cfg: dict | None) -> dict:
    found, errors = entries(cfg)
    out = {"errors": errors, "warnings": [], "grants": [], "changes": False}
    if errors:
        return out
    created = set()
    if services_cfg and "model_services" in state["parts"]:
        created = {f"{services_cfg.get('catalog')}.{services_cfg.get('schema')}.{s.get('name')}"
                   for s in services_cfg.get("model_services") or [] if isinstance(s, dict)}
    exists: dict[str, bool] = {}
    for service in sorted({s for e in found for s in e["services"]}):
        try:
            exists[service] = get(client, f"{API}/{service}") is not None
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{service} can't be read (granting on it needs MANAGE on it, or owning it): "
                                 f"{type(e).__name__}")
            continue
        if not exists[service] and service not in created:
            out["errors"].append(f"{service} doesn't exist, and model-services.yaml doesn't create it")
    for principal in {(e["kind"], e["principal"]) for e in found}:
        error, warning = find_principal(client, *principal)
        out["errors"] += [error] if error else []
        out["warnings"] += [warning] if warning else []
    if out["errors"]:
        return out
    wanted: dict[tuple[str, str, str], list[str]] = {}  # (principal, securable, name) -> privileges, in order
    for e in found:
        for catalog in sorted({s.split(".")[0] for s in e["services"]}):
            wanted.setdefault((e["principal"], "catalog", catalog), []).append("USE_CATALOG")
        for schema in sorted({s.rsplit(".", 1)[0] for s in e["services"]}):
            wanted.setdefault((e["principal"], "schema", schema), []).append("USE_SCHEMA")
        for service in e["services"]:
            wanted.setdefault((e["principal"], "model_service", service), []).extend(e["privileges"])
    for (principal, securable, name), privileges in sorted(wanted.items(), key=lambda x: (
            x[0][0], ("catalog", "schema", "model_service").index(x[0][1]), x[0][2])):
        privileges = list(dict.fromkeys(privileges))
        grant = {"principal": principal, "securable": securable, "name": name, "privileges": privileges}
        if securable == "model_service" and not exists.get(name):
            grant.update(status="after deploy", add=privileges)
        else:
            have = effective(client, securable, name, principal)
            if have is None:
                grant.update(status="unknown", add=privileges)
            else:
                add = [p for p in privileges if not held(have, p)]
                grant.update(status="grant" if add else "ok", add=add)
        out["grants"].append(grant)
    out["changes"] = any(g["add"] for g in out["grants"])
    return out


def apply(client, p: dict) -> dict:
    result = {"granted": [], "unchanged": 0, "failed": [], "not_visible_yet": []}
    for g in p["grants"]:
        if not g["add"]:
            result["unchanged"] += 1
            continue
        where = f"{g['principal']} on {g['securable']} {g['name']}"
        try:
            if g["status"] == "after deploy" and get(client, f"{API}/{g['name']}") is None:
                raise RuntimeError("the service doesn't exist yet: run deploy_services.py --apply first")
            client.do("PATCH", f"{PERMISSIONS_API}/{g['securable']}/{g['name']}",
                      body={"changes": [{"principal": g["principal"], "add": g["add"]}]})
            result["granted"].append({"principal": g["principal"], "securable": g["securable"], "name": g["name"],
                                      "added": g["add"]})
            have = effective(client, g["securable"], g["name"], g["principal"])
            if have is not None and (missing := [x for x in g["add"] if not held(have, x)]):
                result["not_visible_yet"].append(f"{where}: {missing}")
        except Exception as e:  # noqa: BLE001 - one principal's failure doesn't stop the others
            result["failed"].append({"grant": where, "error": f"{type(e).__name__}: {e}"[:500]})
    return result


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    if "grants" not in state["parts"]:
        raise SystemExit("parts has no grants: there's nothing for this script to do")
    folder = args.state.resolve().parent / state["config_dir"]
    cfg, problem = load_yaml(folder / "permissions.yaml")
    services_cfg = None
    if "model_services" in state["parts"]:
        services_cfg, _ = load_yaml(folder / "model-services.yaml")  # which services a deploy creates
    if problem:
        print(json.dumps({"errors": [problem]}, indent=2))
        sys.exit(1)
    client = workspace_client(state).api_client
    p = plan(client, state, cfg, services_cfg)
    if p["errors"] or not args.apply:
        print(json.dumps(p, indent=2))
        sys.exit(1 if p["errors"] else 0)
    result = apply(client, p)
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
