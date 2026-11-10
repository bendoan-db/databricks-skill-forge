# 00 · Preflight

Resolve the inputs and the layout, confirm the tooling, base skills, and permissions, and get a cost go-ahead before anything billable runs. The resolved values go in `<workdir>/extraction.json`.

## Inputs → outputs

- **Inputs:** the user's request and the volume.
- **Outputs:** `<workdir>/extraction.json` with the resolved inputs and `layout`, and the user's go-ahead on cost.

## 1. Collect inputs

Ask once, listing every input from the SKILL.md Inputs table with its proposed default. If `forge` started this skill, the user may already have given some of them: confirm those instead of asking again.

In the same question, ask whether they already have an extraction schema: the fields to pull from each kind of document, with their types. Any format works, such as an `ai_extract` schema, a JSON Schema, a Pydantic model, a table definition, or a list of fields and types.
- **They have one:** ask for it now. Save it as given, as `<workdir>/provided_schema.<ext>` (`.json`, `.py`, `.sql`, or `.md` for pasted text), and record `"provided_schema": "provided_schema.<ext>"`. Module 03 checks it and converts it, so don't copy it into `document_types` here.
- **They don't:** ask which fields they need, from which kinds of documents, in their words. Module 03 walks them through building the schema from that list and a few of their parsed documents.

In the same question, ask whether they want the review app. It has no default, so wait for a yes or a no:

> Do you want a review app? It's a web app where the people you name approve or correct the values the model is unsure of, and each decision is written back to the tables. It needs a Lakebase project, and the app is billed while it runs. Without it, the low-confidence values are listed in the `<prefix>_review` view.

- **Yes:** record `"review_app": true`, and ask who will review (users or groups). Module 07 deploys the app after the pipeline.
- **No:** record `"review_app": false`. Module 07 is skipped, and nothing for the app is created.

Derive the following and show them back to the user:

```
catalog, schema     = <target_schema>                  # default: from volume_path
prefix              = <volume name, snake_case>        # tables: <prefix>_parsed, <prefix>_<type>, ...
name                = <≤40 chars, [a-z0-9-]>           # bundle and job: <name>-extract
review.app_name     = <name>-review                    # review app only: ≤26 chars, [a-z0-9-], starting with a letter
```

With the review app, `schema` and `prefix` must be lowercase letters, digits, and `_`, because the review queue's Postgres names come from them. If the target schema's name has other characters, propose another schema.

## 2. Tooling and base skills

**Load:** `databricks-core` (profile selection and auth; warehouse discovery in step 3).

```bash
databricks --version                     # >= v1.11.0
uv --version
databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid
uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough
node --version                           # review app only: >= 20
psql --version                           # review app only: module 07 grants the app access with it
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
| Lakebase and Apps (review app only) | create a Lakebase project, or `CAN MANAGE` on the one to reuse; create Databricks Apps |

Check with `databricks grants get-effective schema <catalog>.<schema> --profile <profile>`. Report any missing privilege. Don't try to grant it. If the output is hard to read, **load** `databricks-unity-catalog` `references/1-access-control.md`.

**If the user can't create tables in the target schema**, for example because the volume's schema is read-only, propose one they can write to, and let them choose:

1. Ask whether they have a schema in mind, and check it with the same command.
2. Otherwise, look in the volume's catalog. Propose up to three schemas that have the privileges above:

   ```bash
   databricks schemas list <catalog> --profile <profile> -o json | jq -r '.[].name'
   databricks grants get-effective schema <catalog>.<candidate> --profile <profile>
   ```

3. If none has them, propose a new schema, `<catalog>.<prefix>_extraction`. Only when `databricks grants get-effective catalog <catalog> --profile <profile>` shows `USE CATALOG` and `CREATE SCHEMA`, and only after the user confirms that name, create it: `databricks schemas create <prefix>_extraction <catalog> --profile <profile>`.
4. If nothing works, list the privileges to ask an admin for, on the volume's schema or on a schema the user picks, and stop.

Record the chosen schema as `catalog` and `schema`, show the derived values from step 1 again, and rerun the check. The documents stay where they are: the job still reads them from `volume_path`, so the volume's own privileges don't change. With the review app, the new schema's name must also be lowercase letters, digits, and `_`.

## 5. Cost go-ahead

Summarize what each step creates, then wait for an explicit yes:

- **Parse:** N supported files, X MB. `ai_parse_document` is billed per page. Only new or changed files are parsed later.
- **Classify and extract:** `ai_classify` (layouts `one_per_file` and `packet`: once per document or per page) and `ai_extract` (once per document or sub-document) are billed per token. They run once per parse of each document.
- **Job:** a serverless job, triggered on file arrival once the document types exist.
- **Localize (only if on):** a single-node classic job cluster, billed while the task runs.
- **Review app (only if yes):** a Lakebase project, which scales to zero when idle; a synced table refreshed once per job run; Lakehouse Sync; a short job run per batch of decisions; and the app, billed while it runs. Module 07 confirms each one again before creating it.

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
  "review_app": false,
  "warehouse_id": "<warehouse_id>",
  "completed": []
}
```

With the review app, also write `"review": {"app_name": "<name>-review", "reviewers": ["<user or group>"]}`. Later modules add `description_element_types` and `page_images_path` (01), `classify_instructions` and `document_types` (02–03), `review_cutoffs` and `experiment` (05), `ocr_cluster` (06), and `review` (07).

## Verify

- Every input is resolved and echoed back. `name` is ≤40 characters and matches `^[a-z0-9-]+$`.
- The user gave a schema, saved and recorded as `provided_schema`, or listed the fields they need.
- The user answered the review app question, and `review_app` is `true` or `false`. With `true`, `review.reviewers` names who reviews.
- `check_base_skills.py` exits 0.
- The user can create tables in the target schema: the volume's, or one they chose in step 4.
- The user confirmed the layout and approved the cost summary, and `extraction.json` exists.
