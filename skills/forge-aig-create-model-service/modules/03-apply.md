# 03 · Apply

Create or update the model services and their service policies, then grant access, exactly as module 02 planned. Only after the user's go-ahead.

## Inputs → outputs

- **Inputs:** the plan the user reviewed in module 02, the YAML files, and `aig-model-services.json`.
- **Outputs:** the SQL policy functions, the model services as `model-services.yaml` describes them with their policies attached, and the grants in `permissions.yaml`. Plans run afterward show nothing left to do.

## 1. Confirm

Ask for an explicit yes, naming what will be created or changed: the services, the SQL functions (and that the warehouse runs a statement for each), and the principals that get access. If the files changed since module 02, plan again first.

## 2. Create and update the services

Only with `model_services`:

```bash
uv run <skill>/scripts/deploy_services.py --state <workdir>/aig-model-services.json --apply
```

It plans again, and changes nothing if the plan has errors. Then it:
1. creates or replaces a function `<catalog>.<schema>.<policy name>(event VARIANT) RETURNS VARIANT` for each SQL policy a service lists, on `warehouse_id`;
2. creates each new service, and updates each existing one with only the fields that differ;
3. sets each service's policies, in the order listed (with `service_policies` in `parts`);
4. reads each service back, and fails it if it doesn't match.

It prints `applied`, `unchanged`, and `failed`. A failed service doesn't stop the others, and a service whose SQL function failed is skipped. It exits 1 when anything failed: show the user each error, fix the cause (module 02, Troubleshooting), and run it again. It's safe to rerun: what already matches is left alone.

A new service can be used only by its creator until step 3 grants access.

## 3. Grant access

Only with `grants`, and after step 2:

```bash
uv run <skill>/scripts/grant_access.py --state <workdir>/aig-model-services.json --apply
```

It grants only what each principal is missing, then reads it back. `not_visible_yet` lists grants it can't see yet; they usually appear within a few minutes. It exits 1 when a grant failed: show the user the error. It only adds: a principal taken out of `permissions.yaml` keeps its access.

## Removing access or a service

Neither script deletes or revokes anything. When the user asks to:

- **Revoke access:** show the exact principal, privilege, and service, and get a confirmation naming them. Revoke `EXECUTE` on the service, not shared `USE CATALOG` or `USE SCHEMA`. **Load:** `databricks-unity-gateway` `references/permissions.md` (revoking access).
- **Delete a service,** for example to move its inference table: show the service with `get-model-service`, say that its callers stop working, and get a separate confirmation naming it. Deleting doesn't drop its inference table. **Load:** `databricks-unity-gateway` `references/model-services.md` (deleting a model service).

Then take it out of the YAML files too, so the next apply doesn't create or grant it again.

## Verify

- `deploy_services.py --apply` and `grant_access.py --apply` exit 0, for the parts in `parts`.
- Planning again (`deploy_services.py` and `grant_access.py`, without `--apply`) shows every service `unchanged` or only `re-attach` steps, and every grant `ok`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PERMISSION_DENIED` creating a service | The user lacks `CREATE SERVICE` or `USE SCHEMA` on the schema, `USE CATALOG` on the catalog, or `EXECUTE` on a destination. Run `check_access.py --config` (module 02, step 3) |
| A destination doesn't exist, on create | The plan read it, so the model may not be available in the workspace's region. Pick another, and plan again |
| The `config.service_policies` update fails | The Service Policies preview is off, or the API changed. Attaching policies by API isn't documented yet: attach them in the service's **Policies** tab in the AI Gateway UI, then rerun to check |
| `policy function ... cannot be transpiled to CEL` | The SQL uses something a policy can't. Rewrite it (module 02, Troubleshooting), and rerun |
| `creating the function` fails | The SQL doesn't parse, or the user can't replace an existing function they don't own. Test the expression in a `SELECT` on a sample event, such as `SELECT (<sql>) FROM (SELECT parse_json('{"type":"request","context":{"message":"hi"}}') AS event)` |
| `read back after the change` | The service didn't take a field as sent. Show the user the field, and check it in the AI Gateway UI |
| A grant to a group fails | The group is workspace-local, or its name is wrong. Use the exact name of an account-level group |
