# forge-unstructured-doc-extraction design notes

Maintainer notes for `skills/forge-unstructured-doc-extraction`. This file sits outside the skill folders, so it isn't published and agents never load it.

## Where it came from

Modules 00–02 and the job scripts (`parse.py`, `prep_chunks.py`, `sync_index.py`) are the ingestion half of `doc-qa-agent` 0.3.0, split out in `forge-doc-qa-agent` 0.4.0. Their history is in `doc-qa-agent-design.md` (the foundation-repo rows on metadata, chunking, and incremental refresh) and `structured-doc-extraction-design.md` (the advance-document-processing rows on long PDFs, split pages, chart insights, and knowledge-base routing). Module 03, the retrieval evaluation, is new.

**Why split.** The index is useful on its own: another agent, an app, or a Genie space can query it, and the 0.3.0 skill already had an "index only" scope decision that stopped after module 02. As one skill, every index request routed through a skill that described itself as an agent builder and carried agent preflight (Agent Bricks CLI, app permissions) it didn't need. Split, each skill has one deliverable and its own "done" gate: retrieval hit rates for the index, answer quality for the agent.

**Name.** The pair `forge-unstructured-doc-extraction` (chunks and an index) and `forge-structured-doc-extraction` (fields in tables) differs by two letters. `forge` routes by the result the user wants, confirms a named use case by its title, and its routing evals (`skills/forge/evals/routing.yaml`) test both with the same kinds of prompts.

## Requirements asked first (module 00, step 1)

The skill asks for four things before anything else, because each one is costly to get wrong later:

- **Where the documents are** (`volume_path`): everything is keyed on it, and a volume the user can't read stops the run anyway.
- **The endpoint** (`vs_endpoint`): an endpoint is billed while it exists, and some workspaces restrict who can create one.
- **The index name** (`index_name`): consumers hard-code it (`forge-doc-qa-agent` renders it into its tool, and grants it to the app), and renaming means rebuilding.
- **Continuous updates** (`continuous_updates`): decides whether the job has a file-arrival trigger. That's a standing cost commitment, so it's the user's call, not a default.

`render.py` fails if `vs_endpoint`, `index_name`, or `continuous_updates` is missing, so a session that skipped the question can't render the job.

**Checking endpoint-create permission.** The endpoint access-control list has `CAN CREATE`, `CAN USE`, and `CAN MANAGE` ([access control](https://docs.databricks.com/aws/en/security/auth/access-control/), 2026-10-20), but no documented API reports whether a user holds `CAN CREATE` without creating an endpoint, which is billed. So module 00 treats the `admins` group as proof, asks anyone else whether an admin granted them the permission, and stops unless they confirm it. Module 02 backstops a wrong answer: a `PERMISSION_DENIED` on creation stops the run and asks for an existing endpoint, with no retry. If a read-only check is found (for example, permissions on the endpoints root object), replace the question with it. The `requirements-asked-first` scenario covers the stop.

**Index names.** `index_name` is the last part only, created in the target schema, so preflight's permission check (one schema) and the app grant (`USE SCHEMA` on one schema) stay correct. `sync_index.py` refuses to sync an existing index whose source table isn't this run's `<prefix>_chunks`, so a name collision can't touch someone else's index.

## The handoff to forge-doc-qa-agent

`doc-index.json` is the contract. `forge-doc-qa-agent` reads it and never writes it, so re-running this skill (a new metadata schema, new `filter_values`) can't leave a stale copy behind; the agent picks up changes the next time its tool is rendered.

| Key | Written by | Read by forge-doc-qa-agent for |
|---|---|---|
| `profile`, `catalog`, `schema`, `index_name` | module 00 | the index name, CLI commands, the app's index grant |
| `metadata_schema`, `filter_values` | modules 00, 02 | the tool's filter columns and allowed values (`render.py` checks they agree) |
| `corpus_description` | module 02 | the tool description (doc-qa.json can override it) |
| `completed` | every module | `02-chunk-index` must be present; a missing `03-evaluate` is reported to the user |

The index's columns are the other half of the contract: `chunk_id` (primary key), `chunk_to_embed` (embedding source, returned as the passage), `file_name` and `page_numbers` (citations), `source_uri`, and the metadata columns. `forge-doc-qa-agent` doesn't support indexes built another way, because its citations depend on these.

`ucs.yaml` in `forge-doc-qa-agent` records the dependency under `requires`, and the shared `check_base_skills.py` checks it like a base skill. `tests/test_skills.py` checks that the pinned version isn't newer than the repo's copy.

`evals.jsonl` is shared too: synthetic rows carry `expected_facts` as well as `file_name` and `pages`, so `forge-doc-qa-agent` module 03 grades answers on the same questions this skill scored retrieval on.

## Retrieval evaluation (module 03)

- **What's scored.** A question is a document hit when one of the top k results comes from its labeled file, and a page hit when that result also lists a labeled page in `page_numbers`. Both are deterministic, so the gate costs searches, not judge calls. `doc_mrr` is reported to show whether hits are near the top.
- **k = 8 and hybrid search** match what `forge-doc-qa-agent`'s tool sends, so a pass here means the agent's searches can find the passage. Searches are unfiltered unless a row carries `filters`: the model chooses filters at run time, and the index shouldn't depend on it to pass.
- **Synthetic labels.** Each sampled chunk goes into `generate_evals_df` with its `chunk_id` as `doc_uri`, and `expected_retrieved_context[].doc_uri` maps each question back to its chunk's file and pages. Questions that come back without a known source are kept for `forge-doc-qa-agent` and skipped here.
- **Thresholds.** 0.9 (document) and 0.8 (page) are starting floors, not measured ones. Synthetic questions come from one passage and usually name their entity, so they're easier than real questions. Calibrate on the first certification runs, especially `financebench-sec-filings`, whose near-duplicate filings (one company, several years) are the hard case for unfiltered search.

## Not yet verified in a workspace

Nothing here has run against a workspace; the static tests cover rendering, SQL placeholders, chart cropping, and hit-rate scoring. Beyond the parse-stage assumptions in `structured-doc-extraction-design.md` (items 1, 2, 5, and 7 apply here), these are assumptions:

1. `generate_evals_df` returns the `doc_uri` it was given in `expectations.expected_retrieved_context`, so synthetic questions can be labeled. The docs describe the field as "the context this evaluation has been synthesized from" without showing its shape; `label_synthetic` accepts a list or a single object.
2. `query_index` returns `page_numbers` as a JSON array or a JSON string in `data_array`; `as_list` accepts both.
3. `filters_json` with dict filters works with `query_type="HYBRID"` on STANDARD endpoints, as `VectorSearchRetrieverTool` sends them.
4. Element `bbox` coordinates from `ai_parse_document` are in the pixel space of the page images it saves, so chart crops (`crop_box`) land on the figure.
5. Members of the `admins` group can create AI Search endpoints, and a user with `CAN USE` on an endpoint can read its permissions with `get-permissions` (module 00, step 6 falls back to asking when it can't).

The `mixed-office-docs` scenario checks item 1 (`synthetic_rows_labeled`), `chart-heavy-deck` checks item 4, `financebench-sec-filings` checks items 2 and 3, and `requirements-asked-first` and `mixed-office-docs` check item 5.
