# Structured extraction (`forge-structured-doc-extraction`)

Turns the documents in a Unity Catalog volume into typed Delta tables: one table per document type, one row per document, and one column per field. Each value comes with a confidence score and a citation back to the page it came from.
- **Mixed documents:** it classifies them with `ai_classify`, and splits multi-document packets, such as loan or claim files, into their documents.
- **Extraction:** it extracts each type's fields with `ai_extract`, using a schema you bring or design with it.
- **Review:** values the model is unsure of go to a review queue. An optional review app lets the people you name approve or correct them, and their decisions flow back into the tables.
- **Accuracy:** with labeled documents, it measures accuracy before anyone relies on the tables.

It's a Forge use case. Start it with `/forge` (`@forge` in Genie Code) and say what you want, or name the skill. Your coding assistant works through it step by step, and asks before anything that costs money.

## What it's for

- **Choose it for** field values, entities, or line items from documents, in tables:
  - classifying documents, or splitting document packets;
  - a review queue for uncertain values, or an app where people approve or correct them;
  - extraction accuracy.
- **Not for** answering questions about documents: use `forge-doc-qa-agent`. **Not for** a search index: use `forge-unstructured-doc-extraction`.
- **The documents it reads:** the same formats and limits as the search index skill: PDF, Word, PowerPoint, and images, up to 100 MB a file. PDFs over 500 pages are parsed in slices, and with `split_pages`, page-per-file scans are put back together.

## What you provide

**Before you start:**
- **Data and workspace:** a Unity Catalog volume holding the documents, in a workspace with serverless compute.
- **Tools:**
  - the Databricks CLI (v1.11.0 or later), signed in to the workspace;
  - `uv`;
  - the Databricks Agent Skills (v0.2.27 or later), installed next to the Forge skills.

  The Forge installer sets these up.
- **Privileges:**
  - `READ VOLUME` on the volume;
  - `USE SCHEMA` and `CREATE TABLE` on the target schema, plus `CREATE VOLUME` for page images (word-level boxes);
  - permission to create jobs;
  - for word-level boxes, a classic job cluster;
  - for the review app, permission to create a Lakebase project and Databricks Apps.

  The skill checks these first.
- **Optional:** the schema you already have, in any format, and labeled documents for measuring accuracy.

**What it asks you.** It needs the volume and a yes or no on the review app. It proposes the rest from a look at your files, and designs the document types and schemas with you.

| Input | Required | Default | What it is |
|---|---|---|---|
| `volume_path` | Yes | — | Where the documents are: `/Volumes/<catalog>/<schema>/<volume>`, or a folder in it |
| `review_app` | Yes | — | Whether to deploy the review app, where the people you name approve or correct uncertain values |
| `profile` | No | your CLI profile | The CLI profile every command uses |
| `target_schema` | No | the volume's catalog and schema | Where the tables are created |
| `name` | No | from the volume's name | Names the job's bundle, `<name>-extract` |
| `layout` | No | proposed from a sample | `single_type` (every file is one document of one type), `one_per_file` (one document per file, of several types), or `packet` (files that bundle many documents) |
| `document_types` | No | designed with you | Each type's description and schema (the fields to extract), with optional instructions, page grouping, and mode |
| `provided_schema` | No | none | A schema you already have (JSON, Python, SQL, or a description), which the skill converts |
| `split_pages` | No | off | Each folder holds the single-page files of one document |
| `review_threshold` | No | 0.8 | Values with lower confidence go to the review queue |
| `review_cutoffs` | No | set after the confidence profile | Per document type, a calibrated level that raises the review threshold |
| `parse_cutoffs` | No | none | Per element type, from the parse quality report: poorly parsed pages go to the review queue too |
| `ground_truth` | No | none | Labeled documents (JSON Lines), for measuring accuracy |
| `localize` | No | off | Find the exact words behind each value (word-level boxes), for highlighting in a review UI. Runs on a classic job cluster |

## What you get

**In your workspace**, in the target schema:
- **Parsed and classified documents:** `<prefix>_parsed`, one row per parsed document, and `<prefix>_segments`, each document or packet sub-document with its type (plus `<prefix>_pages` for packets).
- **Extracted values:** `<prefix>_extracted` and `<prefix>_fields`, every value the model extracted, with its confidence and citation.
- **Typed tables:**
  - `<prefix>_<type>`: one table per document type, with a typed column per field and a confidence column next to each;
  - `<prefix>_<type>__<field>`: a child table for each repeating field, such as line items.
- **The review queue:** the view `<prefix>_review`, listing what a person should check: low-confidence values, unclassified segments, and, with `parse_cutoffs`, poorly parsed pages.
- **Word-level boxes** (with `localize`): `<prefix>_localized`.
- **The review app** (with `review_app`):
  - an AppKit app on Lakebase;
  - its queue, `<prefix>_review_candidates` and a synced table;
  - the `review_sync` job and `<prefix>_decisions`, which write each approval or correction back into the typed tables.
- **The job:** `<name>-extract`, a Lakeflow job in a Declarative Automation Bundle. It processes new files as they arrive, and each document only once. Its tasks are notebooks you can open and edit in the workspace.
- **The evaluation:** an MLflow run with accuracy per field and document, when you have labels.

**In your working directory:**

```
<workdir>/
├── README.md             # what was built, its settings, and each step's status
├── extraction.json       # the settings, including the document types and schemas
├── provided_schema.*     # optional: the schema you brought, as given
├── ground_truth.jsonl    # optional: your labeled documents
├── <name>-extract/       # the job's bundle
└── <review app>/         # optional: the review app's project
```

**When it's done:**
- Every document is parsed and typed.
- Each type's table is populated, and you've seen the review queue's counts.
- The confidence profile is reported, with review cutoffs you approved.
- With labels, it meets these thresholds:
  - classification accuracy ≥ 0.95, when labels include the document type;
  - field accuracy ≥ 0.9;
  - array soft F1 ≥ 0.8, when line items are labeled.

  Without labels, it reports that accuracy is unmeasured.
- With `localize`, at least 80% of cited values are found on the page.

## How it works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Resolves the inputs and the layout, asks about the review app, checks the tools and your privileges, and gets your go-ahead on cost |
| 01 | Ingest and parse | Deploys the job, and parses every document once |
| 02 | Classify | Designs the document types with you, and types every document, splitting packets |
| 03 | Extract | Converts your schema or builds one with you, then extracts every value with a confidence score and citation |
| 04 | Tables and review | Publishes the typed tables, and walks you through the review queue |
| 05 | Evaluate | Profiles confidence to set review cutoffs, and, with labels, measures accuracy in MLflow |
| 06 | Localize | Optional: finds each value's word-level box with OCR |
| 07 | Review app | Optional: deploys the app where reviewers approve or correct values, synced back to the tables |

Each step ends with a check that must pass before the next one starts.

## Before it acts

It asks before each of these:
- parsing, billed per page;
- classifying and extracting, billed per token for every document;
- re-extracting everything with `full_refresh`;
- starting the OCR job cluster;
- for the review app: the Lakebase project, the synced table, Lakehouse Sync, deploying the app, and every grant.

Extracted values are treated as sensitive: reports show counts and field names, and only the values you ask to see. It never deletes a table, view, or volume without a separate confirmation that names it, and grants access only to the people you name.

## Running it

> /forge Our loan files in /Volumes/main/lending/loan_files are single PDFs with many documents inside. Pull the borrower's income and bank balances into tables.

Its progress is saved in `extraction.json`. Run `/forge` in the same folder later to pick up where it stopped, add a document type, change a schema, evaluate with new labels, or add word-level boxes or the review app. New files in the volume are processed as they arrive.
