# 01 · Ingest and parse

Set up a bundle-managed Lakeflow job that parses every supported file in the volume with `ai_parse_document`, and re-runs when new files arrive.

## Inputs → outputs

- **Inputs:** `doc-qa.json` from module 00.
- **Outputs:** the `<name>-ingest/` bundle, job `doc_qa_ingest`, and table `<catalog>.<schema>.<prefix>_parsed` (one row per file).

## Steps

1. **Choose parse options** and record them in `doc-qa.json`. **Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_parse_document` section (options, output schema, limits).
   - `description_element_types`: `*` (the default) generates AI descriptions of figures and tables. Those descriptions flow into `chunk_to_embed` and help with chart- or table-heavy documents. Use `""` for text-only corpora to cut cost.
   - `page_images_path`: only when `page_images` is on. Create the volume first with `CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.<prefix>_page_images`, then record `/Volumes/<catalog>/<schema>/<prefix>_page_images`. If the volume can't be created, **load** `databricks-unity-catalog` `references/6-volumes.md`.

2. **Finish module 02's design step** (step 1, the metadata schema). The parse and chunk stages run in the same job task, so the schema must be final before the first run.

3. **Render the bundle:**

   ```bash
   uv run <skill>/scripts/render.py ingest --state <workdir>/doc-qa.json
   ```

   This writes `<workdir>/<name>-ingest/databricks.yml` with every value from `doc-qa.json` and copies `ingest.py`, `sync_index.py`, and the two SQL files to its `src/`. To change a value later, edit `doc-qa.json` and render again. The script stops rather than overwrite a `databricks.yml` that was edited by hand.

4. **Validate and deploy.** Deploying creates the job but doesn't run it. **Load:** `databricks-dabs` (validate, deploy, and run).

   ```bash
   cd <workdir>/<name>-ingest
   databricks bundle validate --profile <profile>
   databricks bundle deploy --profile <profile>
   ```

5. **Run once now.** This is the parse the user approved in module 00:

   ```bash
   databricks bundle run doc_qa_ingest --profile <profile>
   ```

   The first run parses everything. Later runs are triggered by file arrival and parse only new or changed files (keyed on `path` + `modificationTime`). Files deleted from the volume are removed from both tables. The run fails at `sync_index` until module 02's endpoint exists. That's expected if you deploy before finishing module 02.

## How the parse stage works

`ingest.py` lists the volume with `binaryFile`, without reading file content. It diffs the listing against `<prefix>_parsed`, records files over 100 MB as skipped, and parses the rest in batches of 50 using delete-then-insert, so each file is billed once. Each batch commits on its own, so a failed run resumes where it stopped. If the listing is empty, deletions are skipped instead of wiping the tables.

## Verify

```sql
SELECT count(*) AS files,
       count_if(parsed IS NULL) AS skipped,
       count_if(parse_error IS NOT NULL) AS with_errors,
       sum(size(cast(parsed:document:pages AS ARRAY<VARIANT>))) AS pages
FROM <catalog>.<schema>.<prefix>_parsed;

SELECT file_name, parse_error FROM <catalog>.<schema>.<prefix>_parsed
WHERE parse_error IS NOT NULL LIMIT 20;
```

- `files` matches the supported-file count from preflight.
- Report `skipped` and `with_errors` to the user. Per-page errors can still leave a file usable, because module 02 chunks whatever was parsed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ai_parse_document` not found / VARIANT unsupported | Serverless environment must be ≥ 3 (`environment_version` in `scripts/databricks.yml`; **load** `databricks-serverless`); on classic compute DBR 17.3+ |
| AI function unavailable in region | Check AI Functions region availability (`databricks-ai-functions`); no workaround in-skill |
| `PERMISSION_DENIED` reading the volume | The job runs as the deployer; needs `READ VOLUME` + `USE CATALOG`/`USE SCHEMA` (**load** `databricks-unity-catalog` `references/6-volumes.md`) |
| Job never triggers on new files | A `mode: development` target forces triggers to PAUSED (and rejects overriding it), so the template's target has no mode. Check the job's trigger status in the UI, and **load** `databricks-jobs` `references/triggers-schedules.md#file-arrival-trigger` |
| Non-Latin scans or signed PDFs parse poorly | Known `ai_parse_document` limitation; tell the user, and consider OCR upstream |
| `render.py` says a file was edited by hand | Move the change into `doc-qa.json` (for example `schedule_cron`) and render again; `--force` discards the hand edit |
