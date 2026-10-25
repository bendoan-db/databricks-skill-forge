---
name: forge-unstructured-doc-extraction
description: "Forge use case, normally started by the forge skill: parses the documents in a Unity Catalog volume (PDF, DOCX, PPTX, images) and serves them from an AI Search (Vector Search) index for retrieval by an agent or app, with incremental ai_parse_document parsing (including PDFs over 500 pages and scans split into one file per page), ai_prep_search chunks with document-level metadata filters, optional chart readings by a vision model, optional refresh on file arrival, and a retrieval evaluation (document and page hit rates) in MLflow. Use when forge hands off to it, when forge-doc-qa-agent needs an index, or when the user names this skill."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, and the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill
metadata:
  version: "0.1.0"
  forge_kind: use-case
---

# Unstructured document extraction into an AI Search index

Builds a search index from the documents in a Unity Catalog volume. Before building, it asks where the documents are, which AI Search endpoint to use, what to name the index, and whether the index must stay current as files arrive. Every supported file is parsed, split into chunks that carry document-level metadata, and served from the index. Searches return the file and pages of each passage, so whatever queries the index can cite its sources. The skill measures retrieval quality before anything relies on the index.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
/Volumes/<catalog>/<schema>/<volume>/  Lakeflow job <name>-index (DAB; file-arrival trigger if continuous_updates)
  *.pdf *.docx *.pptx *.png  ──►  parse.py ───────► <prefix>_parsed  (ai_parse_document v2, one row per document;
                                                                      500-page slices; split pages assembled)
                                  prep_chunks.py ─► <prefix>_chunks  (ai_prep_search: chunks + metadata, CDF on;
                                                                      optional chart readings in figure text)
                                  sync_index.py ──► <index_name>     (AI Search Delta Sync on <vs_endpoint>,
                                                                      managed embeddings)
                                                        │ hybrid search, metadata filters
evals.jsonl (labeled or synthetic) ──► evaluate.py ─────┴──► MLflow run: document and page hit rates (03)
Consumers: forge-doc-qa-agent, or any agent or app; doc-index.json describes the index to them
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `doc-index.json`, `evals.jsonl`, and the `<name>-index/` bundle live. Default: the current directory.
- `<workdir>/doc-index.json` holds every resolved value. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<prefix>`. `forge-doc-qa-agent` reads it to find the index, and never writes it.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.

## Inputs

Module 00 asks for the first four before anything else, and doesn't continue until the user answers them. Offer the defaults for the rest.

| Input | Default | Notes |
|---|---|---|
| `volume_path` | — (asked) | Where the documents are: `/Volumes/<catalog>/<schema>/<volume>[/subdir]` |
| `vs_endpoint` | — (asked) | An existing endpoint the user can use (`CAN USE`), or none: then `<name>-vs` is created, but only if the user can create endpoints. Otherwise they must name one |
| `index_name` | — (asked; propose `<prefix>_chunks_index`) | Letters, digits, `_`; created as `<catalog>.<schema>.<index_name>` |
| `continuous_updates` | — (asked) | Yes: the job runs on file arrival. No: one build, reruns by hand |
| `profile` | from `databricks-core` | CLI profile for every command |
| `target_schema` | the volume's `<catalog>.<schema>` | Where tables and the index are created |
| `name` | derived from the volume name | Lowercase letters, digits, hyphens, ≤40 chars; the bundle is `<name>-index`. ≤17 chars when `forge-doc-qa-agent` will reuse it |
| `metadata_schema` | none | Document-level fields used for filtering, such as `company`, `document_type`, `fiscal_year`. See module 02 |
| `embedding_model` | `databricks-qwen3-embedding-0-6b`, else `databricks-gte-large-en` | Resolved from the workspace |
| `page_images` | off | Saves rendered page images; required for `chart_insights` |
| `chart_insights` | off | Reads each chart with a vision model before chunking (module 02) |
| `chart_model` | Claude Sonnet 5.5: `system.ai.claude-sonnet-5-5` | Only with `chart_insights`. Confirmed in `databricks ai-gateway list-model-services` |
| `split_pages` | off | Each folder holds the single-page files of one document |
| `eval_set` | synthetic | JSONL with `question` and `file_name`, plus optional `pages` (module 03) |

## Guardrails

- **Ask for the four requirements first** (module 00, step 1): where the documents are, the endpoint, the index name, and whether updates must be continuous. Don't assume answers to them.
- **Confirm before billable or shared actions:** parsing and chunking (priced per page and token), chart reading, creating an AI Search endpoint (billed while it exists), re-chunking with `full_refresh`, and synthesizing eval questions. Show what will be created and wait for an explicit go-ahead.
- **Never delete** tables, volumes, indexes, or endpoints without separate confirmation that names the exact resource.
- **Don't grant access** to the tables or the index unless the user names the users, groups, or service principals.
- **Never ask for or store tokens.** Use the CLI profile. `doc-index.json` holds names and settings only.
- **Don't build or suggest an Agent Bricks Knowledge Assistant.** It is being deprecated. Build the index this skill describes; `forge-doc-qa-agent` builds the Q&A agent.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `doc-index.json` when its Verify passes:

- [ ] 00 Preflight: the four requirements answered, endpoint usable or creatable, inputs resolved, base skills present, cost approved, `doc-index.json` written
- [ ] 01 Ingest and parse: `<prefix>_parsed` has one row per supported file
- [ ] 02 Chunk and index: index row count equals `<prefix>_chunks` row count; `filter_values` and `corpus_description` recorded
- [ ] 03 Evaluate retrieval: thresholds met, run ID reported

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `doc-index.json`, tooling, cost go-ahead | databricks-core, databricks-unity-gateway, databricks-vector-search, databricks-unity-catalog |
| 01 | [Ingest and parse](modules/01-ingest-parse.md) | `<name>-index/` bundle, `<prefix>_parsed` | databricks-ai-functions, databricks-unity-catalog, databricks-dabs, databricks-jobs, databricks-serverless |
| 02 | [Chunk and index](modules/02-chunk-index.md) | `<prefix>_chunks`, the `<index_name>` index | databricks-ai-functions, databricks-vector-search |
| 03 | [Evaluate retrieval](modules/03-evaluate.md) | `evals.jsonl`, MLflow run, pass/fail against thresholds | databricks-vector-search, databricks-mlflow-evaluation |

The working directory ends up with the state file, the eval set, and the bundle:

```
<workdir>/
├── doc-index.json   # resolved values; render.py reads it, and so does forge-doc-qa-agent
├── evals.jsonl      # retrieval eval set (module 03); forge-doc-qa-agent reuses it
└── <name>-index/    # Declarative Automation Bundle: parse → chunk → sync_index job (rendered by render.py)
```

**Resuming.** If `<workdir>/doc-index.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 3). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Change the metadata schema | 02, step 1 |
| Turn on chart insights | 02, step 3. It needs page images from parsing (01, step 1), and already-chunked documents need a `full_refresh=true` run |
| Re-run the retrieval evaluation | 03 |
| New documents in the volume | With continuous updates, nothing to do. Without, rerun the job (module 01, step 5) after the user confirms; it bills only new or changed files |
| Turn continuous updates on or off | Set `continuous_updates` in `doc-index.json`, then 01, steps 3–4 (render and deploy) |
| Build a Q&A agent on the index | Run `forge-doc-qa-agent` in the same `<workdir>`; it reads `doc-index.json` |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Separate indexes per domain | One index, with a `document_type` metadata filter | Domains need different access, or separate agents (for example claims vs. underwriting). Then run the skill once per domain, each in its own `<workdir>`, with its own volume folder, `prefix`, and `name` |
| Metadata filters | Off unless `metadata_schema` is given | The corpus spans entities or periods users name in questions, or module 03 shows similar documents winning. Then turn them on |
| Chart-heavy documents | The figure descriptions `ai_parse_document` writes | Questions about chart values miss in module 03 (or fail in `forge-doc-qa-agent`'s evaluation). Then turn on `chart_insights` (module 02, step 3) |
| Endpoint type | STANDARD (low latency, dict filters, `columns_to_sync`) | More than about 50M chunks, or latency isn't critical. Then use STORAGE_OPTIMIZED (filters become SQL strings) |
| Update trigger | Asked in module 00: file arrival (continuous updates) or none | Large batches land on a schedule. Then keep `continuous_updates` true, set `schedule_cron` (Quartz syntax) in `doc-index.json`, and rerun `render.py` |
| Split pages | Off | Files are single pages split upstream, one folder per document. Then set `split_pages` to `true` before the first parse |

## Done when

- The index `<catalog>.<schema>.<index_name>` row count equals `<prefix>_chunks` row count.
- Module 03 meets its thresholds: document hit rate ≥ 0.9 and page hit rate ≥ 0.8 (when questions have pages), at k = 8. Report the run URL.
- `doc-index.json` has `filter_values` (when there's metadata) and `corpus_description`.
- You told the user the job name and whether it runs on file arrival, the index name and its filter columns, the endpoint, and anything you skipped. If `forge-doc-qa-agent` sent you here, return to its module 00. Otherwise, if `forge` started this skill, return to it for a next step; if not, mention that `forge-doc-qa-agent` can build a cited Q&A agent on the index.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Knowledge Assistant:** `databricks-agent-bricks` offers Knowledge Assistants for document Q&A over a volume. Knowledge Assistant is being deprecated, so don't build or suggest one.
- **Endpoint permissions:** `databricks-vector-search` attributes permission errors on create to Unity Catalog privileges. Those govern the index (`CREATE TABLE` on the schema). Endpoints have their own access-control list: `CAN CREATE`, `CAN USE` (create indexes on it), and `CAN MANAGE` ([docs](https://docs.databricks.com/aws/en/security/auth/access-control/)). Module 00, step 6 checks them.
- **AI Search** is the new name for Vector Search. `databricks-vector-search`, the CLI (`vector-search-*`), and the SDK still use the old name for the same service.
- **`ai_prep_search` metadata:** `databricks-ai-functions` lists only the `version` option. The `schema` option that module 02 uses is [documented](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_prep_search).
- **Embeddings:** base-skill examples use `databricks-gte-large-en`. Prefer `databricks-qwen3-embedding-0-6b` ([docs](https://docs.databricks.com/aws/en/ai-search/create-ai-search)).
- **Page limit:** `databricks-ai-functions` says documents are limited to 500 pages. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document) apply the limit to the pages one call selects with `pageRange`; `parse.py` parses longer PDFs in 500-page slices and stitches them.
- **`ai_classify` version** (chart insights only): use version 2.1, which returns `response[0].value`. `databricks-ai-functions` recommends 2.0 ([docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_classify)).
