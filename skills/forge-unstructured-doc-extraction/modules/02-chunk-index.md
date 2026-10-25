# 02 · Chunk and index

Turn parsed documents into retrieval-ready chunks with document-level metadata, and serve them from an AI Search (formerly Vector Search) Delta Sync index.

## Inputs → outputs

- **Inputs:** `<prefix>_parsed` (module 01), plus `metadata_schema`, `vs_endpoint`, and `embedding_model` from `doc-index.json`.
- **Outputs:** `<prefix>_chunks` (CDF on, one row per chunk) and the index `<catalog>.<schema>.<index_name>`, plus `filter_values` and `corpus_description` in `doc-index.json`, which tell consumers such as `forge-doc-qa-agent` what the index holds.

## 1. Design the metadata schema

**Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_prep_search` section. For the `schema` option, follow the docs (see SKILL.md, Base-skill guidance to override).

Metadata becomes index columns that searches can filter on. For example, "Netflix's 2017 liabilities" becomes `company = NETFLIX AND fiscal_year = 2017`. Good fields:
- Are document-level, not chunk-level.
- Are low-cardinality or ordered (entity, type, year, region, product).
- Are named in questions users actually ask.

Rules that `prep_chunks.py` and `render.py` enforce:
- Keys are snake_case.
- Types are limited to string, enum, integer, number, and boolean. Nested objects and arrays can't be index filters.
- Keys can't reuse the base column names.

Keep it to 2–5 keys. Show the proposed schema to the user before running.

```json
{"company": {"type": "string", "description": "Issuer's common name in upper case, no punctuation, e.g. 'JOHNSON & JOHNSON' -> 'JOHNSONJOHNSON'"},
 "document_type": {"type": "enum", "labels": ["10K", "10Q", "8K", "EARNINGS"], "description": "Filing type"},
 "fiscal_year": {"type": "integer", "description": "Fiscal year the document covers"}}
```

Descriptions steer extraction, so spell out the normalization you want, such as casing. Without a schema, `ai_prep_search` still discovers fields per document and adds them to `chunk_to_embed`, but there are no filter columns.

Record the schema as `metadata_schema` in `doc-index.json` (the JSON object above, or `null` for no metadata), then run `render.py` again (module 01, step 3). It derives the index's metadata columns from the keys.

## 2. Create the endpoint, if the user approved a new one

```bash
databricks vector-search-endpoints create-endpoint <vs_endpoint> STANDARD --profile <profile>
databricks vector-search-endpoints get-endpoint <vs_endpoint> --profile <profile>   # wait for ONLINE
```

If creation fails with `PERMISSION_DENIED`, the user can't create endpoints after all. Stop, and ask them to specify an existing endpoint they can use (module 00, step 6). Don't retry the creation.

Use STANDARD unless you hit a STORAGE_OPTIMIZED trigger from the SKILL.md decision table. STORAGE_OPTIMIZED doesn't support `columns_to_sync`, and it takes SQL-string filters instead of the dict filters that `forge-doc-qa-agent`'s tool sends. If you choose it, **load** `databricks-vector-search` `references/index-types.md`, drop `columns_to_sync` in `sync_index.py`, and verify that LLM-generated filters work before you go further.

## 3. Chart insights (only when `chart_insights` is on)

**Load:** `databricks-ai-functions` `references/2-ai-query.md` (multimodal `ai_query` with `files =>`). Use `system.ai.*` model names.

`ai_parse_document` already describes each figure (`description_element_types`), and those descriptions reach the chunks. For decks and reports where answers live in chart values, a vision model reads each chart in full first:
1. `ai_classify` (version 2.1) sorts each described figure into chart or other, from its description.
2. Each chart is cropped from its page image and read by `ai_query(<chart_model>, <chart_prompt>, files => image)`.
3. The reading is appended to the figure's description before `ai_prep_search`, so it is chunked with the page's text and cited by page. Readings are stored in `<prefix>_chart_insights`.

It needs `page_images_path` and `description_element_types` of `*` or `figure`; `render.py` checks both. `chart_model` defaults to Claude Sonnet 5.5 (`system.ai.claude-sonnet-5-5`; module 00). The default `chart_prompt` asks for the chart type, axes, every readable value as a table, and trends. For a domain, such as earnings decks, set your own `chart_prompt` in `doc-index.json`. Cost: one `ai_classify` per described figure and one vision call per chart, once per parse. Confirm before turning it on, then render again (module 01, step 3). Documents chunked before you turned it on keep their old chunks until a `full_refresh=true` run (step 4).

## 4. Run the job

```bash
cd <workdir>/<name>-index && databricks bundle deploy --profile <profile> && databricks bundle run doc_index --profile <profile>
```

- **`chunk`** (`prep_chunks.py`) runs `ai_prep_search(parsed, map('schema', ...))` once for each document parsed since it was last chunked; `<prefix>_progress` records which parse it processed. It explodes `document.contents` into rows and replaces that document's chunks (`scripts/prep_chunks.sql`).
  - `chunk_id` is `md5(source_uri#chunk_position)`: stable, unique across files, and the index primary key.
  - `chunk_to_embed` is the embedding source. It holds the title, headers, page, metadata, a document context sentence, and table summaries, and is truncated defensively to stay under the 32,764-byte Delta Sync limit.
  - `page_numbers` are 1-based, for citations.
- **`sync_index`** creates the index on the first run, using managed embeddings on `chunk_to_embed`, `TRIGGERED` sync, and `columns_to_sync` = the base columns plus the metadata columns. On later runs it triggers a sync. Either way it waits until `indexed_row_count` equals the table's row count.

**Changing `metadata_schema` later:** `prep_chunks.py` stops with a clear error. With the user's confirmation, drop the index `<catalog>.<schema>.<index_name>` and `<prefix>_chunks`. Update `doc-index.json`, render again, deploy, and run once with `databricks bundle run doc_index --params full_refresh=true --profile <profile>`. That re-chunks from `<prefix>_parsed` without parsing again, and re-bills `ai_prep_search` (and chart reading, if on). Then redo step 5 and module 03, and tell the user that agents on the index need their tools rendered again (for `forge-doc-qa-agent`, its module 01, step 2).

## 5. Describe the index for its consumers

For each string or enum metadata column with ≤50 distinct values, capture the values:

```sql
SELECT '<col>' AS col, collect_set(<col>) AS values, count(DISTINCT <col>) AS n
FROM <catalog>.<schema>.<prefix>_chunks;
```

Record them in `doc-index.json` as `filter_values`, mapping each metadata column to its list of values, or to `null` for open-ended columns such as years. Also record a one-sentence `corpus_description` of what the documents are, for example `"SEC filings (10-K, 10-Q, 8-K, earnings releases) for 40 US public companies"`. `forge-doc-qa-agent` renders both into its search tool's description, so the model writes valid filters; module 03 uses the description to write synthetic questions.

## Verify

```sql
SELECT count(*) AS chunks, count(DISTINCT source_uri) AS files,
       percentile(octet_length(chunk_to_embed), 0.95) AS p95_embed_bytes,
       count_if(<first_metadata_col> IS NULL) AS missing_metadata
FROM <catalog>.<schema>.<prefix>_chunks;
```

```bash
databricks vector-search-indexes get-index <catalog>.<schema>.<index_name> --profile <profile>
databricks vector-search-indexes query-index <catalog>.<schema>.<index_name> --profile <profile> \
  --json '{"columns": ["chunk_id", "file_name", "page_numbers"], "query_text": "<a question from the corpus>",
           "num_results": 3, "query_type": "HYBRID"}'
```

- `files` equals the parsed files that have no fatal error.
- More than about 10% `missing_metadata` means the field descriptions need work. Fix them before module 03.
- `filter_values` (when there's metadata) and `corpus_description` are in `doc-index.json`.
- The test query returns relevant chunks, with `file_name` and `page_numbers` filled in. If the query itself errors, **load** `databricks-vector-search` `references/search-modes.md`.
- With chart insights on: `SELECT count(*), count_if(error IS NOT NULL) FROM <catalog>.<schema>.<prefix>_chart_insights` shows readings and few errors, and one reading checked against its chart image gets the values right.

## Troubleshooting

For errors not listed here, **load** `databricks-vector-search` `references/troubleshooting-and-operations.md`.

| Symptom | Fix |
|---|---|
| `ai_prep_search` not found | Requires DBR 18.2+ or serverless env ≥ 3; it is Beta — confirm availability in the workspace |
| Index stuck provisioning | Endpoint not ONLINE, or the embedding endpoint is scaled to zero (first sync can time out) |
| `Column not found in index` at query time | Column missing from `columns_to_sync`; recreate the index (confirm first) |
| Sync never reaches the row count | Duplicate `chunk_id`s or NULL primary keys; check `SELECT chunk_id, count(*) ... HAVING count(*) > 1` |
| Capacity-exhausted errors as the corpus grows | Index capacity is sized from the source table at creation; recreate the index after the bulk load |
| Chart readings missing | No page images (`page_images_path` set after parsing means re-parsing), or figures had no description; check `<prefix>_chart_insights.error` |
