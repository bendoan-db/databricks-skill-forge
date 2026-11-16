# forge-aig-create-model-service design notes

Maintainer notes for `skills/forge-aig-create-model-service`, which sets up Unity AI Gateway model services, their service policies, and access to them, from YAML templates the user fills in. Built from [unity-ai-gateway-utilities](https://github.com/bendoan-db/unity-ai-gateway-utilities) at `817b0b7`, owner direction 2026-11-10:
- generate a template the user populates, to quickly create model services and service policies, and grant access to them;
- once the template is written, prompt the user to fill out the YAMLs;
- preflight checks the user's Unity Catalog privileges for the model-service creation and the other steps.

This file isn't published.

## Where the source went

| Source | Went to |
|---|---|
| `deploy-uaig/model-services.yaml`, `service-policies.yaml`, `permissions.yaml` | `scripts/` templates of the same names, which `render.py` writes into `<workdir>/<config_dir>/`, with the target catalog, schema, and evaluator filled in and `<...>` for the rest. Their comments are the source's, shortened, plus the destination-name rule |
| `deploy-uaig/deploy_model_service.py` (the builders, create or update, SQL policy functions, attaching policies) | `scripts/deploy_services.py`: the same builders and wire format, with a read-only plan in front |
| `deploy-uaig/grant_permissions.py` | `scripts/grant_access.py`: the same grants, planned against what each principal already holds |
| `skill/uaig-test-utilities/SKILL.md`: the wire format, destination names, the SQL constructs that don't transpile, the inference-table rule, troubleshooting | The validation in `deploy_services.py`, module 01's table of mistakes, module 02-04 troubleshooting, and `facts` |
| The README's privilege table | `scripts/check_access.py`, module 00 step 6, and `facts` |
| `skill/uaig-test-utilities/scripts/dry_run.py` (the notebooks against a fake client) | Not ported: the plan replaces it, and the tests' `FakeClient` plays the same part |
| The notebooks' run-from-the-workspace path (`import-dir`, `jobs submit`) | Not ported. The owner chose local scripts (2026-11-10): control-plane calls need no job or compute, and the plan and access check run before any change, as `forge-aig-evaluate-guardrails`' `guardrail_service.py` did |

## What changed, and why

- **Check, then plan, then apply.** The source builds every request body before it changes anything, then applies. The skill adds a read-only plan the user reviews (module 02) with what the source learned the hard way checked up front:
  - a destination that's a model service's name, not a model (the most common mistake, with the model named in the error);
  - an evaluator that's a model;
  - an inference table that already exists, which makes create fail (docs), or that would move;
  - SQL the gateway can't transpile to CEL;
  - `<...>` values left unfilled.
- **Only what differs.** An update sends only the fields whose values differ from the service as read, where the source always masked all five. A service that matches is left alone, so a rerun is a no-op, and module 04 measures "matches" with the same plan. A SQL policy is still re-attached on every run, since `CREATE OR REPLACE` can give its function a new id (the source's finding); "matches" ignores that re-attach.
- **Policies are left alone without the part.** With `service_policies` not in `parts`, the plan never sets `config.service_policies`, so policies attached in the UI survive. The source always set them, detaching any the YAML didn't list.
- **Grants only what's missing.** The plan reads each principal's effective privileges, so `USE CATALOG` and `USE SCHEMA` are granted only to principals without them. Where nobody needs them, the runner needs no `MANAGE` on the catalog or schema. Like the source, nothing is ever revoked.
- **Principals are checked.** A workspace-local group is an error, since Unity Catalog grants need account-level groups. A principal the workspace's SCIM API doesn't know is only a warning: an account group that isn't assigned to the workspace can still get a grant.
- **The permission check.** `check_access.py` is new, and runs twice: in preflight on the target schema, before the files exist, and in module 02 on everything the filled-in files name. A privilege is held when it's granted to the user, to `account users`, or to a group from `SCIM /Me`, on the object or inherited from its catalog or schema (the effective-permissions API), or through ownership. Granting is held when the user owns the object, its schema, or its catalog, or is a metastore admin, per the ownership docs. Reading a registered model needs `EXECUTE`, so a model destination is checked by reading it. Grants that can't be read are `unknown`, not missing, so a user isn't blocked by what the script can't see.
- **REST with the CLI profile.** `databricks-unity-gateway` says to use the typed CLI. The skill uses REST for two reasons: the typed CLI and SDK drop `config.service_policies`, and the SDK's `Privilege` enum (0.164.0) has no `CREATE_SERVICE`, so typed grant reads would lose it. This is an override in SKILL.md.
- **SQL policy functions on a warehouse.** The source ran `CREATE OR REPLACE FUNCTION` with `spark.sql` in its notebook. The skill runs it through the Statement Execution API on `warehouse_id`, which preflight resolves.
- **The gate.** `THRESHOLDS = {"config_match_rate": 1.0, "answer_rate": 1.0, "grant_rate": 1.0}`: a setup either matches, answers, and is granted, or it isn't. A reply blocked by a policy counts as an answer. A fail-closed denial doesn't: it means the policy's evaluator can't reach a verdict.
- **Defaults.** The starter service routes to Claude Sonnet 5.5's model, `system.ai.databricks-claude-sonnet-5-5` (the repo's default model), with `system.ai.gpt-oss-120b` as its fallback. The default evaluator is the source's, `system.ai.gpt-oss-20b`. The starter service lists `jailbreak_detection` and `pii_redaction` when the user chose `service_policies`.

## Routing

The router sent "a model service on AI Gateway, or its rate limits, budgets, or access" to `databricks-unity-gateway`. Now creating or changing model services, their guardrails, and access go to this skill. Budgets and alerts, MCP and model provider services, migration, and querying stay with the base skill. The routing case "Add rate limits and a budget to our model service" became a budget-only case.

Setting up guardrails is this skill, and measuring them is `forge-aig-evaluate-guardrails`. The routing cases include a custom-guardrail setup to keep them apart. Since 0.1.1 (2026-11-12), the evaluation builds on this skill: it lists it under `requires`, runs it to create the services it measures, passing the YAML entries it needs, and reads its state file and YAML files without writing them (`docs/aig-evaluate-guardrails-design.md`). So the catalog entry's `next` is the evaluation, which measures a copy of the user's services, never the services themselves. Module 00 confirms what a calling use case passed, module 01 writes its entries with the user's confirmation, and Done when returns to it.

## Running in Genie Code in a notebook

Since 0.2.0 (2026-11-12), the skill runs in Genie Code in a notebook as well as locally, with `forge-aig-evaluate-guardrails` (the decisions are in `docs/aig-evaluate-guardrails-design.md`, Running in Genie Code in a notebook). Module 00 records `runtime`, or takes it from the use case that runs this skill. `profile` is needed only locally, and every script signs in through `workspace_client(state)`. In a notebook, the scripts run in the notebook's own process through `forge_run`, since only that process is signed in as the notebook's user, and `evaluate.py` runs the two plan scripts with `runpy` rather than a subprocess, in both places. SKILL.md, In a notebook, maps each CLI command in the modules to its REST equivalent, including the revoke and delete in module 03, which still need their own confirmations.

## Not yet verified

Nothing has run in a workspace. The tests cover the following, offline:
- the templates (rendered, then filled, pass the checks);
- every validation rule;
- the policy wire format against the source's documented examples;
- create, update with only the changed fields, and a rerun that changes nothing, against `FakeClient`;
- a failed SQL function skipping only its services;
- grant planning with inherited and `ALL PRIVILEGES` grants;
- the permission check's held, missing, and unknown cases;
- the gate.

These are assumptions:
1. **Attaching policies by API.** The PATCH with `update_mask=config.service_policies` isn't documented. The source uses it, as the UI does, and so did `forge-aig-evaluate-guardrails` 0.1.0, which now creates its services through this skill.
2. **The read-back shapes.** Destinations, rate limits, and policies read back with the fields as sent, plus fields of the server's own. The comparison is a subset match. A custom policy's handler may read back as its function's id, so it isn't compared.
3. **REST paths.**
   - `GET /api/2.1/unity-catalog/model-provider-services/<name>`, by analogy with model services.
   - The effective-permissions API with `max_results=0` and a page token, from the SDK's docstring.
4. **Effective permissions without a principal filter** return every principal's assignments, and need more than read access on some objects. The script falls back to one call per principal.
5. **A group's `meta.resourceType`** is `WorkspaceGroup` for a workspace-local group in the workspace SCIM API.
6. **Registered models** can be read only with `EXECUTE` (or ownership or metastore admin), so a successful read proves it.
7. **Inference tables.**
   - Whether an update can add an inference table to a service that has none.
   - Whether an update can clear one (the plan warns).
   - Whether patching `config.inference_table` with an unchanged value is accepted.
8. **The system.ai model names** in the templates (`system.ai.databricks-claude-sonnet-5-5`, `system.ai.databricks-claude-opus-5-5`, `system.ai.gpt-oss-120b`) exist in the sandbox's region.
8. **In a notebook.** `WorkspaceClient()` in the notebook's process reaches every API the scripts call; listing model services takes `parent=schemas/<catalog>.<schema>` as a query parameter, as the CLI's flag does; and Genie Code stops for each go-ahead rather than letting its tool-approval classifier approve the step.

