# Document search index (`forge-unstructured-doc-extraction`)

Turns the documents in a Unity Catalog volume into an AI Search (formerly Vector Search) index that an agent, an app, or a person can query. It parses every supported file with `ai_parse_document`, splits it into chunks with `ai_prep_search`, and serves them from a Delta Sync index with managed embeddings. Each chunk carries document-level metadata to filter on. Every search result names its file and pages, so whatever uses the index can cite its sources. Before anything relies on the index, the skill measures how often a search finds the right document and page.

## Purpose

- **This Forge Skill is used to:**
  - build a search or retrieval index over documents: RAG retrieval, a knowledge base, or chunks and embeddings for another agent or app;
  - keep the index current as documents arrive, and measure how often a search finds the right document and page.
- **Not for** an agent that answers the questions: `forge-doc-qa-agent` builds that, and runs this skill first when it needs an index. **Not for** field values in tables: that's `forge-structured-doc-extraction`.
- **The documents it reads:**
  - formats: PDF, Word (DOC, DOCX), PowerPoint (PPT, PPTX), and images (JPG, PNG, TIFF);
  - size: up to 100 MB a file;
  - long PDFs: those over 500 pages are parsed in 500-page slices and stitched back together;
  - split scans: with `split_pages`, a scan split into one file per page is put back together as one document.

## How to Run

The user starts it with `/forge` (`@forge` in Genie Code) and says what they want, or names the skill. The coding assistant works through it step by step, and asks before anything that costs money.

> /forge Parse the PDFs in /Volumes/main/legal/contracts and build a search index our app can query.

Its progress is saved in `doc-index.json`. The user can run `/forge` in the same folder later to pick up where it stopped, or to change something, such as the metadata filters, chart readings, or how the index stays current. For a Q&A agent on the index, ask `/forge` for one in the same folder.

## Inputs

**Prerequisites:**
- **Data and workspace:** a Unity Catalog volume holding the documents, in a workspace with serverless compute and AI Search.
- **Tools:**
  - the Databricks CLI (v1.11.0 or later), signed in to the workspace;
  - `uv`;
  - the Databricks Agent Skills (v0.2.27 or later), installed next to the Forge skills.

  The Forge installer sets these up.
- **Privileges:**
  - `READ VOLUME` on the volume;
  - `USE SCHEMA` and `CREATE TABLE` on the target schema, plus `CREATE VOLUME` for page images;
  - permission to create jobs;
  - `CAN USE` on an AI Search endpoint, or permission to create one.

  The skill checks these first and reports what's missing.

**What it asks the user.** It asks for the four required inputs before anything else, and proposes defaults for the rest.

| Input | Required | Default | What it is |
|---|---|---|---|
| `volume_path` | Yes | — | Where the documents are: `/Volumes/<catalog>/<schema>/<volume>`, or a folder in it |
| `vs_endpoint` | Yes | — | The AI Search endpoint for the index: one the user can use, or none, to have `<name>-vs` created if the user is allowed to create endpoints |
| `index_name` | Yes | `<prefix>_chunks_index` (proposed) | The index's name. It's created in the target schema |
| `continuous_updates` | Yes | — | Whether the index stays current as files arrive (the job runs on file arrival), or is built once and rerun by hand |
| `profile` | No | the user's CLI profile | The CLI profile every command uses |
| `target_schema` | No | the volume's catalog and schema | Where the tables and the index are created |
| `name` | No | from the volume's name | Names the job's bundle, `<name>-index`. Keep it to 17 characters if `forge-doc-qa-agent` will build on it |
| `metadata_schema` | No | none | Document-level fields to filter searches on, such as `company`, `document_type`, or `fiscal_year`. The skill proposes some from the file names |
| `embedding_model` | No | `databricks-qwen3-embedding-0-6b`, else `databricks-gte-large-en` | The embedding model. Changing it later means rebuilding the index |
| `page_images` | No | off | Save rendered page images. Chart readings need them for Word, PowerPoint, and split pages |
| `chart_insights` | No | off | Read each chart's values into the index, so questions about chart values can be answered |
| `chart_source` | No | `vision` | How charts are read: by a vision model (`vision`), or from the parser's chart data, with no model calls (`builtin`) |
| `chart_model` | No | Claude Sonnet 5.5 (`system.ai.claude-sonnet-5-5`) | The vision model, with `chart_insights` |
| `split_pages` | No | off | Each folder holds the single-page files of one document |
| `eval_set` | No | written from the documents | The user's own test questions (JSON Lines with `question`, `file_name`, and optional `pages`), or synthetic ones the skill writes |

## Outputs

**In the workspace:**
- **Parsed documents:** `<catalog>.<schema>.<prefix>_parsed`, one row per document. Each document is parsed once, and again only when its file changes.
- **Chunks:** `<catalog>.<schema>.<prefix>_chunks`, the search chunks with their metadata, with Change Data Feed on.
- **The index:** `<catalog>.<schema>.<index_name>` on the AI Search endpoint, with hybrid search and metadata filters.
- **The job:** `<name>-index`, a Lakeflow job in a Declarative Automation Bundle that parses, chunks, and syncs the index.
  - With continuous updates, it runs whenever files arrive.
  - Each run processes only new or changed files.
  - Its tasks are notebooks the user can open and edit in the workspace.
- **The evaluation:** an MLflow run with the retrieval scores and every question's results.

**In the working directory:**

```
<workdir>/
├── README.md         # what was built, its settings, and each step's status
├── doc-index.json    # the settings; forge-doc-qa-agent reads it too
├── evals.jsonl       # the test questions
└── <name>-index/     # the job's bundle
```

**When it's done.** At 8 results per search:
- the right document is found for at least 90% of the questions (document hit rate ≥ 0.9);
- the right page is found for at least 80% of those with known pages (page hit rate ≥ 0.8).

## How It Works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Asks the four questions, proposes the rest, checks the tools, the volume and its files, the models, the endpoint, and the user's privileges, and gets the user's go-ahead on cost |
| 01 | Ingest and parse | Deploys the job, and parses every supported file once |
| 02 | Chunk and index | Designs the metadata filters with the user, optionally reads charts, chunks the documents, and builds or syncs the index |
| 03 | Evaluate retrieval | Runs the user's test questions, or synthetic ones, against the index, and scores how often the right document and page come back |

Each step ends with a check that must pass before the next one starts.

### Preflight Checks and Guardrails

It asks before each of these:
- parsing and chunking, billed per page and token;
- reading charts;
- creating an AI Search endpoint, billed for as long as it exists;
- reprocessing everything with `full_refresh`;
- writing synthetic test questions.

It never deletes a table, index, volume, or endpoint without a separate confirmation that names it. It grants access to the tables or the index only to the people the user names.
