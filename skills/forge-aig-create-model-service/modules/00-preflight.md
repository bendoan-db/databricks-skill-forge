# 00 · Preflight

Resolve the inputs, confirm the tooling, base skills, and workspace, check that the user holds the Unity Catalog privileges the changes need, and get a go-ahead before anything is created. The resolved values go in `<workdir>/aig-model-services.json`.

## 1. Ask for the requirements

Before anything else, ask the user for these two things in one question, and wait for answers. Don't fill them in from defaults without asking. If the user already answered one, for example in their request to `forge`, confirm that answer in the same question instead of asking it fresh:

| Requirement | Records | What to ask |
|---|---|---|
| Where new model services go | `target_schema` | A Unity Catalog `<catalog>.<schema>`. New model services, their inference tables, and SQL service policies are created there. It must exist; this skill doesn't create schemas. Asked even when the user only wants grants: it's where the template points first |
| What to set up | `parts` | Any of: new or changed model services (`model_services`); guardrails on them (`service_policies`, which needs `model_services`); and access for groups, users, and service principals (`grants`), on these services or ones that already exist |

## 2. Collect the other inputs

Propose defaults for every other input in the SKILL.md Inputs table, in the same message as step 1 or right after it. Derive the following and show them back to the user, with the requirements:

```
catalog, schema     = <target_schema>
config_dir          = ai-gateway                       # <workdir>/ai-gateway/ holds the YAML files
files               = model-services.yaml (model_services), service-policies.yaml (service_policies),
                      permissions.yaml (grants)
default_evaluator   = system.ai.gpt-oss-20b            # only with service_policies
```

Tell the user that the next module writes these files as templates for them to fill in, so they don't need to know the service names, models, or groups yet.

## 3. Tooling and base skills

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 4).

```bash
databricks --version                     # >= v1.11.0 (needed for `ai-gateway`)
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
```

`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`. If it exits non-zero, stop: the modules cite reference files that the installed base skills don't have. Ask the user to install or update databricks-agent-skills (v0.2.27 or later) in the same skills directory, for example with `databricks aitools update`.

## 4. The workspace

```bash
databricks schemas get <catalog>.<schema> --profile <profile>                           # the target schema exists
databricks ai-gateway list-model-services --parent schemas/<catalog>.<schema> --profile <profile>   # Unity Gateway answers; existing services
```

- **The schema:** if it doesn't exist, stop and ask for another. Don't create it.
- **Unity Gateway:** if `list-model-services` fails because the API isn't there, the workspace's region doesn't have Unity Gateway (it isn't on AWS GovCloud). Stop and tell the user.
- **Existing services:** note any with the names the user mentioned. Module 02 plans an update for a service that exists, not a duplicate.
- **Service policies** (only with `service_policies`): a workspace admin must turn on the **Unity Gateway Service Policies** preview, under **Settings > Previews**. No command reports it, so ask the user. If it's off, they can still set up the services and grants now, and add `service_policies` later.
- **A SQL warehouse** (only with `service_policies`): SQL policies are created as functions on it. Record it as `warehouse_id`: `databricks experimental aitools tools get-default-warehouse --profile <profile>`, or pick one from `databricks warehouses list`.

## 5. Write `aig-model-services.json`

Write the resolved values to `<workdir>/aig-model-services.json`, so the permission check in step 6 can read them:

```json
{
  "profile": "<profile>",
  "target_schema": "<catalog>.<schema>",
  "catalog": "<catalog>",
  "schema": "<schema>",
  "parts": ["model_services", "service_policies", "grants"],
  "config_dir": "ai-gateway",
  "default_evaluator": "system.ai.gpt-oss-20b",
  "warehouse_id": "<warehouse_id>",
  "completed": []
}
```

Leave out `default_evaluator` and `warehouse_id` without `service_policies`. Add `test_prompt` if the user wants module 04 to send its own. Later modules add nothing; they read this file and the YAML files.

## 6. Permissions

Check that the user, as the CLI profile, holds what the changes need on the target schema:

```bash
uv run <skill>/scripts/check_access.py --state <workdir>/aig-model-services.json
```

| Part | On | Needs |
|---|---|---|
| `model_services` | the catalog | `USE CATALOG` |
| | the schema | `USE SCHEMA` and `CREATE SERVICE`; `CREATE TABLE` for services that log to an inference table |
| `service_policies` | the schema | `CREATE FUNCTION`, for SQL policies |
| | `default_evaluator` | `EXECUTE`, for LLM-based policies |
| `grants` | the catalog and schema | `MANAGE` or ownership, to grant `USE CATALOG` and `USE SCHEMA` to people who don't have them |

**Load:** `databricks-unity-gateway` `references/permissions.md` (creation and management access) for what each privilege allows.

The script reports each check as `held` (with where it comes from: a grant to the user, to `account users`, or to one of their groups, on the object or inherited from its catalog or schema, or ownership), `missing`, or `unknown`, when the grants can't be read. It exits 1 when a required check is missing.
- **Missing:** tell the user which privilege, on what, and for which part. Don't grant it, and don't try a workaround. Ask them to get it from the object's owner or an admin, or to drop that part, then run the check again.
- **Unknown:** ask the user to confirm with the object's owner. A metastore admin, or a privilege through a nested group, can hold it without the script seeing it.
- **Optional** checks (`required: false`), such as `CREATE TABLE`, matter only if the YAML files use them. Mention them now; module 02 checks again against the filled-in files.

If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md` (privileges, ownership, and inheritance).

## 7. Go-ahead

Summarize what the build creates and what it costs, then wait for an explicit yes:

- **Model services:** a service isn't billed by itself. Every request through it is billed per token by the model it routes to, and its inference table grows with each request.
- **Service policies:** an LLM-based policy calls its evaluator model on every request it checks, billed per token. Creating SQL policies runs a statement on the SQL warehouse, which starts it if it's stopped.
- **Grants:** they let the people listed query the services, and spend on them.
- **Checks (module 04):** one short test prompt per service, plus the evaluator calls its policies make.

Nothing is created until module 03, after the user has seen the plan.

## Verify

- The user answered both requirements in step 1, and every other input is resolved and echoed back.
- `<catalog>.<schema>` exists, and `list-model-services` answers.
- With `service_policies`, the user confirmed the preview is on, and `warehouse_id` is recorded.
- `check_base_skills.py` exits 0, and `check_access.py` exits 0, or the user dropped the parts whose privileges are missing.
- The user approved the go-ahead, and `aig-model-services.json` exists.
