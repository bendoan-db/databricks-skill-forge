# Guardrail evaluation (`forge-aig-evaluate-guardrails`)

Measures how well Unity AI Gateway guardrails (service policies) block unsafe prompts on a labeled safety dataset. Users can provide a safety dataset. If the user does not provide a dataset, Forge will default to the Aegis 2.0 and ToxicChat test sets. The guardrails can be the built-in Sensitive Data Detection, Unsafe Content, and Jailbreak policies, or the user's own LLM-judge and SQL rules.
- **Model services of its own:** the guardrails run on model services created just for the evaluation. This skill invokes the `forge-aig-create-model-service` forge skill, to create model services that back any llm-driven policy as well as the evaluation model service itself, so that nobody else's traffic is affected.
- **Side by side:** One evaluation can concurrently test several guardrail configurations on the same prompts, so users can compare guardrail configurations side by side on the same input.
- **Real traffic:** A Databricks job sends every prompt through the gateway, the way an application would, and records which guardrail blocked each one.
- **Metrics:** This skill tracks recall, precision, false-positive rate, F1, and latency for each service, overall, per dataset, per harm category, and per guardrail.

## Purpose

- **This Forge Skill is used to:**
  - measure how well the built-in guardrails block unsafe, toxic, or jailbreak prompts, together or each on its own;
  - measure guardrail recall or false positives, of the built-ins or of the user's own LLM-judge and SQL rules;
  - compare guardrail setups or evaluator models side by side;
  - red-team a copy of a model service's policies with the user's own prompts.
- **Not for** setting up model services and guardrails for people to use: that's `forge-aig-create-model-service`.

## How to Run

The user starts it with `/forge` (`@forge` in Genie Code) and says what they want, or names the skill. The coding assistant works through it step by step, and asks before anything that costs money. It runs in either of two places:
- **Locally,** in a coding assistant on the user's computer, with the Databricks CLI. The working directory is the folder it's started in.
- **In Genie Code, in a notebook,** with no CLI to install: it runs each step in the notebook's cells, as the notebook's user, and the notebook's folder is the working directory. The user opens a notebook in the folder the project should live in, and starts it there.

> /forge How well do the AI Gateway guardrails block jailbreaks and toxic prompts? Measure them on a safety dataset.
>
> /forge Compare our custom jailbreak judge with the built-in Jailbreak guardrail on our red-team prompts in main.safety.redteam

Its progress is saved in `guardrail-eval.json`. The user can run `/forge` in the same folder later to evaluate another dataset, change or add guardrails, compare evaluators, or run again. The job itself can also run again without the assistant, from its page in the workspace.

## Inputs

**Prerequisites:**
- **Workspace:** a workspace with Unity AI Gateway and serverless compute. A workspace admin must turn on the **Unity Gateway Service Policies** preview.
- **Tools:**
  - locally: the Databricks CLI (v1.11.0 or later), signed in to the workspace, and `uv`;
  - in Genie Code: a notebook on serverless compute (environment 2 or later), and nothing else;
  - the `forge-aig-create-model-service` skill and the Databricks Agent Skills (v0.2.27 or later), installed next to this skill. The Forge installer sets these up; for Databricks notebook + Genie code invocation, run the `install_genie_code` notebook.
- **Privileges:**
  - `USE SCHEMA`, `CREATE TABLE`, and `CREATE VOLUME` on the target schema;
  - access to the user's dataset;
  - permission to create jobs;
  - If the user wants to create their own service, then they also need `CREATE SERVICE` on their schema, `EXECUTE` on the base model and each evaluator, and `CREATE FUNCTION` for SQL rules. The skills check these first.
- **Default datasets:** they download from huggingface.co. Their licenses are stated before use: Aegis 2.0 is CC BY 4.0, and ToxicChat is CC BY-NC 4.0 (non-commercial only).

**What it asks the user.** It needs the target schema, whether the user has a dataset of their own, and what to evaluate. It proposes the rest.

| Input | Required | Default | What it is |
|---|---|---|---|
| `target_schema` | Yes | — | `<catalog>.<schema>` for the tables and the dataset volume, and for the model services when the working directory has none yet |
| `dataset` | Yes | the default datasets | The user's labeled prompts (a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with a prompt column and a label column), or none, to use Aegis 2.0 and ToxicChat |
| `services` | No | the built-ins together, on one service | What to evaluate: the built-ins together, each built-in on its own, two evaluators, a copy of one of the user's services, or the user's own guardrails. Each setup is one or more services named `<table_prefix>_...` |
| `profile` | No | the user's CLI profile | The CLI profile every command uses, when it runs locally. In Genie Code, the notebook's user |
| `job_name` | No | `guardrail-eval` | Names the job, and the folder its bundle is in |
| `table_prefix` | No | `guardrail_eval` | Starts the name of every table, the dataset volume, and every evaluation service, such as `<table_prefix>_prompts` and `<table_prefix>_model` |
| `base_model` | No | `system.ai.databricks-claude-sonnet-5-5` | The model the evaluation's services route to. Only prompts the guardrails let through reach it |
| `default_evaluator_model` | No | `system.ai.gpt-oss-20b` | The model that judges the LLM-based guardrails, Jailbreak and the user's own LLM judges, unless one names its own. The built-in Unsafe Content guardrail uses `system.ai.gemma-3-12b` unless the user names another |
| `sample_size` | No | 100 | Prompts per dataset and label in the first, sample run |
| `concurrency` | No | 8 | Gateway calls in flight, across all the services |

## Outputs

**In the workspace:**
- **The evaluation's model services,** created by `forge-aig-create-model-service`: each `<table_prefix>_...`, routed to the base model, with exactly the guardrails under evaluation, no inference table, no rate limits, and no access for anyone else.
- **Tables,** in the target schema:
  - `<table_prefix>_prompts`: one row per prompt, labeled block or allow;
  - `<table_prefix>_results`: every prompt's verdict on each service, and the guardrail that blocked it;
  - `<table_prefix>_metrics`: the scores, for each service.
- **Dataset files:** the volume `<table_prefix>_datasets`.
- **The job:** `<job_name>`, a Lakeflow job in a Declarative Automation Bundle that loads the prompts and runs the evaluation. Its tasks are notebooks the user can open and edit in the workspace. Locally, the Databricks CLI deploys the bundle; in Genie Code, the skill's `job.py` creates the job from it.
- **MLflow runs:** one per service in each evaluation run, tagged with the run's id, so they compare side by side.
- **A report:**
  - with several services, a table comparing them;
  - for each service, recall, precision, false-positive rate, and F1, overall and per dataset;
  - each guardrail's blocks and false positives;
  - the categories with the lowest recall;
  - latency.

**In the working directory:**

```
<workdir>/
├── README.md                # what was built, its settings, and each step's status
├── guardrail-eval.json      # the evaluation's settings
├── aig-model-services.json  # the model services' settings (forge-aig-create-model-service)
├── ai-gateway/              # the YAML files that describe the services and their guardrails
└── <job_name>/                  # the job's bundle, which the user can edit
```

**The bundle is the user's to keep.** It's in the working directory either way. To change the job, the user edits it (`databricks.yml`, or a notebook in `src/`), and asks the assistant to deploy and run again; in Genie Code, a run stops until the edit is deployed. Without the assistant, they can run the job from its page, with **Run now with different settings** for a full run (`mode`) or to resume one (`run_id`), or deploy the bundle with the Databricks CLI.

**When it's done.** A full run scores at least 98% of its prompts for every service (scored rate ≥ 0.98), so the rates are reliable. How good the guardrails must be is the user's call: the report gives the rates, and only an incomplete run fails.

## How It Works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Resolves the target schema, the dataset, and what to evaluate, states the default datasets' licenses, checks the tools, the preview, and the user's privileges, and gets the user's go-ahead on cost. Then `forge-aig-create-model-service` creates the evaluation's services, after its own plan and go-ahead |
| 01 | Dataset | Renders the job with the services and their guardrails, deploys it (in Genie Code, creates the job from the bundle), and loads the prompts into one table, each labeled block or allow |
| 02 | Guardrails | Checks each service has exactly its guardrails, all blocking, and nobody else uses it, then that a plain prompt passes and one with an SSN is blocked |
| 03 | Evaluate | Runs a sample, then the full dataset, on every service, and reports the scores side by side |

Each step ends with a check that must pass before the next one starts.

### Preflight Checks and Guardrails

It asks before:
- staging the datasets;
- each evaluation run: every prompt goes to every service, and bills the guardrails' evaluator models and, when a prompt is let through, the base model.

`forge-aig-create-model-service` asks before it creates the services and attaches their guardrails. The prompts are harmful by design. Reports show counts, rates, and category names, and quote prompts only when the user asks. It never sends the prompts to a service other people use, never grants the evaluation's services to anyone, and never deletes a service, the tables, or the volume without a separate confirmation that names it.

A guardrail that only logs, or redacts instead of blocking, leaves no verdict in the reply, so it isn't measured: the user is told which ones.
