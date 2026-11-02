# 01 · Dataset

Set up the bundle-managed job, and load the evaluation's prompts into one table, each labeled BLOCK (the guardrails should block it) or ALLOW.

## Inputs → outputs

- **Inputs:** `guardrail-eval.json` from module 00, with its `dataset`.
- **Outputs:** the `<name>/` bundle, job `guardrail_eval`, volume `<catalog>.<schema>.<prefix>_datasets`, and table `<catalog>.<schema>.<prefix>_prompts`: one row per prompt, with `dataset`, `expected` (BLOCK or ALLOW), and `categories`.

## Steps

1. **Render the bundle:**

   ```bash
   uv run <skill>/scripts/render.py --state <workdir>/guardrail-eval.json
   ```

   This writes `<workdir>/<name>/databricks.yml` and `src/eval_config.json`, and copies the job's notebooks to `src/`. To change a value later, edit `guardrail-eval.json` and render again. The script stops rather than overwrite a rendered file that was edited by hand.

2. **Validate and deploy.** Deploying creates the job but doesn't run it. **Load:** `databricks-dabs` (validate, deploy, and run).

   ```bash
   cd <workdir>/<name>
   databricks bundle validate --profile <profile>
   databricks bundle deploy --profile <profile>
   ```

3. **Load the prompts.** This is the dataset step the user approved in module 00. Run only its task:

   ```bash
   databricks bundle run guardrail_eval --only load_prompts --profile <profile>
   ```

   The task creates the volume if it's missing, and replaces `<prefix>_prompts` on each run. Its output ends with a JSON summary per dataset: rows read, prompts kept, BLOCK and ALLOW, and what was dropped.

## How the prompts are loaded

- **The default datasets** are downloaded from huggingface.co at a fixed revision, into `/Volumes/<catalog>/<schema>/<prefix>_datasets/<dataset>/`, and checked against a sha256. A file already there with the right checksum isn't downloaded again.
  - `aegis2`, Aegis 2.0's test split: BLOCK when `prompt_label` is `unsafe`. Its `violated_categories` (Violence, Hate/Identity Hate, PII/Privacy, Needs Caution, and others) become `categories`. Prompts that say `REDACTED` are dropped: their text isn't distributed with the dataset.
  - `toxicchat`, ToxicChat 0124's test split, real users' prompts: BLOCK when `toxicity` is 1, with category `toxicity`, plus `jailbreaking` when that's 1.
- **The user's dataset** is read from their table or file with the column mapping from module 00. BLOCK when the label, lowercased, is one of `block_values`.
- **Duplicates** (the same prompt twice in one dataset) become one row, and one whose copies disagree on the label is dropped. Each prompt's `prompt_id` is a hash of its dataset and text, so it stays the same across loads and runs.
- **No egress to huggingface.co:** stage the files from a machine that has it, into the paths above, then run the task again. The checksums are in `load_prompts.py` (`DEFAULT_DATASETS`). **Load:** `databricks-unity-catalog` `references/6-volumes.md` if the upload fails.

  ```bash
  curl -fL -o test.json "https://huggingface.co/datasets/nvidia/Aegis-AI-Content-Safety-Dataset-2.0/resolve/d86bb8bedff51d25ac834ab7838f1cc61acb7a2c/test.json"
  databricks fs mkdir dbfs:/Volumes/<catalog>/<schema>/<prefix>_datasets/aegis2 --profile <profile>
  databricks fs cp test.json dbfs:/Volumes/<catalog>/<schema>/<prefix>_datasets/aegis2/test.json --profile <profile>
  ```

  ToxicChat's file is `https://huggingface.co/datasets/lmsys/toxic-chat/resolve/29df8e4dba60e1f4af4b4075c0705c5b313548a8/data/0124/toxic-chat_annotation_test.csv`, staged as `toxicchat/toxic-chat_annotation_test.csv`.

## Editing the job's notebooks

The job's tasks are Databricks notebooks (`load_prompts` and `run_eval`), deployed from `<workdir>/<name>/src/` to the bundle's folder in the workspace. `databricks bundle summary --profile <profile>` shows that folder as `file_path`; the notebooks are in its `src/`, without the `.py` extension. The user can open one there, change it, and run it by hand with the widgets at its top, which the job fills in on its runs.

A deploy replaces the workspace copies with the local ones. To keep a change made in the workspace, export it into the bundle first:

```bash
databricks workspace export <file_path>/src/<notebook> --format SOURCE --file <workdir>/<name>/src/<notebook>.py --profile <profile>
```

`render.py` keeps an edited copy when it renders again, and lists it under `kept_edited`. `--force` replaces it with the skill's version. A kept copy doesn't pick up later changes to the skill, so tell the user which files are kept.

## Verify

```sql
SELECT dataset, expected, count(*) AS prompts FROM <catalog>.<schema>.<prefix>_prompts GROUP BY ALL ORDER BY ALL;

SELECT dataset, category, count(*) AS prompts
FROM <catalog>.<schema>.<prefix>_prompts LATERAL VIEW explode(categories) AS category
GROUP BY ALL ORDER BY prompts DESC LIMIT 20;
```

- Each dataset has the prompts the summary reported. The defaults give 1,914 Aegis 2.0 prompts (1,030 BLOCK, 884 ALLOW) and 4,972 ToxicChat prompts (354 BLOCK, 4,618 ALLOW).
- A dataset with only BLOCK or only ALLOW prompts was reported to the user in module 00.
- Report the counts by dataset and label, and what was dropped. Don't show prompt text unless the user asks.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `<path> has sha256 …, not …` | The staged file isn't the pinned revision. Stage the right file over it, or remove it, after confirming its path with the user, and run the task again |
| `Can't download …` | Serverless has no egress to huggingface.co: stage the files as above (the message prints the commands) |
| `PERMISSION_DENIED` creating the volume or table | `CREATE VOLUME` or `CREATE TABLE` on the schema is missing (module 00, step 4) |
| The user's column isn't found | The mapping in `guardrail-eval.json` names a column the table or file doesn't have: fix `prompt_column`, `label_column`, or `category_column`, render, deploy, and run again |
| Every prompt is BLOCK, or every one ALLOW | `block_values` doesn't match the label column's values: check them with a `GROUP BY` on the label |
| `render.py` says a file was edited by hand | Move the change into `guardrail-eval.json` and render again; `--force` discards the hand edit |
