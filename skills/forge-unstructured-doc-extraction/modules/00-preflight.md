# 00 · Preflight

Resolve every input, confirm the tooling, base skills, and permissions, and get a cost go-ahead before anything billable runs. The resolved values go in `<workdir>/doc-index.json`.

## 1. Ask for the requirements

Before anything else, ask the user for these four things in one question, and wait for answers. Don't fill them in from defaults without asking. If the user already answered one, for example in their request to `forge`, confirm that answer in the same question instead of asking it fresh:

| Requirement | Records | What to ask |
|---|---|---|
| Where the documents are | `volume_path` | A Unity Catalog volume path, `/Volumes/<catalog>/<schema>/<volume>[/subdir]`. Documents in cloud storage need a volume first: an external volume can point at the existing location. If the user doesn't have one, stop until they do |
| The AI Search endpoint to use | `vs_endpoint` | An endpoint name, or none to have one created. Step 6 checks either answer |
| The name of the AI Search index | `index_name` | Propose `<prefix>_chunks_index`. Letters, digits, and underscores; the index is created in the target schema as `<catalog>.<schema>.<index_name>`. If the user gives a three-part name, its catalog and schema become `target_schema` |
| Continuous updates | `continuous_updates` | Yes: the job runs whenever files arrive in the volume, and the index stays current. No: build the index once, and rerun the job by hand when documents change |

## 2. Collect the other inputs

Propose defaults for every other input in the SKILL.md Inputs table, in the same message as step 1 or right after it. For `metadata_schema`, suggest 2–5 fields from a quick look at file names, and explain that these fields become search filters. Example for SEC filings: `company`, `document_type`, `fiscal_year`.

Derive the following and show them back to the user, with the four requirements:

```
catalog, schema     = <target_schema>                  # default: from volume_path
prefix              = <volume name, snake_case>        # tables: <prefix>_parsed, <prefix>_chunks
index               = <catalog>.<schema>.<index_name>
name                = <≤40 chars, [a-z0-9-]>           # bundle: <name>-index
updates             = file arrival | none              # from continuous_updates
```

If `forge-doc-qa-agent` sent you here, keep `name` to 17 characters or fewer, so the agent's app name can reuse it.

## 3. Tooling and base skills

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 4).

```bash
databricks --version                     # >= v1.11.0 (needed for `ai-gateway`)
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
```

`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`. If it exits non-zero, stop: the modules cite reference files that the installed base skills don't have. Ask the user to install or update databricks-agent-skills (v0.2.27 or later) in the same skills directory, for example with `databricks aitools update`. Mention any `newer` skills in your final report. They usually work, but the skill owner should re-certify against them.

## 4. Volume and files

```bash
databricks volumes read <catalog>.<schema>.<volume> --profile <profile>
WAREHOUSE_ID=$(databricks experimental aitools tools get-default-warehouse --profile <profile>)   # or pick from `databricks warehouses list`
databricks experimental aitools tools query --warehouse "$WAREHOUSE_ID" --profile <profile> "
  SELECT lower(regexp_extract(path, '\\.([^.]+)$', 1)) AS ext, count(*) AS files,
         round(sum(length)/1e6, 1) AS mb, sum(CASE WHEN length > 100*1024*1024 THEN 1 ELSE 0 END) AS over_100mb
  FROM read_files('<volume_path>', format => 'binaryFile', recursiveFileLookup => true)
  GROUP BY 1 ORDER BY files DESC"
```

Record the warehouse as `warehouse_id`. Supported types: pdf, jpg/jpeg, png, tif/tiff, doc/docx, ppt/pptx. Other types are skipped. Files over 100 MB are recorded as skipped and are not retried. PDFs over 500 pages are parsed in 500-page slices; other formats over 500 pages fail to parse. If the files are single pages with one folder per document (`page_1.pdf`, `page_2.pdf`, ...), plan to set `split_pages` (module 01). If there are no supported files, stop.

## 5. Models

```bash
# Embedding model for the index (AI Search takes a serving-endpoint name here).
databricks serving-endpoints list -o json --profile <profile> \
  | jq -r '.[] | select(.task=="llm/v1/embeddings") | .name'
```

- Embeddings: prefer `databricks-qwen3-embedding-0-6b`, then `databricks-gte-large-en`. You can't change the embedding model after the index is created without rebuilding the index.
- Chart model, only if `chart_insights` is on. **Load:** `databricks-unity-gateway` `references/querying.md` (how `system.ai` names resolve). Confirm that the default, `system.ai.claude-sonnet-5-5` (Claude Sonnet 5.5), appears in `databricks ai-gateway list-model-services --parent schemas/system.ai -o json --profile <profile>`, and leave `chart_model` unset to use it. Don't guess other names. On Azure Government, Azure China, and AWS GovCloud/DoD, `system.ai` isn't available yet. There, record the legacy endpoint `databricks-claude-sonnet-5-5` as `chart_model` if `databricks serving-endpoints list` shows it.

## 6. AI Search endpoint and index

```bash
databricks vector-search-endpoints list-endpoints --profile <profile>
databricks current-user me -o json --profile <profile>        # user name and groups
```

**The user named an endpoint.** If it's in the list, check that it is ONLINE, and that the user has `CAN USE` or `CAN MANAGE` on it: they created it (`creator`), or `databricks vector-search-endpoints get-permissions <endpoint id> --profile <profile>` lists them or one of their groups. If that ACL can't be read, ask the user to confirm access with the endpoint's owner. Without `CAN USE`, the index can't be created on it, so ask for another endpoint. If the name isn't in the list, it's a new endpoint to create with that name: check as below.

**No endpoint named, or a new one.** Check whether the user can create AI Search endpoints. Creation is controlled by the endpoint access-control list (`CAN CREATE`, or higher), and no API reports it without creating an endpoint, which is billed. So:
- If the user is in the `admins` group, they can create one. Propose a STANDARD endpoint, named as the user said or `<name>-vs`, created in module 02 after the cost go-ahead.
- Otherwise, ask whether a workspace admin has granted them permission to create AI Search endpoints. If they confirm it, propose the endpoint as above.
- If they can't confirm it, stop. Tell the user they must specify an existing endpoint they can use before you continue, and list the ONLINE endpoints from `list-endpoints` to choose from. Don't choose one for them.

Each endpoint holds up to 50 indexes. If the user is weighing STANDARD against STORAGE_OPTIMIZED, **load** `databricks-vector-search` for the endpoint types.

**The index name.** Check that `<catalog>.<schema>.<index_name>` doesn't exist yet:

```bash
databricks vector-search-indexes get-index <catalog>.<schema>.<index_name> --profile <profile>
```

`RESOURCE_DOES_NOT_EXIST` is the expected answer. If the index exists, it must be this skill's index from an earlier run (its source table is `<catalog>.<schema>.<prefix>_chunks`); otherwise ask for another name. `sync_index.py` refuses to sync an index built from a different table.

## 7. Permissions

The user running the skill needs:

| On | Privilege |
|---|---|
| volume | `READ VOLUME` (+ `USE CATALOG`/`USE SCHEMA`) |
| target schema | `CREATE TABLE`, `USE SCHEMA`; `CREATE VOLUME` only if `page_images` is on |
| workspace | create Lakeflow jobs; serverless compute enabled |
| AI Search | `CAN USE` on the named endpoint, or permission to create one (step 6) |

Check with `databricks grants get-effective schema <catalog>.<schema> --profile <profile>`. Report any missing privilege. Don't try to grant it. If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md`.

## 8. Cost go-ahead

Summarize what each step creates, then wait for an explicit yes:

- **Parse and chunk:** N supported files, X MB. `ai_parse_document` and `ai_prep_search` are billed per page/token; only new or changed files are reprocessed later.
- **Chart insights (only if on):** one `ai_classify` call per described figure and one vision-model call per chart, once per parse.
- **AI Search:** a STANDARD endpoint is billed while it exists, even when idle. The index is billed by size.
- **Job:** a serverless job. With continuous updates, it runs whenever files arrive; without, only when you start it.
- **Retrieval evaluation (module 03):** one search per question, plus LLM calls to write synthetic questions if there's no eval set.

## 9. Write `doc-index.json`

After the go-ahead, write the resolved values to `<workdir>/doc-index.json`:

```json
{
  "profile": "<profile>",
  "volume_path": "/Volumes/<catalog>/<schema>/<volume>",
  "catalog": "<catalog>",
  "schema": "<schema>",
  "prefix": "<prefix>",
  "name": "<name>",
  "metadata_schema": null,
  "embedding_model": "databricks-qwen3-embedding-0-6b",
  "vs_endpoint": "<vs_endpoint>",
  "index_name": "<index_name>",
  "continuous_updates": true,
  "warehouse_id": "<warehouse_id>",
  "eval_set": "synthetic",
  "completed": []
}
```

`metadata_schema` is the proposed JSON object, or `null`; module 02 finalizes it. Add `chart_insights: true` and `chart_model` when they apply. Later modules add `description_element_types`, `page_images_path`, and `split_pages` (01), `filter_values` and `corpus_description` (02), and `eval_experiment` (03).

## Verify

- The user answered all four requirements in step 1, and every other input is resolved and echoed back. `name` matches `^[a-z0-9-]{1,40}$`, and `index_name` matches `^[A-Za-z0-9_]+$`.
- `vs_endpoint` is an ONLINE endpoint the user can use, or a new endpoint they can create. If neither, you stopped and asked for one.
- `<catalog>.<schema>.<index_name>` doesn't exist, or is this skill's index from an earlier run.
- `check_base_skills.py` exits 0.
- The embedding endpoint exists, and with chart insights on, the chart model is listed.
- The user has approved the cost summary, and `doc-index.json` exists.
