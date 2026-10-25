# 02 · Deploy as a Databricks App

Deploy the agent to Agent Runtime on Databricks Apps, give the app's service principal access to the index, and smoke-test the live app.

**Docs:** no base skill covers this yet. Use [Agent Runtime](https://docs.databricks.com/aws/en/agents/deploy/agent-runtime) and [Query deployed agents](https://docs.databricks.com/aws/en/agents/query-llms).

## Inputs → outputs

- **Inputs:** the `<name>/` project (module 01), `doc-qa.json`, and the index from `doc-index.json`.
- **Outputs:** app `agent-bricks-<name>`, a URL that serves the chat UI and `/api/invocations`, and the index granted to the app's service principal.

## 1. Deploy, after the user confirms

Deploying creates the app, a Databricks-managed Lakebase Runtime Store (durable runs), the bound Session Store, and the tracing grants. Restate this and get a yes.

```bash
cd <workdir>/<name>
agentbricks --profile <profile> deploy <name>          # app: agent-bricks-<name>; records <name> in agent.toml
```

`deploy` grants the app what `agent.toml` declares: stores, tracing, and declared tools. `system.ai` model services need no grant.

## 2. Grant the index

The index isn't an `agent.toml` resource, so grant it explicitly. Without this grant, every search fails with `PERMISSION_DENIED`, and the agent answers "not found".

```bash
uv run <skill>/scripts/grant_index_access.py --app agent-bricks-<name> \
  --index <catalog>.<schema>.<index_name> --profile <profile>
```

This adds a `uc_securable` (TABLE, SELECT) app resource and keeps the existing ones. Later `agentbricks deploy` runs preserve resources they don't own.

If the workspace rejects the resource, **load** `databricks-apps-python` `references/2-app-resources.md` and `databricks-unity-catalog` `references/1-access-control.md`, and grant the privileges directly instead. Use the app's `service_principal_client_id` from `databricks apps get agent-bricks-<name> -o json`:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<sp-client-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<sp-client-id>`;
GRANT SELECT ON TABLE <catalog>.<schema>.<index_name> TO `<sp-client-id>`;
```

## 3. Smoke-test the live app

```bash
agentbricks --profile <profile> deployments get agent-bricks-<name>        # status + URL
agentbricks --profile <profile> endpoint invoke agent-bricks-<name> --path /api/invocations \
  --json "{\"id\":\"$(uuidgen)\",\"session_id\":\"$(uuidgen)\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"<question 1 from module 01>\"}]}}"
agentbricks --profile <profile> deployments logs agent-bricks-<name>        # if anything fails
```

Record the URL as `app_url` in `doc-qa.json`, and open it in a browser to check the chat UI.

Clients call `POST <app_url>/api/invocations` with a fresh UUID `id` per turn and a stable `session_id` per conversation. They need a Databricks **OAuth** token, because Apps reject PATs. If you scale out with `--instances`, send `X-Routing-Key: <session_id>` so each conversation stays on one instance.

## 4. Share, only on request

Ask who should use the app. Grant `CAN_USE` only to the users or groups the user names:

```bash
databricks apps update-permissions agent-bricks-<name> --profile <profile> \
  --json '{"access_control_list": [{"group_name": "<group>", "permission_level": "CAN_USE"}]}'
```

Everyone who can use the app sees answers from the whole index, because retrieval runs as the app's service principal. If that crosses a data boundary, stop and revisit the tool-identity decision in SKILL.md. For per-user access, **load** `databricks-apps-python` `references/1-authorization.md`.

## Iterating

Edit `agent/`, test with `agentbricks dev`, then run `agentbricks deploy` again. It reuses the app, the stores, and the index grant. New documents need no redeploy: the agent queries the index live, and the `forge-unstructured-doc-extraction` job refreshes it (on file arrival with continuous updates, or when rerun).

## Verify

- `deployments get` shows the app RUNNING.
- The live smoke test returns a cited answer that matches the local answer to the same question.
- Run `databricks apps get agent-bricks-<name> -o json`. Its resources include `doc-qa-index` plus the `agentbricks-*` resources that deploy owns.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Deployment name ... is too long` | `<name>` must be ≤17 chars; the app name adds `agent-bricks-` |
| Live answers say "not found" but local answers work | Index grant missing — rerun step 2, check logs for `PERMISSION_DENIED` |
| `401` calling the app from a script | Use an OAuth token (`databricks auth token --profile <profile>`), not a PAT |
| App build can't download packages (egress-restricted workspace) | `deploy` defaults `--pip-index-url` to public PyPI; pass the customer's mirror: `agentbricks deploy <name> --pip-index-url <url>` |
| Deploy stops before upload with a grant error | Deployer lacks rights to grant a declared resource; current deployment keeps running — fix permissions and redeploy |
| Want to switch agent server later | Not supported in place; create a new project with the other `--server` and deploy under a new name |
