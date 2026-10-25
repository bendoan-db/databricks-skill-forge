# 00 · Preflight

Resolve the inputs and the layout, confirm the tooling, base skills, and permissions, and get a cost go-ahead before anything billable runs. The resolved values go in `<workdir>/extraction.json`.

## Inputs → outputs

- **Inputs:** the user's request and the volume.
- **Outputs:** `<workdir>/extraction.json` with the resolved inputs and `layout`, and the user's go-ahead on cost.

## 1. Collect inputs

Ask once, listing every input from the SKILL.md Inputs table with its proposed default. If `forge` started this skill, the user may already have given some of them: confirm those instead of asking again. Ask which fields the user needs from which kinds of documents, in their words; modules 02–03 turn that into labels and schemas.

Derive the following and show them back to the user:

```
catalog, schema     = <target_schema>                  # default: from volume_path
prefix              = <volume name, snake_case>        # tables: <prefix>_parsed, <prefix>_<type>, ...
name                = <≤40 chars, [a-z0-9-]>           # bundle and job: <name>-extract
```

## 2. Tooling and base skills

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 3).

```bash
databricks --version                     # >= v1.11.0
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
```

`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`. If it exits non-zero, stop: the modules cite reference files that the installed base skills don't have. Ask the user to install or update databricks-agent-skills (v0.2.27 or later) in the same skills directory, for example with `databricks aitools update`. Mention any `newer` skills in your final report.

## 3. Volume, files, and layout

```bash
databricks volumes read <catalog>.<schema>.<volume> --profile <profile>
WAREHOUSE_ID=$(databricks experimental aitools tools get-default-warehouse --profile <profile>)   # or pick from `databricks warehouses list`
databricks experimental aitools tools query --warehouse "$WAREHOUSE_ID" --profile <profile> "
  SELECT lower(regexp_extract(path, '\\.([^.]+)$', 1)) AS ext, count(*) AS files,
         round(sum(length)/1e6, 1) AS mb, sum(CASE WHEN length > 100*1024*1024 THEN 1 ELSE 0 END) AS over_100mb,
         count(DISTINCT regexp_extract(path, '^(.*)/[^/]+$', 1)) AS folders
  FROM read_files('<volume_path>', format => 'binaryFile', recursiveFileLookup => true)
  GROUP BY 1 ORDER BY files DESC"
```

Record the warehouse as `warehouse_id`. Supported types: pdf, jpg/jpeg, png, tif/tiff, doc/docx, ppt/pptx. Files over 100 MB are recorded as skipped; PDFs over 500 pages are parsed in slices. If there are no supported files, stop.

Propose a `layout` from the file names and a look at two or three files (open them with the user, or ask), and confirm it:
- `single_type`: every file is one document of the same kind, such as a folder of invoices.
- `one_per_file`: each file is one document, of several kinds, such as a claims inbox.
- `packet`: a file bundles several documents, such as a loan file with an application, pay stubs, and bank statements.

If the files are single pages with one folder per document (`page_1.pdf`, `page_2.pdf`, ...), set `split_pages` to `true`.

## 4. Permissions

The user running the skill needs:

| On | Privilege |
|---|---|
| volume | `READ VOLUME` (+ `USE CATALOG`/`USE SCHEMA`) |
| target schema | `CREATE TABLE`, `USE SCHEMA`; `CREATE VOLUME` when page images are needed (module 06) |
| workspace | create Lakeflow jobs; serverless compute enabled; classic job clusters only for module 06 |

Check with `databricks grants get-effective schema <catalog>.<schema> --profile <profile>`. Report any missing privilege. Don't try to grant it. If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md`.

## 5. Cost go-ahead

Summarize what each step creates, then wait for an explicit yes:

- **Parse:** N supported files, X MB. `ai_parse_document` is billed per page. Only new or changed files are parsed later.
- **Classify and extract:** `ai_classify` (layouts `one_per_file` and `packet`: once per document or per page) and `ai_extract` (once per document or sub-document) are billed per token. They run once per parse of each document.
- **Job:** a serverless job, triggered on file arrival once the document types exist.
- **Localize (only if on):** a single-node classic job cluster, billed while the task runs.

## 6. Write `extraction.json`

After the go-ahead, write the resolved values to `<workdir>/extraction.json`:

```json
{
  "profile": "<profile>",
  "volume_path": "/Volumes/<catalog>/<schema>/<volume>",
  "catalog": "<catalog>",
  "schema": "<schema>",
  "prefix": "<prefix>",
  "name": "<name>",
  "layout": "packet",
  "split_pages": false,
  "review_threshold": 0.8,
  "localize": false,
  "warehouse_id": "<warehouse_id>",
  "completed": []
}
```

Later modules add `description_element_types` and `page_images_path` (01), `classify_instructions` and `document_types` (02–03), `experiment` (05), and `ocr_cluster` (06).

## Verify

- Every input is resolved and echoed back. `name` is ≤40 characters and matches `^[a-z0-9-]+$`.
- `check_base_skills.py` exits 0.
- The user confirmed the layout and approved the cost summary, and `extraction.json` exists.
