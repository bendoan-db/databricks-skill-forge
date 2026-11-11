# 01 · Template

Write the YAML files that describe the model services, their guardrails, and who can use them, then have the user fill them in. Nothing in the workspace changes in this module.

## Inputs → outputs

- **Inputs:** `parts`, `catalog`, `schema`, `config_dir`, and `default_evaluator` from `aig-model-services.json`.
- **Outputs:** in `<workdir>/<config_dir>/`, the file for each part, filled in by the user, with no `<...>` values left: `model-services.yaml`, `service-policies.yaml`, and `permissions.yaml`.

## 1. Write the templates

```bash
uv run <skill>/scripts/render.py --state <workdir>/aig-model-services.json
```

It writes one file per part, with the target catalog and schema and the default evaluator filled in, and lists under `to_fill` the `<...>` values the user still has to give. It never replaces a file that's already there: that's the user's, and it's listed under `kept`. To start one over, the user moves it aside and you render again.

| File | Part | What the user fills in |
|---|---|---|
| `model-services.yaml` | `model_services` | Each service: its name and description, the models it routes to (with a traffic split if there are several), fallbacks, rate limits, whether it logs to an inference table, and its service policies, in order. It starts with one service on Claude Sonnet 5.5's model, `system.ai.databricks-claude-sonnet-5-5`, falling back to `system.ai.gpt-oss-120b` |
| `service-policies.yaml` | `service_policies` | The guardrails, defined once and listed by name on each service. Three built-ins are defined already (`jailbreak_detection`, `unsafe_content_detection`, `pii_redaction`), with a custom LLM judge and a SQL rule as commented examples |
| `permissions.yaml` | `grants` | Each group, user, or service principal, the services it can use (three-part names), and its privileges (default `EXECUTE`) |

Each file's comments explain every setting.

## 2. Ask the user to fill them in

Tell the user where the files are, and what each one is for, from the table in step 1. Ask them to fill in every `<...>` value and to tell you when they're done. Then wait: don't go on to module 02 until they say so.

Offer two ways to do it, and follow the one they choose:
- **They edit the files** in their editor, and tell you when they're done.
- **They describe what they want,** and you write it into the files. Show them the diff and ask them to confirm it. Don't invent names, models, or groups they didn't give you: ask.

Point out the mistakes that are easy to make:

| In | The rule |
|---|---|
| `destinations`, `fallbacks` | A `databricks` destination is a Unity Catalog **model**, such as `system.ai.databricks-claude-sonnet-5-5`, not the model service `system.ai.claude-sonnet-5-5`. Most `system.ai` models start with `databricks-` and their services don't; `system.ai.gpt-oss-120b` is both |
| `evaluator`, `default_evaluator` | The opposite: a **model service**, such as `system.ai.gpt-oss-20b` |
| `traffic_percentage` | Only with more than one destination, on each one, adding up to 100. Never on a fallback |
| `enable_inference_table` | The table is `<catalog>.<schema>.<name>_payload`. Its location can't change once it's created, so choose the service's name with that in mind |
| `permissions.yaml` | Groups must be account-level. Service names are `<catalog>.<schema>.<name>`, matching `model-services.yaml`: rename a service in one file, and rename it in the other |
| `service_policies` | A name listed on a service must be defined in `service-policies.yaml`. Use `mode: log` while trying a custom policy: a policy that can't reach a verdict denies the request |

When they need details to decide, such as a rate-limit key or an external model's API type, **load** `databricks-unity-gateway` `references/model-services.md` (destination types, fallbacks, rate limits, and inference tables). For whether a group is account-level, **load** `databricks-unity-gateway` `references/account-access.md`.

To help them choose, look things up, read-only:

```bash
databricks registered-models list --catalog-name system --schema-name ai --profile <profile>   # system.ai models, for destinations
databricks ai-gateway list-model-services --parent schemas/system.ai --profile <profile>       # system.ai model services, for evaluators
databricks ai-gateway list-model-services --parent schemas/<catalog>.<schema> --profile <profile>   # services already in the schema
databricks groups list --profile <profile>                                                       # groups in this workspace
```

## 3. Confirm they're filled in

When the user says they're done, render again. It changes nothing, and reports what's left:

```bash
uv run <skill>/scripts/render.py --state <workdir>/aig-model-services.json
```

If `to_fill` still lists values for a file, tell the user which ones, and wait again. Module 02 checks everything else.

## Verify

- Each part's file is in `<workdir>/<config_dir>/`, and the user said they're done with it.
- `render.py` reports an empty `to_fill` for every file.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `service_policies needs model_services` | Policies are attached to the services in `model-services.yaml`. To put guardrails on a service that already exists, add `model_services` to `parts` and list that service, with its current routing, in the file |
| `to_fill` shows `not valid YAML` | The file doesn't parse, often from a tab or a misaligned list. Show the user the line the error names |
| The user wants a fresh file | They move theirs aside (rename it), and you render again. Never delete their file |
| A service already exists in the schema | Fine: module 02 plans an update that makes it match the file. To leave it alone, don't list it |
