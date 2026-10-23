# 01 · Ingest and parse

Set up the bundle-managed Lakeflow job and parse every supported file in the volume with `ai_parse_document`. Classification and extraction come later in the same job, once modules 02–03 have designed them.

## Inputs → outputs

- **Inputs:** `extraction.json` from module 00.
- **Outputs:** the `<name>-extract/` bundle, job `doc_extraction`, and table `<catalog>.<schema>.<prefix>_parsed` (one row per document).

## Steps

1. **Choose parse options** and record them in `extraction.json`. **Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_parse_document` section (options, output schema, limits).
   - `description_element_types`: `*` (the default) writes AI descriptions of figures and tables, which help when a value lives in a chart or a stamp. Use `""` for plain forms to cut cost.
   - `page_images_path`: required when `localize` is on (module 06 crops the page images), else leave it out. Create the volume first with `CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.<prefix>_page_images`, then record `/Volumes/<catalog>/<schema>/<prefix>_page_images`. If the volume can't be created, **load** `databricks-unity-catalog` `references/6-volumes.md`. Turning localization on later means parsing again, so decide now.

2. **Render the bundle:**

   ```bash
   uv run <skill>/scripts/render.py --state <workdir>/extraction.json
   ```

   This writes `<workdir>/<name>-extract/databricks.yml` and `src/extraction_config.json`, and copies the job's scripts to `src/`. Until `document_types` is recorded, the job's trigger is rendered `PAUSED`, so new files don't start runs with nothing to extract. To change a value later, edit `extraction.json` and render again. The script stops rather than overwrite a file that was edited by hand.

3. **Validate and deploy.** Deploying creates the job but doesn't run it. **Load:** `databricks-dabs` (validate, deploy, and run).

   ```bash
   cd <workdir>/<name>-extract
   databricks bundle validate --profile <profile>
   databricks bundle deploy --profile <profile>
   ```

4. **Parse now.** This is the parse the user approved in module 00. Run only the parse task:

   ```bash
   databricks bundle run doc_extraction --only parse --profile <profile>
   ```

   Later runs parse only new or changed files (keyed on `path` + `modificationTime`), and drop files removed from the volume.

## How the parse stage works

`parse.py` is the shared Forge parse stage; `doc-qa-agent` runs the same file. It lists the volume without reading file content, diffs the listing against the table, records files over 100 MB as skipped, and parses the rest in batches using delete-then-insert, so each file is billed once.

- **PDFs over 500 pages:** `ai_parse_document` fails on them without `pageRange`, which must be a literal. `parse.py` counts pages with `pypdf`, parses `1-500`, `501-1000`, ... in separate statements, and stitches the slices into one document, renumbering pages and elements. Pages no slice returned are listed in `parse_error`. These rows have `parse_mode = 'page_range'`.
- **Split pages** (`split_pages` on): each page file is parsed once into `<prefix>_page_files`, then each folder's pages are assembled into one `<prefix>_parsed` row (`parse_mode = 'assembled'`), ordered by the number in the file names, else by the printed page numbers, else by file name.
- **An empty listing** skips deletions instead of wiping the tables.

## Verify

```sql
SELECT parse_mode, count(*) AS documents,
       count_if(parse_error IS NOT NULL) AS with_errors,
       sum(size(try_cast(parsed:document.pages AS ARRAY<VARIANT>))) AS pages
FROM <catalog>.<schema>.<prefix>_parsed GROUP BY parse_mode;

SELECT file_name, parse_mode, parse_error FROM <catalog>.<schema>.<prefix>_parsed
WHERE parse_error IS NOT NULL LIMIT 20;
```

- The document count matches the files from preflight (or the folders, with split pages).
- Report `skipped` documents and errors to the user. Per-page errors can still leave a document usable.
- For `page_range` or `assembled` documents, `pages` matches the source page counts. These rows were stitched by `parse.py`, not returned by one `ai_parse_document` call; if module 02 or 03 later fails on them alone, report it to the skill owner.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ai_parse_document` not found / VARIANT unsupported | Serverless environment must be ≥ 3 (`environment_version` in `scripts/databricks.yml`; **load** `databricks-serverless`); on classic compute DBR 17.3+ |
| AI function unavailable in region | Check AI Functions region availability (`databricks-ai-functions`); no workaround in-skill |
| `PERMISSION_DENIED` reading the volume | The job runs as the deployer; needs `READ VOLUME` + `USE CATALOG`/`USE SCHEMA` (**load** `databricks-unity-catalog` `references/6-volumes.md`) |
| Job doesn't trigger on new files | Expected until `document_types` exists (the trigger is `PAUSED`). After that, a `mode: development` target would force it to PAUSED, so the template has none; **load** `databricks-jobs` `references/triggers-schedules.md#file-arrival-trigger` |
| `stitched slices could not be stored as one VARIANT` | The stitched document is too large for one VARIANT value. Split the PDF upstream, or extract from the slices separately |
| Split-page document assembled in the wrong order | File names lack a page number and pages print none. Rename the files with page numbers, then rerun the parse task |
| `render.py` says a file was edited by hand | Move the change into `extraction.json` and render again; `--force` discards the hand edit |
