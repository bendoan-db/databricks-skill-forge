---
name: forge-structured-doc-extraction
description: "Forge use case, normally started by the forge skill: extracts fields, entities, and line items from the documents in a Unity Catalog volume (PDF, DOCX, PPTX, images) into typed Delta tables, with a citation and confidence score for every value: incremental ai_parse_document parsing, ai_classify v2.1 routing for mixed documents and multi-document packets such as loan or claim files, ai_extract v2.1 per document type, a review queue of low-confidence and unscored values with cutoffs calibrated per document type, an optional review app (Lakebase and AppKit) where people approve or correct them and their decisions are written back to the tables, accuracy scoring against labeled documents in MLflow, and optional word-level bounding boxes from OCR. Use when forge hands off to it, or when the user names this skill."
compatibility: Runs locally, with databricks CLI (>= v1.11.0), Python 3.11+, and uv, or in Genie Code in a notebook, on serverless compute (environment 2 or later). Requires the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill. Word-level localization also needs permission to create a classic job cluster. The review app needs a Lakebase Autoscaling project, and locally Node.js 20+, pnpm, and psql; in a notebook its code is generated for the user to deploy.
metadata:
  version: "0.4.0"
  forge_kind: use-case
---

# Structured document extraction

Turns the documents in a Unity Catalog volume into typed Delta tables: one table per document type, one row per document, one column per field, each value with its confidence score and a citation back to the page it came from. Values the model is unsure of land in a review queue, and optionally in a review app where people approve or correct them and the tables pick up their decisions. With labeled documents, the skill measures accuracy before anyone relies on the tables.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
/Volumes/<catalog>/<schema>/<volume>/   Lakeflow job <name>-extract (DAB, file-arrival trigger)
  *.pdf *.docx *.png ──► parse.py ────► <prefix>_parsed     ai_parse_document v2, one row per document
                                                            (500-page slices; split pages assembled)
                         extract.py ──► <prefix>_segments   whole documents, or packet pages classified
                                                            with ai_classify v2.1 and grouped
                                    ──► <prefix>_extracted  ai_extract v2.1 per type: citations, confidence
                                    ──► <prefix>_fields     one row per value ──► <prefix>_review (view)
                                    ──► <prefix>_<type>     typed tables (+ <prefix>_<type>__<array>), CDF on
                         localize.py ─► <prefix>_localized  optional: OCR word boxes, classic job cluster
<prefix>_fields ──► confidence_profile.py ──► review_cutoffs per type, in extraction.json (05)
labeled JSON Lines ──► evaluate.py ──► MLflow run: field accuracy, array soft F1, calibration (05)

Optional review app (07): <prefix>_review_candidates ══ Lakebase synced table ══► AppKit app ◄── reviewers
  app's Postgres decisions ══ Lakehouse Sync ══► review_sync job ──► <prefix>_decisions ──► typed tables
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `extraction.json` and the `<name>-extract/` bundle live. Default: the current directory; in Genie Code, the folder of the notebook it's open in.
- `<workdir>/extraction.json` holds every resolved value, including the document types and their schemas. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<prefix>`.
- `runtime`, in that file, is where the skill runs, recorded once in module 00: `local`, a coding assistant on the user's computer, using the CLI profile; or `workspace`, Genie Code in a notebook, signed in as the notebook's user. The commands in the modules are the local ones. With `workspace`, run each one's equivalent from [In a notebook](#in-a-notebook).
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.
- The job's tasks (`parse.py`, `extract.py`, `localize.py`) are Databricks notebooks, so users can open and change them in the workspace. To keep such a change, see module 01, Editing the job's notebooks.

### In a notebook

With `runtime: workspace`, Genie Code runs every step in Python cells of the notebook it's open in, on serverless compute, as the notebook's user. There's no CLI profile, `databricks` CLI, `uv`, Node.js, or `psql`. `<workdir>` is that notebook's folder, `/Workspace` plus the folder of `dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()` (serverless doesn't guarantee the working directory). `<skill>` is this skill's folder under `/Workspace/Users/<user>/.assistant/skills/` or `/Workspace/.assistant/skills/`.

Once per session, install the scripts' packages in a cell of their own:

```python
%pip install -q "databricks-sdk>=0.68" "pyyaml>=6" "mlflow[databricks]>=3.1" "numpy>=1.26" "scipy>=1.11"
dbutils.library.restartPython()
```

Then define `forge_run`, which runs a script in the notebook's own Python process, the only one signed in as the notebook's user (a subprocess isn't):

```python
import runpy
import sys


def forge_run(script, *args):
    """Run a Forge skill script here, as `uv run <script> <args>` does locally. Returns its exit status."""
    argv, sys.argv = sys.argv, [script, *map(str, args)]
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return e.code or 0
    finally:
        sys.argv = argv
    return 0
```

| Locally | In a notebook, with `w = WorkspaceClient()` (`from databricks.sdk import WorkspaceClient`) |
|---|---|
| `uv run <skill>/scripts/<file>.py <args>` | `forge_run("<skill>/scripts/<file>.py", <args>)`. A non-zero status fails the step, as locally |
| `databricks --version`, `uv --version`, `node --version`, `psql --version`, `databricks auth login` | Not needed |
| `databricks aitools update` | The user runs the `install_genie_code` notebook again |
| `databricks volumes read <catalog>.<schema>.<volume>` | `w.api_client.do("GET", "/api/2.1/unity-catalog/volumes/<catalog>.<schema>.<volume>")` |
| `databricks schemas list <catalog>` | `w.api_client.do("GET", "/api/2.1/unity-catalog/schemas", query={"catalog_name": "<catalog>"})` |
| `databricks schemas create <schema> <catalog>` (after its confirmation) | `w.api_client.do("POST", "/api/2.1/unity-catalog/schemas", body={"name": "<schema>", "catalog_name": "<catalog>"})` |
| `databricks grants get-effective schema <catalog>.<schema>` (or `catalog <catalog>`) | `w.api_client.do("GET", "/api/2.1/unity-catalog/effective-permissions/schema/<catalog>.<schema>")` (or `catalog/<catalog>`) |
| `get-default-warehouse`, `databricks warehouses list` | `w.api_client.do("GET", "/api/2.0/sql/warehouses")`, and propose a serverless one |
| `databricks experimental aitools tools query`, and SQL in the modules | A SQL cell, or `spark.sql(...)` |
| `databricks clusters spark-versions`, `list-node-types` | `w.api_client.do("GET", "/api/2.1/clusters/spark-versions")`, `w.api_client.do("GET", "/api/2.1/clusters/list-node-types")` |
| `databricks bundle validate`, `deploy`, `run`, `summary` | `job.py` (modules 01, 03, 06, and 07) |
| `databricks workspace export <path> --format SOURCE --file <file>` | `w.api_client.do("GET", "/api/2.0/workspace/export", query={"path": "<path>", "format": "SOURCE"})["content"]`, base64-decoded and written to `<file>` |
| `databricks postgres create-project <id> --json '{"spec": ...}'` | `w.api_client.do("POST", "/api/2.0/postgres/projects", query={"project_id": "<id>"}, body={"spec": ...})`. It answers with an operation: `w.api_client.do("GET", "/api/2.0/postgres/<operation name>")` until it's `done` |
| `databricks postgres list-branches <project>`, `list-databases <branch>` | `w.api_client.do("GET", "/api/2.0/postgres/<project>/branches")`, `w.api_client.do("GET", "/api/2.0/postgres/<branch>/databases")` |
| `databricks postgres create-synced-table <table> --json '{"spec": ...}'` | `w.api_client.do("POST", "/api/2.0/postgres/synced_tables", query={"synced_table_id": "<table>"}, body={"spec": ...})` |
| `databricks postgres get-synced-table synced_tables/<table>` | `w.api_client.do("GET", "/api/2.0/postgres/synced_tables/<table>")` |
| `databricks postgres create-cdf-config <database> <catalog> <schema> <postgres schema> --cdf-config-id <id>` | `w.api_client.do("POST", "/api/2.0/postgres/<database>/cdf-configs", query={"cdf_config_id": "<id>"}, body={"catalog": "<catalog>", "schema": "<schema>", "postgres_schema": "<postgres schema>"})` |
| `databricks postgres list-cdf-statuses <database>/cdf-configs/<id>` | `w.api_client.do("GET", "/api/2.0/postgres/<database>/cdf-configs/<id>/cdf-statuses")` |
| `databricks apps init`, `pnpm install`, `apps validate`, `apps deploy` | `render.py` generates the app, and the user deploys it from the workspace, following its `README.md` (module 07, step 4) |
| `databricks apps get <app_name>` | `w.api_client.do("GET", "/api/2.0/apps/<app_name>")` |
| `databricks psql --project <project> -- ... -c '<SQL>'` | The user runs the SQL in the Lakebase project's SQL editor, as its owner (the app's `README.md`, step 3) |
| `databricks apps update-permissions <app_name> --json '<json>'` (after its confirmation) | `w.api_client.do("PATCH", "/api/2.0/permissions/apps/<app_name>", body=<json>)` |

Everything else is the same in both: the steps, the checks, the go-aheads, and Verify.

## Inputs

Collect these together in one question during module 00. `volume_path` is required, and the user must answer `review_app` yes or no. Propose the rest from a look at the files.

| Input | Default | Notes |
|---|---|---|
| `volume_path` | — (required) | `/Volumes/<catalog>/<schema>/<volume>[/subdir]` |
| `profile` | from `databricks-core` | CLI profile for every command. Locally only: in a notebook, the notebook's user |
| `target_schema` | the volume's `<catalog>.<schema>` | Where the tables are created. If the user can't create tables there, module 00 proposes one they can |
| `name` | derived from the volume name | Lowercase letters, digits, hyphens, ≤40 chars; the bundle is `<name>-extract` |
| `layout` | proposed in module 00 | `single_type`, `one_per_file`, or `packet` (see Decision points) |
| `document_types` | proposed in modules 02–03 | Per type: `description`, `schema`, optional `instructions`, `pages`, `mode` |
| `provided_schema` | none: module 03 builds the schema with the user | A schema the user already has, in any format, saved in `<workdir>`; module 03 converts it |
| `split_pages` | off | Each folder holds the single-page files of one document |
| `review_threshold` | 0.8 | The floor: values with lower confidence go to the review queue |
| `parse_cutoffs` | none | Per element type, from module 05's parse report. When set, poorly parsed pages appear in `<prefix>_review` |
| `review_cutoffs` | none until module 05 | Per document type, the calibrated level that raises the floor; values below `max(review_threshold, review_cutoffs[type])` are reviewed |
| `ground_truth` | none | JSON Lines of labeled documents, for module 05 |
| `localize` | off | Word-level boxes for each value (module 06); runs a classic job cluster |
| `review_app` | — (asked: yes or no) | Yes: module 07 deploys an app where the people the user names approve or correct the queue's values, synced back to the tables. No: no app is deployed |

## Guardrails

- **Confirm before billable or shared actions:** parsing (priced per page), classification and extraction (priced per token, for every document), re-extraction with `full_refresh`, and the OCR job cluster. For the review app, that also means the Lakebase project, the synced table, Lakehouse Sync, deploying the app, and every grant. Show what will run and wait for an explicit go-ahead.
- **Never delete** tables, views, or volumes without separate confirmation that names the exact resource.
- **Don't grant access** to the tables, the review view, or the review app unless the user names the users or groups.
- **Treat extracted values as sensitive.** Documents like these often hold personal data (names, SSNs, account numbers). In reports, show counts and field names, plus only the values the user asks to see.
- **Never ask for or store tokens.** Use the CLI profile locally, and the notebook's own sign-in in a notebook. `extraction.json` holds names and settings only.
- **Don't build or suggest Agent Bricks Information Extraction.** It is being deprecated. Build the pipeline this skill describes.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `extraction.json` when its Verify passes:

- [ ] 00 Preflight: inputs, layout, and the review app answer resolved, base skills present, cost approved, `extraction.json` written
- [ ] 01 Ingest and parse: `<prefix>_parsed` has one row per document
- [ ] 02 Classify: labels designed; every segment typed or flagged `other`
- [ ] 03 Extract: schemas designed; `<prefix>_extracted` has a row per typed segment, without errors
- [ ] 04 Tables and review: typed tables populated, review queue reported
- [ ] 05 Evaluate: confidence profiled and `review_cutoffs` recorded; thresholds met on labeled documents, or accuracy reported as unmeasured
- [ ] 06 Localize (only when `localize` is on): hit rate ≥ 0.8
- [ ] 07 Review app (only if the user said yes in 00): an approval and a correction made in the app reach the typed table

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `extraction.json`, tooling, cost go-ahead | databricks-core, databricks-unity-catalog |
| 01 | [Ingest and parse](modules/01-ingest-parse.md) | `<name>-extract/` bundle, `<prefix>_parsed` | databricks-ai-functions, databricks-unity-catalog, databricks-dabs, databricks-jobs, databricks-serverless |
| 02 | [Classify](modules/02-classify.md) | labels, `<prefix>_segments` (+ `<prefix>_pages` for packets) | databricks-ai-functions |
| 03 | [Extract](modules/03-extract.md) | schemas, `<prefix>_extracted`, `<prefix>_fields` | databricks-ai-functions |
| 04 | [Tables and review](modules/04-tables-review.md) | `<prefix>_<type>` tables, `<prefix>_review` | databricks-unity-catalog |
| 05 | [Evaluate](modules/05-evaluate.md) | `review_cutoffs`, MLflow run, pass/fail against thresholds | databricks-mlflow-evaluation |
| 06 | [Localize](modules/06-localize.md) | `<prefix>_localized` word boxes | databricks-jobs, databricks-serverless-migration |
| 07 | [Review app](modules/07-review-app.md) | review queue in Lakebase, the app, `review_sync` job, `<prefix>_decisions` | databricks-lakebase, databricks-apps |

The working directory ends up with the state file, the bundle, and (for module 05) the labels:

```
<workdir>/
├── README.md            # the project explained for whoever opens the folder; write_readme.py writes it
├── extraction.json      # resolved values and document types; render.py reads it
├── provided_schema.*    # optional: the schema the user brought, as given (module 00)
├── ground_truth.jsonl   # optional labels for module 05
├── <name>-extract/      # Declarative Automation Bundle: parse → extract (→ localize, review_queue) job, plus review_sync; rendered by render.py
└── <review.app_name>/   # optional review app (module 07): an AppKit project; render.py copies in the review files (in a notebook, it writes the whole project and a README.md on deploying it)
```

**The README.** Each time a module's Verify passes, module 00's included, update `<workdir>/README.md`:

```bash
uv run <skill>/scripts/write_readme.py --state <workdir>/extraction.json
```

It explains the project to whoever opens the folder, from this skill and every Forge state file in `<workdir>`: what it builds, its settings, and each module's purpose, outputs, and status. Its Notes section is the user's, and is kept. If the script reports `kept_edited`, someone edited the README outside Notes: tell the user, and pass `--force` only if they agree. A README.md that `<workdir>` already had is left alone, and the project's goes to `FORGE-README.md`.

**Resuming.** If `<workdir>/extraction.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 2). If it has no `review_app` (it's from before version 0.3.0), ask module 00's review app question. Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Add a document type, or change labels | 02, step 1 |
| Change a type's schema or instructions | 03, step 1 (re-extraction needs `full_refresh`, which re-bills) |
| Evaluate only, with new labels | 05 |
| Review queue empty, or flooded | 05, step 1 (recalibrate `review_cutoffs`) |
| Add word-level boxes later | 06 |
| Add the review app later | 07 |
| New documents in the volume | Nothing to do: the job processes them on arrival |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Layout | Proposed from a sample in module 00 | `single_type`: every file is one document of the same type (no classification). `one_per_file`: each file is one document, of several types. `packet`: one file bundles many documents, such as a loan or claim file; pages are classified and grouped |
| Pages per document (packets) | `single`: each page is its own document | A type's documents run over several pages (statements, tax returns, appraisals). Then set `"pages": "multi"` for it, and consecutive pages of that type become one document |
| Split pages | Off | Files are single pages split upstream, one folder per document. Then set `split_pages` to `true` |
| AI function input | `variant`: the parsed document | `ai_extract` or `ai_classify` rejects the segment VARIANT. Then set `extract_input` to `text`. Citations become character spans, so module 06 can't run |
| Schema shape | Scalars and objects | Documents have repeating rows (line items, transactions). Then use an array of objects, which becomes a child table |
| Extraction mode | Default | A type has long documents or many fields, and module 05 shows misses. Then set `"mode": "precision"` for that type |
| Review threshold | 0.8, raised per type by `review_cutoffs` from module 05 | Raise `review_threshold` to send more values to people. Lower it only when module 05's calibration shows that values just under it are reliably correct. Recalibrate `review_cutoffs` after a schema change or a large new batch of a different kind of document |
| Word-level boxes | Off | Reviewers need to see the exact words behind each value (highlights in a review UI). Then turn on `localize` (module 06) |
| Review app | Asked in module 00 | Yes: module 07 deploys the app after the pipeline, so people approve or correct the review queue's values and the tables carry their decisions. In a notebook, it generates the app's code instead, and the user deploys it from the workspace with the `README.md` it writes. No: nothing for it is created, and the `<prefix>_review` view still lists the values |
| Trigger | File arrival on the volume | Large batches land on a schedule. Then set `schedule_cron` (Quartz syntax) in `extraction.json` and render again |

## Done when

- `<prefix>_parsed` has one row per document, and you reported files that didn't parse.
- Every segment has a document type, or is in `<prefix>_review` as `unclassified`.
- Each type's table is populated, and you reported the review queue counts by reason, including `unscored`.
- Module 05 reported the confidence profile, and the user approved the `review_cutoffs` recorded in `extraction.json`.
- With labels, module 05 meets its thresholds: classification accuracy ≥ 0.95 (when labels include `doc_type`), field accuracy ≥ 0.9, and array soft F1 ≥ 0.8 (when arrays are labeled). Report the run URL. Without labels, say that accuracy is unmeasured.
- When `localize` is on, at least 80% of cited values are localized.
- When `review_app` is on, a value approved and a value corrected in the app are in the typed table, with confidence 1.0 and listed in `reviewed_fields`, and only the named reviewers can open the app.
- `<workdir>/README.md` describes the finished project: `write_readme.py` ran after the last module.
- You told the user the job name, the tables, the review view, and anything you skipped. If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **`ai_classify` version:** `databricks-ai-functions` recommends v2.0, with a 128K-token limit and plain-string labels. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_classify) recommend v2.1: it accepts the parsed VARIANT, has a 1M-token context, and returns `response[0].value` with an optional `confidence_score`. Use `map('version', '2.1', ...)`.
- **`ai_extract` citations:** bounding-box citations need VARIANT input from `ai_parse_document`; text input gets character spans instead ([docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_extract)). The base skill's citation example uses text.
- **Page limit:** `databricks-ai-functions` says documents are limited to 500 pages. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document) apply the limit to the pages one call selects with `pageRange`; `parse.py` parses longer PDFs in 500-page slices and stitches them.
- **Filtering by confidence:** `databricks-ai-functions` suggests filtering `ai_extract` values by `confidence_score`. Scores often come in a few levels (such as 0.95, 0.98, 1.0), so a fixed cutoff can flag almost nothing, and a value can come back without a score. Use module 05's per-type `review_cutoffs`, and review unscored values; never drop them.
- **Agent Bricks Information Extraction:** being deprecated. Don't use it, even where the docs or older examples point to it.
- **Triggered synced tables:** `databricks-lakebase` doesn't say how a `TRIGGERED` synced table syncs. The [docs](https://docs.databricks.com/aws/en/oltp/projects/sync-tables): it syncs only when its pipeline runs, for example as a job's "Database Table Sync pipeline" task. The job's `review_queue` task does this.
- **Lakehouse Sync:** `databricks-lakebase` says the tables must be in the `databricks_postgres` database. The [docs](https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync) allow any one database per feed. They also say empty tables aren't synced, so the history table appears after the first decision.
