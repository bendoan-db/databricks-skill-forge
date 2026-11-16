---
name: forge-aig-evaluate-guardrails
description: "Forge use case, normally started by the forge skill: measures how well Unity AI Gateway guardrails (service policies: the built-in Sensitive Data Detection, Unsafe Content, and Jailbreak, or custom LLM-judge and SQL policies) block unsafe prompts and let safe ones through, on a labeled safety dataset: the user's own table or file, or by default the Aegis 2.0 and ToxicChat test sets, pinned to exact revisions. It has forge-aig-create-model-service create model services of the evaluation's own with the guardrails attached, one or several to compare side by side, sends every prompt through the gateway from a Databricks job, records which policy blocked each one, and scores recall, precision, false-positive rate, F1, and latency for each service, overall, per dataset, per harm category, and per guardrail, in Delta tables and MLflow. Use when forge hands off to it, or when the user names this skill."
compatibility: Runs locally, with databricks CLI (>= v1.11.0), Python 3.11+, and uv, or in Genie Code in a notebook, on serverless compute (environment 2 or later). Requires the forge-aig-create-model-service skill and the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill, and the Unity Gateway Service Policies preview. The default datasets download from huggingface.co.
metadata:
  version: "0.3.0"
  forge_kind: use-case
---

# Evaluate guardrails

Measures Unity AI Gateway guardrails on a labeled safety dataset: what share of the prompts that should be blocked are blocked (recall), how often safe prompts are blocked (false-positive rate), and which guardrail catches what. The guardrails are the gateway's built-ins or the user's own LLM-judge and SQL policies. They run on model services of the evaluation's own, which `forge-aig-create-model-service` creates, so nobody else's traffic is affected; several services, each with different guardrails, compare side by side in one run. Every prompt goes through the gateway the way an application's would.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
forge-aig-create-model-service (run first; writes aig-model-services.json and <config_dir>/*.yaml)
  └─► model services <prefix>_..., each with the policies under evaluation; no grants, no inference table (00)
Aegis 2.0 + ToxicChat (pinned)  ─┐    Lakeflow job <name> (DAB; in a notebook, job.py deploys it), notebook tasks
or the user's table / file      ─┴─►  load_prompts.py ──► <prefix>_prompts   one row per prompt, BLOCK or ALLOW (01)
render.py ◄── model-services.yaml, service-policies.yaml: the services, and the policies each must have (01)
check_services.py ──► each service: exactly its policies, all blocking, nobody else's; a pass and a block (02)
run_eval.py: each prompt, to each service ──► /ai-gateway/mlflow/v1/chat/completions ──► blocked, and by which policy
             ──► <prefix>_results ──► <prefix>_metrics, per service; an MLflow run per service (03)
evaluate.py ──► the report: the services compared; recall, precision, false-positive rate, F1, latency; the gate (03)
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`, and so is `forge-aig-create-model-service`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `guardrail-eval.json`, the model services' `aig-model-services.json` and YAML files, and the `<name>/` bundle live. Default: the current directory; in Genie Code, the folder of the notebook it's open in.
- `<workdir>/guardrail-eval.json` holds every resolved value. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<prefix>`.
- `runtime`, in that file, is where the skill runs, recorded once in module 00 and passed to `forge-aig-create-model-service`, which records the same: `local`, a coding assistant on the user's computer, using the CLI profile; or `workspace`, Genie Code in a notebook, signed in as the notebook's user. The commands in the modules are the local ones. With `workspace`, run each one's equivalent from [In a notebook](#in-a-notebook).
- `<workdir>/aig-model-services.json` and the YAML files in its `<config_dir>/` belong to `forge-aig-create-model-service`: read them, but never write them. The services under evaluation, and their policies, are what those files say. To change one, run that skill (module 00, step 7).
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.
- The job's tasks are Databricks notebooks the user can edit in the workspace; module 01, "Editing the job's notebooks", says how to keep such an edit.

### In a notebook

With `runtime: workspace`, Genie Code runs every step in Python cells of the notebook it's open in, on serverless compute, as the notebook's user. There's no CLI profile, `databricks` CLI, or `uv`. `<workdir>` is that notebook's folder, `/Workspace` plus the folder of `dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()` (serverless doesn't guarantee the working directory). `<skill>` is this skill's folder under `/Workspace/Users/<user>/.assistant/skills/` or `/Workspace/.assistant/skills/`.

Once per session, install the scripts' packages in a cell of their own:

```python
%pip install -q "databricks-sdk>=0.68" "pyyaml>=6"
dbutils.library.restartPython()
```

Then define `forge_run`, which runs a script in the notebook's own Python process, the only one signed in as the notebook's user (a subprocess isn't):

```python
import runpy
import sys


def forge_run(script, *args):
    """Run a Forge skill script here, as `uv run <script> <args>` does locally. Returns its exit status."""
    argv, sys.argv = sys.argv, [script, *map(str, args)]
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return e.code or 0
    finally:
        sys.argv = argv
    return 0
```

| Locally | In a notebook, with `w = WorkspaceClient()` (`from databricks.sdk import WorkspaceClient`) |
|---|---|
| `uv run <skill>/scripts/<file>.py <args>` | `forge_run("<skill>/scripts/<file>.py", <args>)`. A non-zero status fails the step, as locally |
| `databricks --version`, `uv --version`, `databricks auth login` | Not needed |
| `databricks aitools update` | The user runs the `install_genie_code` notebook again |
| `databricks api get <path>` | `w.api_client.do("GET", "<path>")` |
| `databricks schemas get <catalog>.<schema>` | `w.api_client.do("GET", "/api/2.1/unity-catalog/schemas/<catalog>.<schema>")` |
| `databricks grants get-effective schema <catalog>.<schema>` | `w.api_client.do("GET", "/api/2.1/unity-catalog/effective-permissions/schema/<catalog>.<schema>")` |
| `get-default-warehouse`, `databricks warehouses list` | `w.api_client.do("GET", "/api/2.0/sql/warehouses")`, and propose a serverless one |
| `databricks bundle validate`, `deploy`, `run`, `summary` | `job.py` (module 01, steps 2 and 3; module 03) |
| `databricks fs cp <file> dbfs:/Volumes/<path>` | The user uploads the file in Catalog Explorer, with the volume's **Upload to this volume** |
| `databricks workspace export <path> --format SOURCE --file <file>` | `w.api_client.do("GET", "/api/2.0/workspace/export", query={"path": "<path>", "format": "SOURCE"})["content"]`, base64-decoded and written to `<file>` |
| SQL in the modules | A SQL cell, or `spark.sql(...)` |

Everything else is the same in both: the steps, the checks, the go-aheads, and Verify.

## Inputs

Collect these together in one question during module 00. `target_schema` is required, and the user must say whether they have a `dataset` of their own and what to evaluate; propose the rest.

| Input | Default | Notes |
|---|---|---|
| `target_schema` | — (required) | `<catalog>.<schema>` for the tables and the dataset volume, and for the model services when `<workdir>` has none yet |
| `dataset` | — (asked) | The user's labeled prompts: a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with a prompt column and a label column. Without one: Aegis 2.0 and ToxicChat (module 00 states their licenses) |
| `services` | the built-ins together | What to evaluate: the evaluation's model services, each named `<prefix>_...`, and the policies each carries (module 00, step 1). Several compare side by side |
| `profile` | from `databricks-core` | CLI profile for every command. Locally only: in a notebook, the notebook's user |
| `name` | `guardrail-eval` | Lowercase letters, digits, hyphens, ≤40 chars; the bundle and the job |
| `prefix` | `guardrail_eval` | Table prefix, and the start of every evaluation service's name, such as `<prefix>_model` |
| `base_model` | `system.ai.databricks-claude-sonnet-5-5` | The model the evaluation's services route to: a Unity Catalog model, not a model service. Only prompts the guardrails let through reach it |
| `evaluators` | `system.ai.gemma-3-12b` (Unsafe Content), `system.ai.gpt-oss-20b` (Jailbreak) | The model services the built-in LLM-judge guardrails run on |
| `sample_size` | 100 | Prompts per dataset and label in a sample run |
| `concurrency` | 8 | Gateway calls in flight, across all the services |
| `targets` | none | Rates the user needs, such as `{"recall": 0.9, "fp_rate": 0.1}`; reported as met or missed for each service |

## Guardrails

- **Confirm before billable or shared actions:** staging the datasets, and each evaluation run (every prompt goes to every service, and bills its guardrails' evaluator models and, when allowed through, the base model). `forge-aig-create-model-service` asks for its own go-ahead before it creates the services and attaches their policies. Show what will run and wait for an explicit go-ahead.
- **Never delete** the model services, tables, or the volume without separate confirmation that names the exact resource. The services are deleted through `forge-aig-create-model-service` (its module 03, Removing access or a service).
- **Evaluate only services of the evaluation's own.** During the Beta, a service policy applies to every caller of the service it's attached to, and a service's inference table keeps every prompt. Never send the prompts to a service other people use, and never grant the evaluation's services to anyone. To measure a production service's guardrails, evaluate a copy (module 00, step 1). `check_services.py` fails a service that someone else holds a privilege on.
- **Treat the prompts as harmful content.** Safety datasets hold violent, sexual, hateful, and self-harm text, and the results tables keep it. In reports, show counts, rates, and category names, and quote prompts only when the user asks. Grant the tables only to the users or groups the user names.
- **State the dataset licenses before using them.** ToxicChat is CC BY-NC 4.0 (non-commercial use only), Aegis 2.0 is CC BY 4.0. Get the user's go-ahead for each.
- **Never ask for or store tokens.** Use the CLI profile locally, and the notebook's own sign-in in a notebook. `guardrail-eval.json` holds names and settings only.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `guardrail-eval.json` when its Verify passes:

- [ ] 00 Preflight: inputs, the dataset, and what to evaluate resolved, base skills present, the Beta enabled, cost and licenses approved, `guardrail-eval.json` written, and the model services created by `forge-aig-create-model-service`
- [ ] 01 Dataset: the bundle rendered with the services and their policies, and `<prefix>_prompts` loaded, with BLOCK and ALLOW counts reported
- [ ] 02 Guardrails: every service has exactly its policies, all blocking, nobody else uses it, and its probes pass
- [ ] 03 Evaluate: a sample run, then a full run, with at least 98% of prompts scored for every service, and the report given

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `guardrail-eval.json`, cost and license go-ahead, the model services (through `forge-aig-create-model-service`) | databricks-core, databricks-unity-catalog |
| 01 | [Dataset](modules/01-dataset.md) | `<name>/` bundle, `<prefix>_prompts` | databricks-unity-catalog, databricks-dabs |
| 02 | [Guardrails](modules/02-guardrails.md) | each model service checked, and probed with a pass and a block | databricks-unity-gateway |
| 03 | [Evaluate](modules/03-evaluate.md) | `<prefix>_results`, `<prefix>_metrics`, an MLflow run per service, the report | databricks-dabs |

The working directory ends up with both skills' state files, the model services' YAML files, and the bundle:

```
<workdir>/
├── README.md                # the project explained for whoever opens the folder; write_readme.py writes it
├── guardrail-eval.json      # resolved values; render.py and the scripts read it
├── aig-model-services.json  # the model services' settings (forge-aig-create-model-service); read-only here
├── <config_dir>/            # its YAML files: the services under evaluation and their policies; read-only here
└── <name>/                  # Declarative Automation Bundle: load_prompts → run_eval job, rendered by render.py; the user can edit it
```

**The README.** Each time a module's Verify passes, module 00's included, update `<workdir>/README.md`:

```bash
uv run <skill>/scripts/write_readme.py --state <workdir>/guardrail-eval.json
```

It explains the project to whoever opens the folder, from this skill and every Forge state file in `<workdir>`: what it builds, its settings, and each module's purpose, outputs, and status. Its Notes section is the user's, and is kept. If the script reports `kept_edited`, someone edited the README outside Notes: tell the user, and pass `--force` only if they agree. A README.md that `<workdir>` already had is left alone, and the project's goes to `FORGE-README.md`.

**Resuming.** If `<workdir>/guardrail-eval.json` exists, read it and `aig-model-services.json`, confirm their values with the user, and rerun module 00's base-skill check (step 2). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Evaluate another dataset | 00, step 1 (dataset), then 01 |
| Change the guardrails, their evaluators, or the services compared | 00, step 1 (what to evaluate) and step 7, then 01, step 1, and 02 |
| Rerun on the same guardrails | 03, step 2 |
| A run stopped partway | 03, step 2, with its `run_id` |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| What to evaluate | The built-ins together, on one service | The user wants each guardrail's own recall: policies stop at the first block, so on one service a later guardrail only sees what earlier ones let through. Put each on its own service, compared in one run. A production service's guardrails: a copy of it. A custom rule: alone, or next to the built-in it would replace |
| Dataset | Asked in module 00 | The user has labeled prompts from their own application: evaluate those. Otherwise Aegis 2.0 (12 harm categories) and ToxicChat (real user prompts, toxicity and jailbreaks) |
| Evaluator models | `gemma-3-12b`, `gpt-oss-20b` | The policy form's preselected evaluator differs in this workspace (Advanced options), or many fail-closed blocks show an evaluator refusing. Then set `evaluators`, or compare two on two services |
| Sample or full run | Sample first, then full | Never skip the sample: it shows rate limits and fail-closed blocks before a long run |
| Concurrency | 8 | Errors or fail-closed blocks rise with rate limits: lower it. It's the calls in flight across every service, so several services take longer, not harder |
| Policies that only log or redact | Not evaluated | A policy in `mode: log`, or a Sensitive Data Detection policy that redacts, leaves no verdict in the reply. Leave it out of the copy, and tell the user it isn't measured |
| Hallucination guardrail | Not evaluated | It judges responses against grounding, which a prompt dataset doesn't have |

## Done when

- `<prefix>_prompts` holds the dataset, and you reported its prompts by dataset and label, and what was dropped.
- Every service in `<model_services>` has exactly the policies its entry in `model-services.yaml` lists, all blocking, nobody else holds a privilege on it, and `check_services.py --probe` passes.
- A full run scored at least 98% of its prompts for every service (`scored_rate`; `evaluate.py` exits 0), and you reported, for each service, recall, precision, false-positive rate, and F1 overall and per dataset, each guardrail's blocks and false positives, the categories with the lowest recall, latency, and each target met or missed, and, with several services, how they compare. Report the MLflow runs.
- `<workdir>/README.md` describes the finished project: `write_readme.py` ran after the last module.
- You told the user the model services, the tables, the job and how to run it again, and how to delete them (each after a confirmation naming it). If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Guardrails on a model service:** `databricks-unity-gateway` doesn't cover service policies. The [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) describe the built-ins (`system.ai.detect_sensitive_data`, `system.ai.block_unsafe_content`, `system.ai.block_jailbreak`, and the output-only `system.ai.block_hallucination`) and custom policies, and attach them in the UI. `forge-aig-create-model-service` attaches them through the model service's `config.service_policies` field instead, and `check_services.py` reads them back, with their options, to check each one blocks.
- **Reading a model service's guardrails:** `databricks ai-gateway get-model-service` leaves out `config.service_policies` (CLI 1.18). Read them with `databricks api get /api/2.1/unity-catalog/model-services/<service>`.
