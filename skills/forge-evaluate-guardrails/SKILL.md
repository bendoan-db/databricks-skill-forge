---
name: forge-evaluate-guardrails
description: "Forge use case, normally started by the forge skill: measures how well Unity AI Gateway's built-in guardrails (service policies: Sensitive Data Detection, Unsafe Content, Jailbreak) block unsafe prompts and let safe ones through, on a labeled safety dataset: the user's own table or file, or by default the Aegis 2.0 and ToxicChat test sets, pinned to exact revisions. It creates a dedicated model service with the guardrails attached, sends every prompt through the gateway from a Databricks job, records which policy blocked each one, and scores recall, precision, false-positive rate, F1, and latency overall, per dataset, per harm category, and per guardrail, in Delta tables and MLflow. Use when forge hands off to it, or when the user names this skill."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill, and the Unity Gateway Service Policies preview enabled in the workspace. The default datasets download from huggingface.co.
metadata:
  version: "0.1.0"
  forge_kind: use-case
---

# Evaluate guardrails

Measures the Unity AI Gateway's built-in guardrails on a labeled safety dataset: what share of the prompts that should be blocked are blocked (recall), how often safe prompts are blocked (false-positive rate), and which guardrail catches what. The guardrails run in a model service of the evaluation's own, so nobody else's traffic is affected, and every prompt goes through the gateway the way an application's would.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
Aegis 2.0 + ToxicChat (pinned)  ─┐    Lakeflow job <name> (DAB), notebook tasks
or the user's table / file      ─┴─►  load_prompts.py ──► <prefix>_prompts   one row per prompt, BLOCK or ALLOW
guardrail_service.py ──► model service <prefix>_model, routed like <base_model_service>, with service policies:
                         sensitive_data_detection (1) ▸ unsafe_content_detection (2) ▸ jailbreak_detection (3)
                         run_eval.py: each prompt ──► /ai-gateway/mlflow/v1/chat/completions ──► blocked, and by which policy
                                      ──► <prefix>_results ──► <prefix>_metrics, MLflow run
evaluate.py ──► the report: recall, precision, false-positive rate, F1, latency; the run gate (03)
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `guardrail-eval.json` and the `<name>/` bundle live. Default: the current directory.
- `<workdir>/guardrail-eval.json` holds every resolved value. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<prefix>`.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.
- The job's tasks are Databricks notebooks the user can edit in the workspace; module 01, "Editing the job's notebooks", says how to keep such an edit.

## Inputs

Collect these together in one question during module 00. `target_schema` is required, and the user must say whether they have a `dataset` of their own; propose the rest.

| Input | Default | Notes |
|---|---|---|
| `target_schema` | — (required) | `<catalog>.<schema>` for the tables, the dataset volume, and the model service |
| `dataset` | — (asked) | The user's labeled prompts: a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with a prompt column and a label column. Without one: Aegis 2.0 and ToxicChat (module 00 states their licenses) |
| `profile` | from `databricks-core` | CLI profile for every command |
| `name` | `guardrail-eval` | Lowercase letters, digits, hyphens, ≤40 chars; the bundle and the job |
| `prefix` | `guardrail_eval` | Table prefix, and the model service `<prefix>_model` |
| `base_model_service` | `system.ai.claude-sonnet-5-5` | The model the evaluation's model service routes to; only prompts the guardrails let through reach it |
| `guardrails` | all three | Any of `sensitive_data`, `unsafe_content`, `jailbreak` |
| `evaluators` | `system.ai.gemma-3-12b` (Unsafe Content), `system.ai.gpt-oss-20b` (Jailbreak) | The model services the two LLM-judge guardrails run on |
| `sample_size` | 100 | Prompts per dataset and label in a sample run |
| `concurrency` | 8 | Gateway calls in flight |
| `targets` | none | Rates the user needs, such as `{"recall": 0.9, "fp_rate": 0.1}`; reported as met or missed |

## Guardrails

- **Confirm before billable or shared actions:** creating the model service and attaching its policies, staging the datasets, each evaluation run (every prompt bills the guardrails' evaluator models and, when allowed through, the base model), and any grant. Show what will run and wait for an explicit go-ahead.
- **Never delete** the model service, tables, or the volume without separate confirmation that names the exact resource.
- **Use a model service of the evaluation's own.** During the Beta, a service policy applies to every caller of the service it's attached to. Never attach policies to a service other people use.
- **Treat the prompts as harmful content.** Safety datasets hold violent, sexual, hateful, and self-harm text, and the results tables keep it. In reports, show counts, rates, and category names, and quote prompts only when the user asks. Grant the tables only to the users or groups the user names.
- **State the dataset licenses before using them.** ToxicChat is CC BY-NC 4.0 (non-commercial use only), Aegis 2.0 is CC BY 4.0. Get the user's go-ahead for each.
- **Never ask for or store tokens.** Use the CLI profile. `guardrail-eval.json` holds names and settings only.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `guardrail-eval.json` when its Verify passes:

- [ ] 00 Preflight: inputs and the dataset resolved, base skills present, the Beta enabled, cost and licenses approved, `guardrail-eval.json` written
- [ ] 01 Dataset: `<prefix>_prompts` loaded, with BLOCK and ALLOW counts reported
- [ ] 02 Guardrails: the model service has exactly the chosen policies, and its two checks pass
- [ ] 03 Evaluate: a sample run, then a full run, with at least 98% of prompts scored, and the report given

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `guardrail-eval.json`, tooling, cost and license go-ahead | databricks-core, databricks-unity-catalog |
| 01 | [Dataset](modules/01-dataset.md) | `<name>/` bundle, `<prefix>_prompts` | databricks-unity-catalog, databricks-dabs |
| 02 | [Guardrails](modules/02-guardrails.md) | model service `<prefix>_model` with the built-in guardrails | databricks-unity-gateway |
| 03 | [Evaluate](modules/03-evaluate.md) | `<prefix>_results`, `<prefix>_metrics`, MLflow run, the report | databricks-dabs |

The working directory ends up with the state file and the bundle:

```
<workdir>/
├── guardrail-eval.json   # resolved values; render.py and the scripts read it
└── <name>/               # Declarative Automation Bundle: load_prompts → run_eval job, rendered by render.py
```

**Resuming.** If `<workdir>/guardrail-eval.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 2). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Evaluate another dataset | 00, step 1 (dataset), then 01 |
| Change the guardrails or their evaluators | 02, step 1 |
| Rerun on the same guardrails | 03, step 2 |
| A run stopped partway | 03, step 2, with its `run_id` |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Dataset | Asked in module 00 | The user has labeled prompts from their own application: evaluate those. Otherwise Aegis 2.0 (12 harm categories) and ToxicChat (real user prompts, toxicity and jailbreaks) |
| Guardrails | All three built-ins | The user wants each guardrail's own recall. Policies stop at the first block, so with all three, a later guardrail only sees what earlier ones let through. Attach one at a time, and run once per guardrail |
| Evaluator models | `gemma-3-12b`, `gpt-oss-20b` | The policy form's preselected evaluator differs in this workspace (Advanced options), or many fail-closed blocks show an evaluator refusing. Then set `evaluators` |
| Sample or full run | Sample first, then full | Never skip the sample: it shows rate limits and fail-closed blocks before a long run |
| Concurrency | 8 | Errors or fail-closed blocks rise with rate limits: lower it |
| Hallucination guardrail | Not evaluated | It judges responses against grounding, which a prompt dataset doesn't have |

## Done when

- `<prefix>_prompts` holds the dataset, and you reported its prompts by dataset and label, and what was dropped.
- The model service `<model_service>` has exactly the chosen guardrails, and `guardrail_service.py --check` passes.
- A full run scored at least 98% of its prompts (`scored_rate`; `evaluate.py` exits 0), and you reported recall, precision, false-positive rate, and F1 overall and per dataset, each guardrail's blocks and false positives, the categories with the lowest recall, latency, and each target met or missed. Report the MLflow run.
- You told the user the model service, the tables, and how to delete them (each after a confirmation naming it). If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Guardrails on a model service:** `databricks-unity-gateway` doesn't cover service policies. The [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) describe the built-ins (`system.ai.detect_sensitive_data`, `system.ai.block_unsafe_content`, `system.ai.block_jailbreak`, and the output-only `system.ai.block_hallucination`), and attach them in the UI. `guardrail_service.py` attaches them through the model service's `config.service_policies` field instead.
- **Reading a model service's guardrails:** `databricks ai-gateway get-model-service` leaves out `config.service_policies` (CLI 1.18). Read them with `databricks api get /api/2.1/unity-catalog/model-services/<model_service>`.
