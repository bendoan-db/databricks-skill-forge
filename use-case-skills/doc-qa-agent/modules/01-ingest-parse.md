# 01 · Ingest and parse

Set up a bundle-managed Lakeflow job that parses every supported file in the volume with `ai_parse_document`, and re-runs when new files arrive.

## Inputs → outputs

- **Inputs:** `doc-qa.json` from module 00.
- **Outputs:** the `<name>-ingest/` bundle, job `doc_qa_ingest`, and table `<catalog>.<schema>.<prefix>_parsed` (one row per file).

## Steps

1. **Choose parse options** and record them in `doc-qa.json`. **Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_parse_document` section (options, output schema, limits).
   - `description_element_types`: `*` (the default) generates AI descriptions of figures and tables. Those descriptions flow into `chunk_to_embed` and help with chart- or table-heavy documents. Use `""` for text-only corpora to cut cost.
   - `page_images_path`: only when `page_images` or `chart_insights` is on (chart insights crop the page images). Create the volume first with `CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.<prefix>_page_images`, then record `/Volumes/<catalog>/<schema>/<prefix>_page_images`. If the volume can't be created, **load** `databricks-unity-catalog` `references/6-volumes.md`.

2. **Finish module 02's design steps** (step 1, the metadata schema, and step 3 if chart insights are on). The chunk task runs right after parsing in the same job, so they must be final before the first run. If documents arrive as one file per page with one folder per document, set `split_pages` to `true` in `doc-qa.json` now.

3. **Render the bundle:**

   ```bash
   uv run <skill>/scripts/render.py ingest --state <workdir>/doc-qa.json
   ```

   This writes `<workdir>/<name>-ingest/databricks.yml` with every value from `doc-qa.json` and copies the job's scripts to its `src/`: `parse.py` and `parse.sql` (the shared Forge parse stage), `prep_chunks.py` and `prep_chunks.sql`, and `sync_index.py`. To change a value later, edit `doc-qa.json` and render again. The script stops rather than overwrite a `databricks.yml` that was edited by hand.

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

   The job runs three tasks: `parse`, `chunk`, and `sync_index`. The first run parses everything. Later runs are triggered by file arrival and parse only new or changed files (keyed on `path` + `modificationTime`). Files deleted from the volume are removed from every table. The run fails at `sync_index` until module 02's endpoint exists. That's expected if you deploy before finishing module 02.

## How the parse stage works

`parse.py` is the shared Forge parse stage; `structured-doc-extraction` runs the same file. It lists the volume with `binaryFile`, without reading file content. It diffs the listing against `<prefix>_parsed`, records files over 100 MB as skipped, and parses the rest in batches of 50 using delete-then-insert, so each file is billed once. Each batch commits on its own, so a failed run resumes where it stopped. If the listing is empty, deletions are skipped instead of wiping the tables.

- **PDFs over 500 pages** are parsed in 500-page `pageRange` slices and stitched into one document (`parse_mode = 'page_range'`). Pages no slice returned are listed in `parse_error`.
- **Split pages** (`split_pages` on): each page file is parsed once, then each folder's pages are assembled into one document (`parse_mode = 'assembled'`), ordered by the number in the file names, else by the printed page numbers.

## Verify

```sql
SELECT parse_mode, count(*) AS documents,
       count_if(parse_error IS NOT NULL) AS with_errors,
       sum(size(try_cast(parsed:document.pages AS ARRAY<VARIANT>))) AS pages
FROM <catalog>.<schema>.<prefix>_parsed GROUP BY parse_mode;

SELECT file_name, parse_error FROM <catalog>.<schema>.<prefix>_parsed
WHERE parse_error IS NOT NULL LIMIT 20;
```

- The document count matches the supported-file count from preflight (or the folder count, with split pages).
- Report `skipped` documents and errors to the user. Per-page errors can still leave a document usable, because module 02 chunks whatever was parsed.
- For `page_range` or `assembled` documents, `pages` matches the source page counts. `parse.py` stitched these rather than one `ai_parse_document` call; if module 02 fails on them alone, report it to the skill owner.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ai_parse_document` not found / VARIANT unsupported | Serverless environment must be ≥ 3 (`environment_version` in `scripts/databricks.yml`; **load** `databricks-serverless`); on classic compute DBR 17.3+ |
| AI function unavailable in region | Check AI Functions region availability (`databricks-ai-functions`); no workaround in-skill |
| `PERMISSION_DENIED` reading the volume | The job runs as the deployer; needs `READ VOLUME` + `USE CATALOG`/`USE SCHEMA` (**load** `databricks-unity-catalog` `references/6-volumes.md`) |
| Job never triggers on new files | A `mode: development` target forces triggers to PAUSED (and rejects overriding it), so the template's target has no mode. Check the job's trigger status in the UI, and **load** `databricks-jobs` `references/triggers-schedules.md#file-arrival-trigger` |
| Non-Latin scans or signed PDFs parse poorly | Known `ai_parse_document` limitation; tell the user, and consider OCR upstream |
| `stitched slices could not be stored as one VARIANT` | The stitched document is too large for one VARIANT value; split the PDF upstream |
| `render.py` says a file was edited by hand | Move the change into `doc-qa.json` (for example `schedule_cron`) and render again; `--force` discards the hand edit |
