# 00 · Preflight

Resolve the inputs, the dataset, and what to evaluate; confirm the tooling, base skills, the Service Policies preview, and permissions; get a go-ahead on cost and the dataset licenses before anything billable runs; and have `forge-aig-create-model-service` create the model services under evaluation. The resolved values go in `<workdir>/guardrail-eval.json`.

## Inputs → outputs

- **Inputs:** the user's request, their dataset if they have one, and the `aig-model-services.json` already in `<workdir>`, if there is one.
- **Outputs:** `<workdir>/guardrail-eval.json` with the resolved inputs, the dataset, and the services to evaluate; the user's go-ahead on cost and licenses; and the model services, created by `forge-aig-create-model-service` with its `aig-model-services.json` and YAML files.

## 1. Collect inputs

Ask once, listing every input from the SKILL.md Inputs table with its proposed default. If `forge` started this skill, the user may already have given some of them: confirm those instead of asking again. Three need an answer:

- **`target_schema`:** the `<catalog>.<schema>` for the tables and the dataset volume. The user names an existing one, or one they can create.
- **`dataset`:** ask whether they have a labeled safety dataset of their own, and wait for the answer:

  > Do you have a labeled dataset of prompts to evaluate the guardrails on? It can be a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with one column for the prompt and one that says whether it should be blocked. If you don't, I'll use two public safety datasets: Aegis 2.0 (NVIDIA) and ToxicChat (LMSYS).

  - **They have one:** ask for the table name or the volume path. Look at its columns with them (`DESCRIBE TABLE`, or the file's first rows), and agree on the prompt column, the label column, the label values that mean "should be blocked", and, optionally, a category column. Record it, for example `{"source": "table", "table": "<catalog>.<schema>.<table>", "prompt_column": "text", "label_column": "label", "block_values": ["unsafe"], "category_column": "category", "name": "support_prompts"}`, or `"source": "file"` with `path` and `format` (`csv`, `json`, `jsonl`, or `parquet`).
  - **They don't:** record `{"source": "default"}`. The defaults are the test splits of Aegis 2.0 and ToxicChat, pinned to exact revisions (step 5 states their licenses). Without the user's go-ahead on ToxicChat's license, use `{"source": "default", "defaults": ["aegis2"]}`.
- **What to evaluate (`services`):** ask which guardrails to measure, offering these setups. Each is one or more model services of the evaluation's own, named `<table_prefix>_...`, which `forge-aig-create-model-service` creates in step 7. Every service gets the same prompts, so several compare side by side in one run, and each one adds a run's worth of calls.

  | Setup | The services | When |
  |---|---|---|
  | The built-ins together (default) | `<table_prefix>_model`, with `eval_sensitive_data`, `eval_unsafe_content`, and `eval_jailbreak` | How well the gateway's built-in guardrails do as a set |
  | Each built-in on its own | `<table_prefix>_sensitive_data`, `<table_prefix>_unsafe_content`, `<table_prefix>_jailbreak`, one policy each | Each guardrail's own recall: on one service, a later policy sees only what earlier ones let through |
  | Two evaluators | `<table_prefix>_<evaluator>` for each, with the same policy on a different `evaluator` | Which evaluator model judges better |
  | A copy of one of the user's services | `<table_prefix>_<service>`, with that service's destinations and policies | Their production guardrails, without sending harmful prompts to the service people use |
  | The user's own guardrails | `<table_prefix>_<rule>`, with custom `llm_judge` or `sql` policies, alone or next to the built-ins | A custom rule, or a custom rule against the built-in it would replace |

  Never evaluate a service other people use, even when the user asks: during the Beta, a service policy applies to every caller, and the prompts would land in its inference table. Offer a copy instead. A policy that only logs (`mode: log`) or redacts (`action: redact`) leaves no verdict in the reply, so it can't be measured: leave it out of a copy, and tell the user it isn't measured.

Derive the following and show them back to the user:

```
catalog, schema     = <target_schema>                  # the tables and the dataset volume
prefix              = <table_prefix>                         # tables <table_prefix>_prompts, _results, _metrics; volume <table_prefix>_datasets
name                = <≤40 chars, [a-z0-9-]>           # bundle and job
model_services      = <services' catalog>.<services' schema>.<table_prefix>_model, ...   # one per service in the setup
experiment          = /Users/<user>/<job_name>             # MLflow
```

The services' catalog and schema are `<target_schema>`'s, unless `<workdir>` already has an `aig-model-services.json`: its `model-services.yaml` has one catalog and schema for every service it lists, so the evaluation's services go there too.

## 2. Tooling and base skills

First, where this runs, which becomes `runtime` in step 6: `workspace` when you're Genie Code in a notebook, and `local` otherwise. In a notebook, run the two setup cells from SKILL.md, In a notebook, then the base-skill check with `forge_run`, and skip the CLI and `uv` lines below.

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 3).

```bash
databricks --version                     # >= v1.11.0
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
```

`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`, including the `forge-aig-create-model-service` skill under `requires`. If it exits non-zero, stop: the modules cite reference files that the installed skills don't have. Ask the user to install or update databricks-agent-skills (v0.2.27 or later) in the same skills directory, for example with `databricks aitools update`, and to install `forge-aig-create-model-service` from databricks-skill-forge if it's missing. In a notebook, the `install_genie_code` notebook installs or updates both.

## 3. The workspace

```bash
databricks schemas get <catalog>.<schema> --profile <profile>
WAREHOUSE_ID=$(databricks experimental aitools tools get-default-warehouse --profile <profile>)   # or pick from `databricks warehouses list`
```

- Record the warehouse as `warehouse_id`. If the schema doesn't exist and the user can create it, create it after the go-ahead in step 5.
- **The Service Policies preview** ("Unity Gateway Service Policies") must be on: a workspace admin enables it on the workspace's Previews page. Ask the user to confirm it is. Without it, the services can't get their guardrails.
- With a dataset of the user's, count its rows and labels: `SELECT <label_column>, count(*) FROM <table> GROUP BY 1`. Both kinds of prompt should be there; with only one, say which rates can't be measured (recall needs prompts to block, the false-positive rate needs prompts to allow).

The model services skill checks the base model and each evaluator when it plans the services, in step 7.

## 4. Permissions

The user running the skill needs:

| On | Privilege |
|---|---|
| target schema | `USE SCHEMA`, `CREATE TABLE`, and `CREATE VOLUME`; `CREATE SCHEMA` on the catalog if the schema is new |
| the user's dataset | `SELECT` on the table, or `READ VOLUME` on its volume |
| workspace | create Lakeflow jobs; serverless compute; egress to huggingface.co for the default datasets (module 01 has a fallback) |

Check with `databricks grants get-effective schema <catalog>.<schema> --profile <profile>`. Report any missing privilege. Don't try to grant it. If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md`.

What the model services need, such as `CREATE SERVICE`, `EXECUTE` on the base model and each evaluator, and `CREATE FUNCTION` for SQL policies, `forge-aig-create-model-service` checks with its `check_access.py`, in step 7. The user owns the services it creates, which gives them `MANAGE` on them.

## 5. Cost and license go-ahead

Summarize what each step creates or bills, then wait for an explicit yes:

- **Dataset:** a volume, `<table_prefix>_datasets`, and a table, `<table_prefix>_prompts`. The default datasets are about 1,900 prompts (Aegis 2.0) and 5,000 (ToxicChat).
- **Model services:** the services in the setup, which `forge-aig-create-model-service` creates after its own plan and go-ahead (step 7). They bill nothing while idle.
- **Each evaluation run:** a serverless job. Every prompt goes to every service, and calls its guardrails' evaluator models (Unsafe Content, Jailbreak, an LLM judge) and, when the guardrails let it through, the base model, for at most `max_tokens` (64) tokens. A sample run is `sample_size` prompts per dataset and label (400 with both defaults) for each service; a full run is every prompt for each service, about 6,900 with both defaults, roughly 30 to 60 minutes per service at 8 in flight.
- **Licenses, for the default datasets:** Aegis 2.0 is CC BY 4.0 (attribution). ToxicChat is CC BY-NC 4.0: non-commercial use only. Ask the user to confirm their use fits each license, and leave out a dataset they can't use.

## 6. Write `guardrail-eval.json`

After the go-ahead, write the resolved values to `<workdir>/guardrail-eval.json`:

```json
{
  "runtime": "local",
  "profile": "<profile>",
  "catalog": "<catalog>",
  "schema": "<schema>",
  "table_prefix": "guardrail_eval",
  "job_name": "guardrail-eval",
  "warehouse_id": "<warehouse_id>",
  "dataset": {"source": "default"},
  "services_state": "aig-model-services.json",
  "model_services": ["<catalog>.<schema>.guardrail_eval_model"],
  "concurrency": 8,
  "max_tokens": 64,
  "sample_size": 100,
  "settle_minutes": 5,
  "experiment": "/Users/<user>/guardrail-eval",
  "completed": []
}
```

In a notebook, `runtime` is `workspace` and there's no `profile`. `base_model` and `default_evaluator_model` aren't kept here: they go into the model services' YAML files in step 7, which record what's evaluated. Later modules add nothing; they read this file.

## 7. The model services

`forge-aig-create-model-service` creates the services from its YAML files, after its own privilege check, plan, and go-ahead.

- **They exist already** (resuming, or another evaluation in this folder): every name in `model_services` is in its `model-services.yaml`, and the `completed` in its `aig-model-services.json` includes `03-apply`. Go on to Verify.
- **Otherwise,** run `forge-aig-create-model-service` (`<skill>/../forge-aig-create-model-service/SKILL.md`) in this `<workdir>`, through its module 03, and tell it the services are for a guardrail evaluation. Pass it:
  - `runtime`: the same as here, so both skills run in the same place;
  - `target_schema`: the services' catalog and schema, from step 1;
  - `parts`: `model_services` and `service_policies`, added to any it has already. Never add `grants` for these services: nobody else may use them;
  - the entries to write into its YAML files (its module 01, step 2), for the setup the user chose.

  For the default setup, with the default `table_prefix`, `base_model`, and `default_evaluator_model` (use the user's values instead, when they gave any):

  ```yaml
  # model-services.yaml, under model_services:
  - name: guardrail_eval_model
    description: Guardrail evaluation (forge-aig-evaluate-guardrails). Only the evaluation calls it; don't grant it.
    destinations:
      - provider: databricks
        destination: system.ai.databricks-claude-sonnet-5-5
    enable_inference_table: false
    enable_rate_limits: false
    service_policies: [eval_sensitive_data, eval_unsafe_content, eval_jailbreak]
  ```

  ```yaml
  # service-policies.yaml, under service_policies:
  - name: eval_sensitive_data
    type: detect_sensitive_data
    categories: [email_address, ip_address, mac_address, vin, credit_card, iban_code, phone_number, us_ssn, us_itin,
                 us_passport, us_bank_number, uk_nhs, uk_nino, in_pan, in_aadhaar]
  - name: eval_unsafe_content
    type: block_unsafe_content
    phases: [input, output]
    evaluator: system.ai.gemma-3-12b       # Unsafe Content's own, unless the user names another
  - name: eval_jailbreak
    type: block_jailbreak
    evaluator: system.ai.gpt-oss-20b       # default_evaluator_model
  ```

  For the other setups:
  - **Each built-in on its own:** three services like the one above, `<table_prefix>_sensitive_data`, `<table_prefix>_unsafe_content`, and `<table_prefix>_jailbreak`, each listing one of the three policies.
  - **Two evaluators:** two definitions of the same type, such as `eval_jailbreak_gpt_oss` and `eval_jailbreak_gemma`, each with its `evaluator`, and a service for each.
  - **A copy of one of the user's services:** read the service, from its entry in `model-services.yaml` or with `databricks api get /api/2.1/unity-catalog/model-services/<service> --profile <profile>`. The copy, `<table_prefix>_<service>`, has its destinations and fallbacks, and lists its policies, which must be defined in `service-policies.yaml`: define any attached in the UI there, with the same settings. Leave out a policy that only logs or redacts, and tell the user it isn't measured.
  - **The user's own guardrails:** their `llm_judge` or `sql` definitions, already in `service-policies.yaml` or described to you, on a service of their own, or next to the built-ins. An `llm_judge` you write names `evaluator: <default_evaluator_model>`, unless the user names another.

  Every entry keeps `enable_inference_table: false` and `enable_rate_limits: false`: the results table is the evaluation's record, and a rate limit slows the run. Policies keep the default `mode: block`. Never change a policy definition that another service lists: add the evaluation's own, named `eval_...`.

  Then come back here. The services are listed in `model-services.yaml`, which `render.py` reads in module 01.

## Verify

- Every input is resolved and echoed back. `job_name` is ≤40 characters and matches `^[a-z0-9-]+$`, and `table_prefix` matches `^[a-z0-9_]+$`.
- The user answered the dataset question, and `dataset` is recorded; with defaults, the user approved each license.
- `runtime` is recorded, with `profile` when it's `local`. `check_base_skills.py` exits 0, and the user confirmed the Service Policies preview is on.
- The user approved the cost summary, and `guardrail-eval.json` exists.
- Every name in `model_services` starts with `<table_prefix>_` and is in `forge-aig-create-model-service`'s `model-services.yaml`, and its `aig-model-services.json` lists `03-apply` under `completed`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check_base_skills.py` reports `forge-aig-create-model-service` missing or too old | Install the Forge skills again: the installer puts every Forge skill next to the base skills |
| The user wants to evaluate a service other people use | Don't. Offer a copy of it, `<table_prefix>_<service>`, with the same destinations and policies |
| The model services skill's plan rejects an entry | Fix the entry with the user in its module 01, as its module 02 Troubleshooting says, then plan again |
| `aig-model-services.json` has another catalog and schema | Its `model-services.yaml` has one location for all its services: the evaluation's services go there, and `model_services` names them there |
