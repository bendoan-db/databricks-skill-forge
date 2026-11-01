# forge-structured-doc-extraction design notes

Maintainer notes for `skills/forge-structured-doc-extraction`, and for the parts of `doc-qa-agent` 0.3.0 built from the same source (now in `forge-unstructured-doc-extraction`; see `unstructured-doc-extraction-design.md`). This file sits outside the skill folders, so it isn't published and agents never load it.

## Where the source repo went

The skill is built from [advance-document-processing](https://github.com/databricks-solutions/advance-document-processing) (ADP), commit `60ab408` (2026-10-08). ADP has seven folders, but only two end-to-end use cases. The rest are stages or techniques, so they became modules or decision points, not skills of their own:

| ADP folder | Became |
|---|---|
| `document-page-classify-extraction` | This skill: layout `packet` (modules 02–04) |
| `ai-extract-word-level-citation` | This skill: layout `single_type`, and module 06 (localization) |
| `evaluation-harness` | This skill: module 05. Recipes 01–02 are the confidence profile; 03–04 are `evaluate.py` |
| `ai-search-knowledge-base-creation` | `forge-unstructured-doc-extraction` (built as `doc-qa-agent` 0.3.0): its classify step is a `document_type` metadata filter; the index-only scope became that skill, and the per-domain decision point moved with it |
| `document-embedding-chart-analysis` | `forge-unstructured-doc-extraction`: optional chart insights (module 02, step 3) |
| `parse-pdfs-with-large-num-of-pages` | Shared parse stage (`parse.py`): 500-page slices |
| `combine-document-pages` | Shared parse stage (`parse.py`): `--split-pages folder` |

One skill per folder would have meant seven skills that all start with "parse the PDFs in a volume", competing for the same prompts and repeating preflight, parsing, and guardrails. Skills with different outputs (an index, an agent, tables) route cleanly; `forge` routes by output, and its routing evals test that. The ADP split made two skills, `doc-qa-agent` and this one; `forge-doc-qa-agent` 0.4.0 later split its index into `forge-unstructured-doc-extraction`.

## Changes from ADP

| ADP | This skill | Why |
|---|---|---|
| Batch bundles reread the whole volume with `CREATE OR REPLACE` | Incremental parse keyed on `(path, modificationTime)`; downstream stages keyed on `(path, parsed_at)` in `<prefix>_progress` | Every ADP batch run re-billed parsing (and re-embedding) for every file |
| Each pipeline has its own bronze stage, with different defaults | One shared `parse.py`, identical in every skill that parses | One place to fix parsing; the copies are tested for drift |
| Long-PDF stitching rewrote pages as `{page: {pageNumber, width, height}}` and left element bbox `page_id`s unshifted | `stitch()` renumbers `pages[].id`, `elements[].id`, `bbox[].page_id`, and `error_status[].page_id`, keeps every other field, and reports missing pages as errors | Downstream reads `pages[].id` and `bbox[0].page_id`; ADP's output didn't match the v2 schema its own pipelines read |
| Split-page merge set a `page_number` field that the v2 schema doesn't have, on the driver | Same `stitch()`, in a Python UDF | Same schema fix; no collect to the driver |
| Long-PDF partial-slice guard printed a warning | Missing pages go into `parse_error` | A warning in a job log is never read |
| `ai_classify` default version, reading `response[0]::string` (pages, charts), and v2.1 reading `response[0].value` (KB) | v2.1 everywhere, with `enableConfidenceScores` | Docs recommend 2.1; confidence feeds the review queue |
| Page extraction from `page_text` strings | Segment VARIANT (the parsed VARIANT cut to the segment's pages) by default; text as a fallback | Bounding-box citations need VARIANT input (docs); module 06 needs them |
| One row per page, even for multi-page documents | Per type `pages: single \| multi`; consecutive pages of a multi-page type form one segment | A three-page statement extracted page by page loses fields; two adjacent pay stubs must stay separate |
| Hand-written gold flattening per schema (`FIELD_SPECS`) | Typed tables generated from each schema: scalars, `__confidence` columns, child tables for arrays of objects | Any schema, no per-field code |
| Recipe 04 scored unlabeled fields as 0.0 (its README's known limitation) | Only labeled keys are scored; `null` means "the document lacks it" | The fix ADP planned for its next iteration |
| Word-level bundle on serverless env 5 | Classic single-node job cluster with an init script that installs `tesseract-ocr` | The Tesseract binary isn't on serverless (`databricks-serverless-migration`); ADP's results table is still "TBD" |
| Numeric OCR match accepted any token run with the right digits | Runs containing letters are rejected for numbers | "Gross Pay $1,884.00" matched 1884, so word boxes covered the label too (found by `test_localize_finds_the_printed_number_inside_the_cited_element`) |
| Chart VLM via the OpenAI client and the notebook's API token, `databricks-claude-sonnet-4-5` | `ai_query(system.ai model, prompt, files => crop, failOnError => false)` | No token handling; Unity Gateway model names |
| Chart insights spliced into one `full_text` string per document | Appended to the figure's `description` in the parsed VARIANT before `ai_prep_search` | Keeps page numbers, so `[file_name, p. N]` citations still work |
| Two indexes routed by document type | One index with a metadata filter; separate runs per domain only for access boundaries | Fewer moving parts; routing by type is just a filter |
| Agent Bricks Information Extraction cited as the scoring reference | Not used or suggested | Being deprecated (owner direction, 2026-10-23) |

## Runtime design (v0.1.0)

- **Layouts and segments.** Every layout produces `<prefix>_segments` rows, `(path, segment_id, doc_type, page_ids)`, so extraction, publishing, review, and evaluation don't branch on layout. Only module 02's SQL does.
- **`<prefix>_progress`.** One ledger per prefix: `(path, parsed_at, stage)`. A stage processes a document when the ledger lacks its current parse, so documents that yield nothing (no chunks, no fields) aren't re-billed every run. `full_refresh` clears a stage's rows.
- **Typed tables.** `extract.py` derives DDL and SELECT fragments from each schema, and stops if an existing table lacks a column the schema now needs. Column names are backquoted, since fields like `date` collide with SQL keywords. The docs (2026-10-22) say arrays keep their shape and each element's fields carry the wrapper. The array SQL still unwraps both `{value: [...]}` and bare arrays, and wrapped and bare items, in case a version wraps the array itself.
- **Paused until designed.** Module 01 parses with `bundle run --only parse` so the agent can read samples before designing labels and schemas. Until `document_types` exists, `render.py` renders the trigger `PAUSED`.
- **Config file.** The job reads `src/extraction_config.json`, not task parameters, because schemas can exceed task parameter size limits. It carries the same checksum protection as `databricks.yml`, in a `_rendered_by` key.
- **Shared files.** `parse.py` and `parse.sql` are listed under `shared` in this skill's and `forge-unstructured-doc-extraction`'s `ucs.yaml`, and `check_base_skills.py` in every skill's; `tests/test_skills.py` fails if the copies differ. Change one, then copy it over the others.

## Changes in 0.2.0

- **Renamed `forge-structured-doc-extraction`,** and normally started by the `forge` router. The description no longer competes with the base skills, and the routing evals moved to `skills/forge/evals/routing.yaml` (`forge-design.md`). No change to the modules or the job.

## Changes in 0.3.0: the review app

Module 07 adds a human-in-the-loop review app, built from [hil-doc-extraction](https://github.com/bendoan-db/hil-doc-extraction) at `1ad41cd`. Only its app and its review-publication path were ported. The prompt- and schema-optimization loop (`app/optimize/`, `resources/prompt_optimization.job.yml`) was left out on purpose, at the owner's request (2026-11-01).

| Source | Went to |
|---|---|
| `ingest/notebooks/07_sync_hil_staging.py` (below-cutoff fields into staging) | `merge_candidates` in `extract.sql`: values below `review_threshold` into `<prefix>_review_candidates`, at the end of each extraction run |
| `ingest/notebooks/08_sync_hil_to_lakebase.py` (VARIANT-free projection, synced table) | The candidates table has no VARIANT columns, so it's the projection. Module 07 creates the synced table once with the CLI; the job doesn't manage control-plane resources |
| `ingest/notebooks/10_publish_lakebase_reviews.py` (CDC history into staging, audit, Gold) | `extract.py --stage decisions`: `merge_decisions` into `<prefix>_decisions`, then the typed rows. The `review_sync` job runs it on a table update trigger, as the source's publication job did |
| `resources/hil_review_publication.job.yml` | The `review_sync` block in `scripts/databricks.yml` |
| `app/hil-review-app/server/routes/review-routes.ts`, `review-schema.ts` | `scripts/review-app/review-routes.ts` |
| `app/hil-review-app/server/server.ts` | `scripts/review-app/server.ts` (no jobs plugin: it only triggered optimization) |
| `app/hil-review-app/client/src/` (workspace, queue, viewer, field panel, value parsing) | `scripts/review-app/App.tsx`, one file |
| `app/hil-review-app/grant_app_lakebase_access.py` | Module 07, step 5: two `GRANT`s through `databricks psql` |
| The rest of the app project (package.json, configs, lockfile) | Not copied. `databricks apps init` scaffolds it at the current AppKit version, which the `databricks-apps` base skill says not to override. That also keeps the skill under the 50-file limit |

**What changed, and why:**
- **Decisions apply to the typed tables, not a flat Gold table.** The source published every field to one row per field (`gold_document_fields`). This skill's output is one typed table per document type, so a decision overrides its value in place. The publish SQL reads a `reviewed` map (field path to value) beside each segment, and both stages use it: the extract stage, so decisions survive `full_refresh`, and the decisions stage, which updates rows in place with MERGE.
  - A decided value gets confidence 1.0, and its field goes in `reviewed_fields`. The model's own scores stay in `<prefix>_fields`, so module 05 still measures the model.
- **One Postgres table, not two.** The source wrote `review_records` (current state) and `review_decision_events` (an outbox), in one transaction. Here `<prefix>_decisions` holds the current state, with `row_version` for concurrent reviewers and `decision_request_id` for safe retries. Lakehouse Sync's SCD Type 2 history of that table is the audit trail.
- **Decisions are made against one parse.** `review_id` hashes `(path, parsed_at, segment_id, doc_type, field)`, and the overlay joins on the same keys. A changed file needs new decisions, and a re-extraction of the same parse keeps them.
  - The app copies `parsed_at` from the queue in SQL. A JavaScript `Date` would drop its microseconds, and the join would miss.
- **No retry stage.** The source's `ai_query` retry (stages 05-06) and its routing reasons have no counterpart: this skill sends values below `review_threshold` straight to review. Reviewers see the `ai_extract` value and its confidence.
- **TRIGGERED, not CONTINUOUS, sync.** The queue only changes when the extraction job runs, so the job's `review_queue` task refreshes the synced table after each run. That avoids an always-on sync pipeline.
- **Lakehouse Sync is set up with the CLI.** The source configured it in the Lakebase UI, because there was no API at the time. `databricks postgres create-cdf-config` now covers it.
- **No registered Lakebase catalog.** The synced table lives in the build's own schema, as the base skill recommends, so its Postgres schema is that schema's name. That's why `render.py` requires `schema` and `prefix` to be Postgres-safe.
- **Values stay text.** The queue carries `<prefix>_fields.value` as text, and the app checks a correction against the field's type: numbers, integers, booleans, and enum labels. The typed tables cast it. The source stored typed JSONB, because its Gold table was one row per field.
- **Reviewers can change a decision.** The source treated a decision as final in the app. Here a decision can be changed, which bumps `row_version`.
- **`ai_extract` v2.1 limits are checked before the job runs:** 150-character field names, 12 levels of nesting, 500 enum labels (array items included, which weren't checked before), and `instructions` under 20,000 characters, as well as the 256 fields. The docs don't say how fields or levels are counted. `extract.py` counts fields as values, and levels as the names in a field's path, so a schema near a limit can still fail in the workspace.
- **Preflight asks for an existing schema.** A user with one gives it in any format. It's saved as `provided_schema.<ext>`, and module 03 converts it. Without one, module 03 builds the schema with the user, field by field. It isn't written to `document_types` in preflight, because the job's trigger unpauses once `document_types` exists. Owner direction, 2026-11-01.
- **Preflight asks.** `review_app` is a required input with no default: module 00 asks yes or no, and `render.py` refuses to render until the answer is recorded. Yes runs module 07 after the pipeline modules; no creates nothing for the app. Owner direction, 2026-11-01.

## Changes: job tasks are notebooks

- **Why:** users should be able to open and change the job's code in their workspace. `parse.py`, `extract.py`, and `localize.py` are Databricks notebooks (source format), and every job task is a `notebook_task`. Bundle deploy imports a `.py` file with the notebook header as a notebook.
- **Settings:**
  - Each task's `base_parameters` arrive as widgets, and `notebook_argv()` turns them back into the script's own flags, so the same file still runs locally through Databricks Connect.
  - The job parameter `full_refresh` reaches the notebooks as a widget without being repeated in `base_parameters`.
  - A notebook has no `__file__` and no `__doc__`, so `script_dir()` falls back to the working directory (a notebook's own folder), and the argparse description tolerates a missing docstring.
- **Dependencies:** a `%pip` cell in the notebook replaces the job `environments` block. The base skills disagree on whether a notebook task takes an `environment_key`: `databricks-jobs` shows one, and `databricks-serverless` says notebook tasks need nothing else. `%pip` works either way. `localize.py` keeps its cluster libraries on the classic job cluster.
- **Keeping workspace edits:**
  - A deploy replaces the workspace copies, so module 01 has the user export an edited notebook (or SQL file) into the bundle's `src/`.
  - `render.py` marks each copied job file with a checksum line, and keeps a copy that no longer matches it, listing it under `kept_edited`. `--force` replaces it.
  - A copy without the line, from an older `render.py`, is replaced, since the user can't have edited it after this change.

## Not yet verified in a workspace

Nothing here has run against a workspace. The static tests cover rendering, validation, SQL placeholders, stitching, scoring, and OCR matching. These platform behaviors are assumptions:

1. `ai_classify`, `ai_extract`, and `ai_prep_search` accept a VARIANT built with `parse_json(to_json(named_struct(...)))` (segment slices, stitched and assembled documents, chart-patched documents) as they accept `ai_parse_document` output. ADP's page pipeline hedged on this ("switch to page_variant if your ai_extract build accepts it"). Module Verify steps tell the agent to report failures on such rows.
2. `pageRange` must be a literal (ADP's finding; the docs show only literals). A `pageRange` slice numbers its pages either from 0 or from its first page; `stitch()` handles both.
3. `ai_classify` v2.1 returns `confidence_score` when `enableConfidenceScores` is set; the docs list the option, and their example shows the field.
4. `ai_extract` v2.1 citations carry `bbox[].coord` in page-image pixels, the space ADP's crops used.
5. `CREATE TABLE IF NOT EXISTS a LIKE a` is a no-op when the table exists (used when split pages are off).
6. The OCR cluster's init script reaches the Ubuntu mirrors, and workspace-file init scripts are allowed on dedicated (`SINGLE_USER`) clusters.
7. A stitched document over the VARIANT size limit makes `try_parse_json` return NULL rather than fail the statement.

8. The review app's SQL:
   - `map_contains_key` and outer column references inside `transform` lambdas, in the publish SQL;
   - `MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE` on the candidates table;
   - `UPDATE SET *` from a temp view.
   None of these has run, even under `EXPLAIN`.
9. A synced table in `TRIGGERED` mode exposes a `pipeline_id`, and a job's `pipeline_task` with that id syncs it. The docs describe this as a "Database Table Sync pipeline" task.
10. A table update trigger on the Lakehouse Sync history table fires on its updates, and `wait_after_last_change_seconds` must be over 60 (from the source; the docs give no range).
11. The review app was written against AppKit 0.84.0's scaffold (`databricks apps init --features lakebase,files`), but hasn't been built, type-checked, or deployed:
    - the `files` plugin's `volumes` and `policy` options;
    - `appkit.lakebase.pool`;
    - the `@databricks/appkit-ui` components it imports.
    The source app used the same APIs at AppKit 0.38.1.
12. Concurrent writes: a `review_sync` MERGE and an extraction run's delete-then-insert on different documents of one typed table don't conflict (row-level concurrency). On the same document, the task's retries cover it.

13. The workspace's default serverless environment, which notebook tasks run on, supports what `parse.py` and `extract.py` need: VARIANT, `ai_parse_document`, `ai_classify`, and `ai_extract` from `spark.sql`. The job's `environment_version: "4"` used to pin this.
14. A `%pip install` cell works in a serverless notebook task, with egress to PyPI, and pypdf reaches the workers that count pages.
15. The job parameter `full_refresh` reaches the `extract` notebook as a widget without being listed in `base_parameters`.
16. A notebook exported with `databricks workspace export --format SOURCE` keeps `render.py`'s checksum line within two lines of the top. If the export reformats an unedited notebook, `render.py` keeps it as edited, which is safe but stops skill updates reaching it.

The certification scenarios in `evals/scenarios.yaml` (`long-pdf`, `split-pages`, `paystubs-single-type-localized`, `mortgage-packets`, `review-app-round-trip`) exercise each of these.
