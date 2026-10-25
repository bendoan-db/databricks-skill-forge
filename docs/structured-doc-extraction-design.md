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
- **Typed tables.** `extract.py` derives DDL and SELECT fragments from each schema, and stops if an existing table lacks a column the schema now needs. Column names are backquoted, since fields like `date` collide with SQL keywords. The wrapper shape for arrays isn't documented, so array SQL unwraps both `{value: [...]}` and bare arrays.
- **Paused until designed.** Module 01 parses with `bundle run --only parse` so the agent can read samples before designing labels and schemas. Until `document_types` exists, `render.py` renders the trigger `PAUSED`.
- **Config file.** The job reads `src/extraction_config.json`, not task parameters, because schemas can exceed task parameter size limits. It carries the same checksum protection as `databricks.yml`, in a `_rendered_by` key.
- **Shared files.** `parse.py` and `parse.sql` are listed under `shared` in this skill's and `forge-unstructured-doc-extraction`'s `ucs.yaml`, and `check_base_skills.py` in every skill's; `tests/test_skills.py` fails if the copies differ. Change one, then copy it over the others.

## Changes in 0.2.0

- **Renamed `forge-structured-doc-extraction`,** and normally started by the `forge` router. The description no longer competes with the base skills, and the routing evals moved to `skills/forge/evals/routing.yaml` (`forge-design.md`). No change to the modules or the job.

## Not yet verified in a workspace

Nothing here has run against a workspace. The static tests cover rendering, validation, SQL placeholders, stitching, scoring, and OCR matching. These platform behaviors are assumptions:

1. `ai_classify`, `ai_extract`, and `ai_prep_search` accept a VARIANT built with `parse_json(to_json(named_struct(...)))` (segment slices, stitched and assembled documents, chart-patched documents) as they accept `ai_parse_document` output. ADP's page pipeline hedged on this ("switch to page_variant if your ai_extract build accepts it"). Module Verify steps tell the agent to report failures on such rows.
2. `pageRange` must be a literal (ADP's finding; the docs show only literals). A `pageRange` slice numbers its pages either from 0 or from its first page; `stitch()` handles both.
3. `ai_classify` v2.1 returns `confidence_score` when `enableConfidenceScores` is set; the docs list the option, and their example shows the field.
4. `ai_extract` v2.1 citations carry `bbox[].coord` in page-image pixels, the space ADP's crops used.
5. `CREATE TABLE IF NOT EXISTS a LIKE a` is a no-op when the table exists (used when split pages are off).
6. The OCR cluster's init script reaches the Ubuntu mirrors, and workspace-file init scripts are allowed on dedicated (`SINGLE_USER`) clusters.
7. A stitched document over the VARIANT size limit makes `try_parse_json` return NULL rather than fail the statement.

The certification scenarios in `evals/scenarios.yaml` (`long-pdf`, `split-pages`, `paystubs-single-type-localized`, `mortgage-packets`) exercise each of these.
