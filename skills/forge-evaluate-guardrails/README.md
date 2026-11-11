# Guardrail evaluation (`forge-evaluate-guardrails`)

Measures how well Unity AI Gateway's built-in guardrails (service policies) block unsafe prompts and let safe ones through. It evaluates Sensitive Data Detection, Unsafe Content, and Jailbreak on a labeled safety dataset: yours, or by default the Aegis 2.0 and ToxicChat test sets, pinned to exact revisions.
- **Its own model service:** the guardrails are attached to a model service of the evaluation's own, so nobody else's traffic is affected.
- **Real traffic:** a Databricks job sends every prompt through the gateway, the way an application would, and records which guardrail blocked each one.
- **The scores:** recall, precision, false-positive rate, F1, and latency, overall, per dataset, per harm category, and per guardrail.

It's a Forge use case. Start it with `/forge` (`@forge` in Genie Code) and say what you want, or name the skill. Your coding assistant works through it step by step, and asks before anything that costs money.

## What it's for

- **Choose it for:**
  - how well the built-in guardrails block unsafe, toxic, or jailbreak prompts;
  - guardrail recall or false positives;
  - red-teaming a model service's policies with your own prompts.
- **Not for** setting up model services and guardrails for your users: use `forge-aig-create-model-service`. **Not for** evaluating a document agent's answers: use `forge-doc-qa-agent`.

## What you provide

**Before you start:**
- **Workspace:** a workspace with Unity AI Gateway and serverless compute. A workspace admin must turn on the **Unity Gateway Service Policies** preview.
- **Tools:**
  - the Databricks CLI (v1.11.0 or later), signed in to the workspace;
  - `uv`;
  - the Databricks Agent Skills (v0.2.27 or later), installed next to the Forge skills.
- **Privileges:**
  - `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME`, and `CREATE SERVICE` on the target schema;
  - `EXECUTE` on the base model service and on each evaluator;
  - access to your dataset;
  - permission to create jobs.

  The skill checks these first.
- **Default datasets:** they download from huggingface.co. Their licenses are stated before use: Aegis 2.0 is CC BY 4.0, and ToxicChat is CC BY-NC 4.0 (non-commercial only).

**What it asks you.** It needs the target schema and whether you have a dataset of your own. It proposes the rest.

| Input | Required | Default | What it is |
|---|---|---|---|
| `target_schema` | Yes | — | `<catalog>.<schema>` for the tables, the dataset volume, and the evaluation's model service |
| `dataset` | Yes | the default datasets | Your labeled prompts (a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with a prompt column and a label column), or none, to use Aegis 2.0 and ToxicChat |
| `profile` | No | your CLI profile | The CLI profile every command uses |
| `name` | No | `guardrail-eval` | Names the bundle and the job |
| `prefix` | No | `guardrail_eval` | Prefixes the tables, and names the model service `<prefix>_model` |
| `base_model_service` | No | `system.ai.claude-sonnet-5-5` | The model the evaluation's service routes to. Only prompts the guardrails let through reach it |
| `guardrails` | No | all three | Any of `sensitive_data`, `unsafe_content`, and `jailbreak` |
| `evaluators` | No | `system.ai.gemma-3-12b` (Unsafe Content), `system.ai.gpt-oss-20b` (Jailbreak) | The model services the two LLM-judged guardrails run on |
| `sample_size` | No | 100 | Prompts per dataset and label in the first, sample run |
| `concurrency` | No | 8 | Gateway calls in flight |
| `targets` | No | none | Rates you need, such as recall ≥ 0.9; reported as met or missed |

## What you get

**In your workspace**, in the target schema:
- **The evaluation's model service:** `<prefix>_model`, routed like the base model service, with exactly the chosen guardrails attached.
- **Tables:**
  - `<prefix>_prompts`: one row per prompt, labeled block or allow;
  - `<prefix>_results`: every prompt's verdict, and the guardrail that blocked it;
  - `<prefix>_metrics`: the scores.
- **Dataset files:** the volume `<prefix>_datasets`.
- **The job:** `<name>`, a Lakeflow job in a Declarative Automation Bundle that loads the prompts and runs the evaluation. Its tasks are notebooks you can open and edit in the workspace.
- **The MLflow run:** the metrics, recorded with each run.
- **A report:**
  - recall, precision, false-positive rate, and F1, overall and per dataset;
  - each guardrail's blocks and false positives;
  - the categories with the lowest recall;
  - latency;
  - each target, met or missed.

**In your working directory:**

```
<workdir>/
├── README.md             # what was built, its settings, and each step's status
├── guardrail-eval.json   # the settings
└── <name>/               # the job's bundle
```

**When it's done.** A full run scores at least 98% of its prompts (scored rate ≥ 0.98), so the rates are reliable. How good the guardrails must be is your call: your targets are reported as met or missed, and never fail the run.

## How it works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Resolves the target schema and the dataset, states the default datasets' licenses, checks the tools, the preview, and your privileges, and gets your go-ahead on cost |
| 01 | Dataset | Deploys the job, and loads the prompts into one table, each labeled block or allow |
| 02 | Guardrails | Creates the evaluation's model service with the chosen guardrails, and checks that a plain prompt passes and one with an SSN is blocked |
| 03 | Evaluate | Runs a sample, then the full dataset, and reports the scores |

Each step ends with a check that must pass before the next one starts.

## Before it acts

It asks before:
- creating the model service and attaching its guardrails;
- staging the datasets;
- each evaluation run: every prompt bills the guardrails' evaluator models and, when a prompt is let through, the base model.

The prompts are harmful by design. Reports show counts, rates, and category names, and quote prompts only when you ask. It never attaches guardrails to a service other people use, and never deletes the model service, the tables, or the volume without a separate confirmation that names it.

## Running it

> /forge How well do the AI Gateway guardrails block jailbreaks and toxic prompts? Measure them on a safety dataset.

Its progress is saved in `guardrail-eval.json`. Run `/forge` in the same folder later to evaluate another dataset, change the guardrails or their evaluators, or run again.
