---
name: forge-aig-create-model-service
description: "Forge use case, normally started by the forge skill: sets up Unity AI Gateway model services from YAML templates the user fills in. It checks the user's Unity Catalog privileges first, then writes model-services.yaml (routing, traffic splits, fallbacks, rate limits, inference tables), service-policies.yaml (guardrails: built-in jailbreak, unsafe-content, hallucination, and sensitive-data policies, and custom LLM-judge and SQL rules), and permissions.yaml (which account groups, users, and service principals can use which services). Once they're filled in, it plans every change read-only, then creates or updates the services, attaches their policies in order, grants only the access that's missing, and checks that each service matches its file, answers through the gateway, and has its grants in effect. Use when forge hands off to it, or when the user names this skill."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, and the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill, in a workspace with Unity Gateway; service policies also need the Unity Gateway Service Policies preview
metadata:
  version: "0.1.0"
  forge_kind: use-case
---

# Model services and access on Unity AI Gateway

Sets up Unity AI Gateway model services, their guardrails, and who can use them, from YAML files the user fills in. It checks the user's privileges before anything else, then writes the three files as templates, with every setting explained in their comments, and waits while the user fills them in. It checks the filled-in files against the workspace and shows the plan, and only after the user's go-ahead creates the services, attaches their service policies, and grants access. Running it again makes the workspace match the files.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
00 check_access.py ─► your privileges on <target_schema> for each part: CREATE SERVICE, CREATE TABLE, CREATE FUNCTION, MANAGE
01 render.py ─► <config_dir>/  model-services.yaml   service-policies.yaml   permissions.yaml
                               the user fills in every <...>
02 deploy_services.py, grant_access.py ─► read-only plans     check_access.py --config ─► access to what the files name
03 deploy_services.py --apply
     ├─► SQL warehouse ─► <catalog>.<schema>.<policy>(event VARIANT) RETURNS VARIANT, one per SQL policy
     └─► model service <catalog>.<schema>.<name>
           routing: destinations (traffic %) ─► fallbacks · per-user rate limits · inference table <name>_payload
           service policies, in rank order: built-ins · LLM judges (on an evaluator model service) · SQL functions
   grant_access.py --apply ─► EXECUTE (or more) on each service · USE CATALOG and USE SCHEMA on its parents
04 evaluate.py ─► each service matches its file · answers /ai-gateway/mlflow/v1/chat/completions · grants in effect
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `aig-model-services.json` and the `<config_dir>/` folder of YAML files live. Default: the current directory.
- `<workdir>/aig-model-services.json` holds every resolved value. Module 00 creates it, and each module reads it first. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<catalog>`. What to create is in the YAML files, which are the user's: the scripts read them and never rewrite them.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to write the templates; never copy them from `scripts/` by hand.

## Inputs

Module 00 asks for the first two before anything else, and doesn't continue until the user answers them. Offer the defaults for the rest. The services, policies, and grants themselves go in the YAML files, in module 01.

| Input | Default | Notes |
|---|---|---|
| `target_schema` | — (asked) | `<catalog>.<schema>`, which must exist. New model services, their inference tables, and SQL policy functions are created there |
| `parts` | — (asked) | Any of `model_services`, `service_policies` (needs `model_services`), and `grants` |
| `profile` | from `databricks-core` | CLI profile for every command; the identity whose privileges module 00 checks |
| `config_dir` | `ai-gateway` | The folder in `<workdir>` for the YAML files |
| `default_evaluator` | `system.ai.gpt-oss-20b` | The model service that runs LLM-based policies, unless a policy sets its own. Only with `service_policies` |
| `warehouse_id` | the default warehouse | The SQL warehouse that creates SQL policy functions. Only with `service_policies` |
| `test_prompt` | "What is Databricks? Answer in one sentence." | The prompt module 04 sends each service |

## Guardrails

- **Ask for the requirements first** (module 00, step 1), and check the user's privileges before writing anything (step 6). Don't assume answers.
- **The YAML files are the user's.** Wait for them to fill them in (module 01). Write into them only what the user tells you, show the diff, and never replace or delete one.
- **Plan, then confirm, then apply.** Show the read-only plan (module 02), and get an explicit go-ahead before creating or updating a service, creating a SQL function, or granting anything (module 03). A change to the files means planning again.
- **Never grant the user privileges** they're missing, and never work around a missing one. Report it.
- **Grant only to the principals in `permissions.yaml`,** and give `MANAGE` only where it's listed.
- **Never revoke or delete** without a separate confirmation that names the exact principal and privilege, or service (module 03, Removing access or a service). The scripts do neither.
- **Never ask for or store tokens.** Use the CLI profile. The state file and YAML files hold names and settings only.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `aig-model-services.json` when its Verify passes:

- [ ] 00 Preflight: both requirements answered, the schema and Unity Gateway found, base skills present, privileges checked, go-ahead given, `aig-model-services.json` written
- [ ] 01 Template: the YAML files written and filled in by the user, no `<...>` left
- [ ] 02 Check and plan: both plans and the access check pass, and the user reviewed the plan
- [ ] 03 Apply: the services, policies, and grants applied, and planning again shows nothing left to do
- [ ] 04 Evaluate: every service matches and answers, and every grant is in effect

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `aig-model-services.json`, privilege check, go-ahead | databricks-core, databricks-unity-gateway, databricks-unity-catalog |
| 01 | [Template](modules/01-template.md) | `<config_dir>/` YAML files, filled in by the user | databricks-unity-gateway |
| 02 | [Check and plan](modules/02-plan.md) | read-only plans, access check, the user's review | databricks-unity-gateway |
| 03 | [Apply](modules/03-apply.md) | model services with their policies, SQL policy functions, grants | databricks-unity-gateway |
| 04 | [Evaluate](modules/04-evaluate.md) | pass/fail report against thresholds | databricks-unity-gateway |

The working directory ends up with the state file and the YAML files:

```
<workdir>/
├── README.md                # the project explained for whoever opens the folder; write_readme.py writes it
├── aig-model-services.json  # resolved values; the scripts read it
└── <config_dir>/            # the YAML files the user fills in: model-services.yaml, service-policies.yaml, permissions.yaml
```

**The README.** Each time a module's Verify passes, module 00's included, update `<workdir>/README.md`:

```bash
uv run <skill>/scripts/write_readme.py --state <workdir>/aig-model-services.json
```

It explains the project to whoever opens the folder, from this skill and every Forge state file in `<workdir>`: what it builds, its settings, and each module's purpose, outputs, and status. Its Notes section is the user's, and is kept. If the script reports `kept_edited`, someone edited the README outside Notes: tell the user, and pass `--force` only if they agree. A README.md that `<workdir>` already had is left alone, and the project's goes to `FORGE-README.md`.

**Resuming.** If `<workdir>/aig-model-services.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 3). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Add or change a service, a policy, or a grant | Edit the YAML files with the user (01, step 2), then 02 and 03 |
| Give more people access | Add them to `permissions.yaml`, then 02, step 2, and 03, step 3 |
| Add guardrails later | Add `service_policies` to `parts`, then 01, step 1 (it writes only the missing file), then 02 |
| Check everything is still in place | 04 |
| Remove access, or delete a service | 03, Removing access or a service |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Parts | Asked in module 00 | Only access to services that already exist, such as granting a group on `system.ai` services: `grants` alone. Only routing: leave out `service_policies` |
| Destinations | One model, with a fallback | Users need A/B tests or a gradual move to a new model: several destinations with `traffic_percentage`. A destination's provider fails or throttles: more fallbacks |
| Guardrails | The built-ins the user lists, blocking | A custom rule: an `llm_judge` for judgment calls, or `sql` for fixed rules (cheaper, no model call). A new policy: `mode: log` first, then `block` once its decisions look right |
| Inference tables | On | The prompts hold data that mustn't be stored, or nobody will read them: `enable_inference_table: false`. They're the requests' audit trail, readable by anyone with `SELECT` on the table |
| Rate limits | Per user: 60 requests and 100,000 tokens a minute | A service needs more or less: its own `rate_limits`. Limits per group or service principal aren't in the template, and an apply replaces a service's rate limits; set them on a service this skill doesn't manage |
| Who can manage | `EXECUTE` for everyone listed | The people who run a service: add `MANAGE` for them, and only them |

## Done when

- Module 04 meets its thresholds: `config_match_rate`, `answer_rate`, and `grant_rate` ≥ 1.0, for the parts in `parts`.
- Planning again shows nothing left to do.
- `<workdir>/README.md` describes the finished project: `write_readme.py` ran after the last module.
- You told the user each service's three-part name, its policies, who can use it, where the YAML files are, and that editing them and running the skill again updates the workspace. If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Service policies:** `databricks-unity-gateway` doesn't cover guardrails on model services. The [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) describe the built-ins and custom policies, and attach them in the UI. `deploy_services.py` attaches them through the model service's `config.service_policies` field, as the UI does.
- **REST, not the typed CLI:** `databricks-unity-gateway` says to manage services and grants with the CLI, not REST or the SDK. This skill's scripts call the REST API with the CLI profile, because the typed CLI and SDK drop `config.service_policies`. They plan read-only first, and change nothing without the user's go-ahead.
- **Reading a service's policies:** `databricks ai-gateway get-model-service` leaves out `config.service_policies`. Read them with `databricks api get /api/2.1/unity-catalog/model-services/<name>`.
- **Destination names:** `databricks-unity-gateway` shows destinations as `models/<catalog>.<schema>.<model>`. A `system.ai` destination is the model, which usually starts with `databricks-` (`system.ai.databricks-claude-sonnet-5-5`), not the model service of the same model (`system.ai.claude-sonnet-5-5`). Policy evaluators are model services.
- **Custom policies:** SQL policies are Unity Catalog functions the gateway transpiles to CEL, so some SQL can't be used ([function reference](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/policy-function-reference)). `deploy_services.py` rejects the constructs known to fail.
