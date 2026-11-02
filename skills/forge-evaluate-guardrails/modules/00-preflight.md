# 00 · Preflight

Resolve the inputs and the dataset, confirm the tooling, base skills, the Service Policies preview, and permissions, and get a go-ahead on cost and the dataset licenses before anything billable runs. The resolved values go in `<workdir>/guardrail-eval.json`.

## Inputs → outputs

- **Inputs:** the user's request, and their dataset if they have one.
- **Outputs:** `<workdir>/guardrail-eval.json` with the resolved inputs and the dataset, and the user's go-ahead on cost and licenses.

## 1. Collect inputs

Ask once, listing every input from the SKILL.md Inputs table with its proposed default. If `forge` started this skill, the user may already have given some of them: confirm those instead of asking again. Two need an answer:

- **`target_schema`:** the `<catalog>.<schema>` for the tables, the dataset volume, and the model service. The user names an existing one, or one they can create.
- **`dataset`:** ask whether they have a labeled safety dataset of their own, and wait for the answer:

  > Do you have a labeled dataset of prompts to evaluate the guardrails on? It can be a table, or a CSV, JSON, JSON Lines, or Parquet file in a volume, with one column for the prompt and one that says whether it should be blocked. If you don't, I'll use two public safety datasets: Aegis 2.0 (NVIDIA) and ToxicChat (LMSYS).

  - **They have one:** ask for the table name or the volume path. Look at its columns with them (`DESCRIBE TABLE`, or the file's first rows), and agree on the prompt column, the label column, the label values that mean "should be blocked", and, optionally, a category column. Record it, for example `{"source": "table", "table": "<catalog>.<schema>.<table>", "prompt_column": "text", "label_column": "label", "block_values": ["unsafe"], "category_column": "category", "name": "support_prompts"}`, or `"source": "file"` with `path` and `format` (`csv`, `json`, `jsonl`, or `parquet`).
  - **They don't:** record `{"source": "default"}`. The defaults are the test splits of Aegis 2.0 and ToxicChat, pinned to exact revisions (step 5 states their licenses). Without the user's go-ahead on ToxicChat's license, use `{"source": "default", "defaults": ["aegis2"]}`.

Derive the following and show them back to the user:

```
catalog, schema     = <target_schema>
prefix              = <prefix>                         # tables <prefix>_prompts, _results, _metrics; volume <prefix>_datasets
name                = <≤40 chars, [a-z0-9-]>           # bundle and job
model_service       = <catalog>.<schema>.<prefix>_model
experiment          = /Users/<user>/<name>             # MLflow
```

## 2. Tooling and base skills

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 3).

```bash
databricks --version                     # >= v1.11.0
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
```

`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`. If it exits non-zero, stop: the modules cite reference files that the installed base skills don't have. Ask the user to install or update databricks-agent-skills (v0.2.27 or later) in the same skills directory, for example with `databricks aitools update`.

## 3. The workspace

```bash
databricks schemas get <catalog>.<schema> --profile <profile>
WAREHOUSE_ID=$(databricks experimental aitools tools get-default-warehouse --profile <profile>)   # or pick from `databricks warehouses list`
databricks api get /api/2.1/unity-catalog/model-services/<base_model_service> --profile <profile>
```

- Record the warehouse as `warehouse_id`. If the schema doesn't exist and the user can create it, create it after the go-ahead in step 5.
- The base model service and each of the `evaluators` must be model services the user can read; `system.ai` ones usually are.
- **The Service Policies preview** ("Unity Gateway Service Policies") must be on: a workspace admin enables it on the workspace's Previews page. Ask the user to confirm it is. Without it, module 02 can't attach the guardrails.
- With a dataset of the user's, count its rows and labels: `SELECT <label_column>, count(*) FROM <table> GROUP BY 1`. Both kinds of prompt should be there; with only one, say which rates can't be measured (recall needs prompts to block, the false-positive rate needs prompts to allow).

## 4. Permissions

The user running the skill needs:

| On | Privilege |
|---|---|
| target schema | `USE SCHEMA`, `CREATE TABLE`, `CREATE VOLUME`, and `CREATE SERVICE` (the model service); `CREATE SCHEMA` on the catalog if the schema is new |
| model services | `EXECUTE` on the base model service and on each evaluator, plus `USE CATALOG`/`USE SCHEMA` on their catalog and schema |
| the user's dataset | `SELECT` on the table, or `READ VOLUME` on its volume |
| workspace | create Lakeflow jobs; serverless compute; egress to huggingface.co for the default datasets (module 01 has a fallback) |

The user owns the model service they create, which gives them `MANAGE` on it, needed to attach policies. Check with `databricks grants get-effective schema <catalog>.<schema> --profile <profile>`. Report any missing privilege. Don't try to grant it. If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md`.

## 5. Cost and license go-ahead

Summarize what each step creates or bills, then wait for an explicit yes:

- **Dataset:** a volume, `<prefix>_datasets`, and a table, `<prefix>_prompts`. The default datasets are about 1,900 prompts (Aegis 2.0) and 5,000 (ToxicChat).
- **Model service:** `<model_service>`, with the chosen guardrails attached. It bills nothing while idle.
- **Each evaluation run:** a serverless job. Every prompt calls the guardrails' evaluator models (Unsafe Content, Jailbreak) and, when the guardrails let it through, the base model, for at most `max_tokens` (64) tokens. A sample run is `sample_size` prompts per dataset and label (400 with both defaults); a full run is every prompt, about 6,900 with both defaults, roughly 30 to 60 minutes at 8 in flight.
- **Licenses, for the default datasets:** Aegis 2.0 is CC BY 4.0 (attribution). ToxicChat is CC BY-NC 4.0: non-commercial use only. Ask the user to confirm their use fits each license, and leave out a dataset they can't use.

## 6. Write `guardrail-eval.json`

After the go-ahead, write the resolved values to `<workdir>/guardrail-eval.json`:

```json
{
  "profile": "<profile>",
  "catalog": "<catalog>",
  "schema": "<schema>",
  "prefix": "guardrail_eval",
  "name": "guardrail-eval",
  "warehouse_id": "<warehouse_id>",
  "dataset": {"source": "default"},
  "model_service": "<catalog>.<schema>.guardrail_eval_model",
  "base_model_service": "system.ai.claude-sonnet-5-5",
  "guardrails": ["sensitive_data", "unsafe_content", "jailbreak"],
  "evaluators": {"unsafe_content": "system.ai.gemma-3-12b", "jailbreak": "system.ai.gpt-oss-20b"},
  "concurrency": 8,
  "max_tokens": 64,
  "sample_size": 100,
  "settle_minutes": 5,
  "experiment": "/Users/<user>/guardrail-eval",
  "completed": []
}
```

Add `targets` if the user gave any. Later modules add nothing; they read this file.

## Verify

- Every input is resolved and echoed back. `name` is ≤40 characters and matches `^[a-z0-9-]+$`, and `prefix` matches `^[a-z0-9_]+$`.
- The user answered the dataset question, and `dataset` is recorded; with defaults, the user approved each license.
- `check_base_skills.py` exits 0, and the user confirmed the Service Policies preview is on.
- The user approved the cost summary, and `guardrail-eval.json` exists.
