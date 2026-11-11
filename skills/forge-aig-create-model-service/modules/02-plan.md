# 02 · Check and plan

Check the filled-in files against the workspace, check the user's access to everything they name, and show the user exactly what module 03 will create, change, and grant. Everything here is read-only.

## Inputs → outputs

- **Inputs:** the YAML files from module 01, and `aig-model-services.json`.
- **Outputs:** a plan with no errors for the services and their policies, and for the grants; an access check with nothing required missing; the user's review of the plan.

## 1. Plan the model services

Only with `model_services`:

```bash
uv run <skill>/scripts/deploy_services.py --state <workdir>/aig-model-services.json
```

It checks `model-services.yaml` and `service-policies.yaml` first: names, providers, traffic that adds up to 100, policies that are defined and valid, SQL policies the gateway can't transpile, and `<...>` values left. Then it reads the workspace: the schema, each existing service, each destination model (reading one needs `EXECUTE`), endpoint, and model provider service, each evaluator, and each new service's inference table.

It prints `errors` and `warnings`, the SQL functions it would create, and each service as `create`, `update` (with the fields that differ), or `unchanged`, with its policy steps: `attach`, `update`, `keep`, `detach`, or `re-attach` (a SQL policy, whose function is replaced on every run). It exits 1 on any error.

- **Errors:** show them to the user, fix the file with them (as in module 01, step 2), and run the plan again, until it exits 0. Most name the fix, such as the model behind a model service's name.
- **Warnings:** tell the user, such as a policy no service lists, or policies ignored because `service_policies` isn't in `parts`.
- **External destinations:** if the plan can't read a model provider service, **load** `databricks-unity-gateway` `references/model-provider-services.md` (creating one, and its credential). This skill doesn't create them.

## 2. Plan the grants

Only with `grants`:

```bash
uv run <skill>/scripts/grant_access.py --state <workdir>/aig-model-services.json
```

It checks `permissions.yaml`, looks up each principal (a workspace-local group is an error, and a principal that isn't in the workspace is a warning), reads each service, and reads what each principal already holds, so it grants only what's missing: `USE CATALOG`, `USE SCHEMA`, and the privileges listed. Each grant is `grant` (with `add`), `ok` (already held), `after deploy` (a service step 1 creates), or `unknown` (its grants can't be read, so it grants them all; a grant that's already there changes nothing). Fix errors with the user, as in step 1.

## 3. Check your access to what the files name

```bash
uv run <skill>/scripts/check_access.py --state <workdir>/aig-model-services.json --config
```

Module 00 checked the target schema. This checks what the filled-in files name: `CREATE SERVICE` and `CREATE TABLE` where new services go, `MANAGE` on each existing service the plan updates, `EXECUTE` on each destination and evaluator, `CAN MANAGE` on each provisioned-throughput endpoint, `CREATE FUNCTION` for SQL policies, and the right to grant on each service in `permissions.yaml` (and on its catalog and schema, when someone listed lacks `USE CATALOG` or `USE SCHEMA`). It exits 1 when a required check is missing.

Handle it as in module 00, step 6: report what's missing, never grant it yourself, and ask the user to get it or change the file. **Load:** `databricks-unity-gateway` `references/permissions.md` (creation and management access) for what each privilege allows.

## 4. Show the user the plan

Show the user, briefly:
- each service to create or update, with the fields that change, and its policies in order;
- the SQL functions to create or replace, and the warehouse;
- the grants to add, by principal;
- what stays as it is.

Then ask for the go-ahead to apply it in module 03. A change to the files after this means planning again.

## Verify

- `deploy_services.py` and `grant_access.py` (for the parts in `parts`) exit 0 with no errors, and the user has seen their warnings.
- `check_access.py --config` exits 0.
- The user reviewed the plan.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `isn't a Unity Catalog model; ... its model is system.ai.databricks-...` | The destination is a model service's name. Use the model the error names |
| `isn't a model service; evaluators are model services` | The evaluator is a model. Drop its `databricks-` prefix, or pick one from `list-model-services --parent schemas/system.ai` |
| `can't be read (you need EXECUTE on it)` | Ask the model's owner for `EXECUTE`, or route to another model |
| `its inference table ... already exists` | A service of that name logged there before. Rename the service, or set `enable_inference_table: false` |
| `its inference table can't change after it's created` | Set `enable_inference_table` to match the service, or delete and recreate it: module 03, Removing access or a service |
| `can't be transpiled to CEL` (in the plan) | Rewrite the SQL without the construct named, such as nesting two-argument `COALESCE` calls |
| `is workspace-local` | Unity Catalog grants need an account-level group. Use one with the same members; **load** `databricks-unity-gateway` `references/account-access.md` to find or create one |
| `doesn't exist, and model-services.yaml doesn't create it` | The service name in `permissions.yaml` is wrong, or the service is in `model-services.yaml` under another name |
| `check_access.py` reports `unknown` | Its grants can't be read as the user. Ask the object's owner to confirm the privilege |
