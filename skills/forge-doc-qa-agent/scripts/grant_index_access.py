# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Give a Databricks App's service principal SELECT on an AI Search index (forge-doc-qa-agent module 02).

`agentbricks deploy` grants access only to resources declared in agent.toml, and an AI Search index
isn't a tool kind there. This adds a `uc_securable` TABLE/SELECT app resource, the Apps equivalent
of the Model Serving `vector_search_index` resource. All existing app resources are kept, and
`agentbricks deploy` preserves resources it doesn't own, so this survives redeploys.

    uv run grant_index_access.py --app agent-bricks-<name> --index <catalog>.<schema>.<index_name> --profile <profile>
"""

import argparse
import json
import subprocess

RESOURCE_NAME = "doc-qa-index"


def databricks(args: list[str], profile: str | None) -> dict:
    cmd = ["databricks", *args, "--output", "json", *(["--profile", profile] if profile else [])]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout or "{}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--app", required=True, help="Deployed app name, e.g. agent-bricks-<name>")
    p.add_argument("--index", required=True, help="<catalog>.<schema>.<index>")
    p.add_argument("--profile", default=None)
    args = p.parse_args()

    app = databricks(["apps", "get", args.app], args.profile)
    resources = app.get("resources") or []
    for resource in resources:
        securable = resource.get("uc_securable") or {}
        if securable.get("securable_full_name") == args.index and securable.get("permission") == "SELECT":
            print(f"{args.app} already has SELECT on {args.index} (resource {resource.get('name')!r})")
            return

    resources = [r for r in resources if r.get("name") != RESOURCE_NAME] + [
        {
            "name": RESOURCE_NAME,
            "description": "AI Search index for the search_documents tool",
            "uc_securable": {
                "securable_full_name": args.index,
                "securable_type": "TABLE",
                "permission": "SELECT",
            },
        }
    ]
    payload = {"app": {"resources": resources}, "update_mask": "resources"}
    databricks(["apps", "create-update", args.app, "--json", json.dumps(payload)], args.profile)

    granted = databricks(["apps", "get", args.app], args.profile).get("resources") or []
    if not any((r.get("uc_securable") or {}).get("securable_full_name") == args.index for r in granted):
        raise SystemExit(f"Update finished but {args.index} is not among {args.app}'s resources")
    print(f"granted {app.get('service_principal_name') or 'the app service principal'} SELECT on {args.index}")


if __name__ == "__main__":
    main()
