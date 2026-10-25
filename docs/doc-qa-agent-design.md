# forge-doc-qa-agent design notes

Maintainer notes for `skills/forge-doc-qa-agent`. This file sits outside the skill folder, so it isn't published and agents never load it. Since 0.4.0, the ingestion half (parsing, chunking, the index) is `forge-unstructured-doc-extraction`; the ingestion rows below describe that skill now, and its own notes are in `unstructured-doc-extraction-design.md`.

## Changes from the foundation repo

The skill generalizes [databricks-financebench-agent](https://github.com/bendoan-db/databricks-financebench-agent), an SEC-filings agent served on Model Serving.

| Foundation repo | This skill | Why |
|---|---|---|
| Filename → `ai_extract`/`ai_query` metadata against an entities table | `ai_prep_search(..., map('schema', ...))` document-level metadata | Domain-agnostic; reads content, not filenames |
| Page aggregation + LLM quality filter + LLM page summaries (3 batch LLM passes) | `ai_prep_search` semantic chunks with context-enriched `chunk_to_embed` | One function replaces three stages, at lower cost |
| Full overwrite every run | Incremental by `(path, modificationTime)`, deletions propagated | Volume-driven refresh |
| `SelfQueryRetriever` (langchain-classic + lark) with a hardcoded company list | `VectorSearchRetrieverTool(dynamic_filter=True)` plus distinct values in the tool description | Fewer dependencies, and filter values come from data |
| Supervisor → doc agent / deep-research agent → final-answer node (4+ LLM calls) | One `create_agent` with parallel tool calls | Less latency and cost; the planner-node decision point covers the exception |
| `databricks-claude-*` serving endpoints | `system.ai.*` model services via Unity Gateway | Legacy pay-per-token endpoints are being retired |
| Lakebase `CheckpointSaver(instance_name=...)` | Managed Session Store (`agentbricks init --session-store`) | No database to operate |
| `agents.deploy()` → Model Serving | `agentbricks deploy` → Agent Runtime on Databricks Apps | Model Serving is the legacy agent runtime |
| PAT in a secret scope | CLI OAuth profile + app service principal | Apps don't accept PATs |

**Why one agent.** The foundation repo routed every question through a supervisor, a document agent or deep-research agent, and a final-answer node: four or more LLM calls, plus a self-query call. Current tool-calling models plan sub-queries and call the tool in parallel on their own, so one `create_agent` loop costs fewer calls and less latency.

## Runtime design (v0.2.0, still current)

- **Base skills.** For now the base skills are upstream databricks-agent-skills, installed in the same skills directory as this skill, not a Forge base release. `scripts/check_base_skills.py` checks presence and versions against `ucs.yaml`. The floor is v0.2.27, because earlier tags lack `databricks-unity-gateway` and `databricks-serverless`. Upstream rarely bumps `metadata.version` when content changes: from v0.2.29 to v0.2.31, `databricks-ai-functions` and `databricks-apps-python` changed but kept their versions. Drift detection should compare file hashes, not versions.
- **Load when, not load first.** Modules name the base skill and reference file at the step that needs it, and push troubleshooting-only references into the troubleshooting tables. Loading every cited skill and reference up front cost about 46K tokens across the workflow at v0.2.29.
- **`doc-qa.json` and `render.py`.** Resolved values live in one file in the working directory, so a later session or a compacted context can resume. Templates are rendered from it, never hand-filled. The motivating bug: module 02's example schema contains an apostrophe, which broke `databricks.yml` when pasted single-quoted, as the module used to say. `render.py` writes JSON strings, which are valid YAML scalars, and checks that every value round-trips.
- **Hand-edit protection.** Rendered files start with a checksum header. A re-render stops if the body no longer matches, unless `--force` is passed. Settings the decision table offers, such as `schedule_cron`, live in `doc-qa.json` so they don't need hand edits.
- **Named SQL sections.** `ingest.py` runs the `.sql` files one `-- name:` section at a time, so CI can run `EXPLAIN` on each section.
- **Knowledge Assistant.** Being deprecated (skill owner direction, 2026-10-15). The skill must not recommend it, even though `databricks-agent-bricks` advertises it for document Q&A. SKILL.md overrides that guidance, and the `no_knowledge_assistant` scenario check enforces it.

## Changes in v0.3.0

Built alongside `forge-structured-doc-extraction` from the advance-document-processing repo; see `structured-doc-extraction-design.md` for the source mapping and the assumptions not yet verified in a workspace.

- **Shared parse stage.** `ingest.py` and `parse_documents.sql` became `parse.py` and `parse.sql`, identical in both skills, plus `prep_chunks.py` for chunking. The job now has three tasks: `parse`, `chunk`, `sync_index`. The parsed table gained `source_files`, `page_count`, and `parse_mode`; tables created by v0.2.0 must be dropped before the first v0.3.0 run (none were deployed).
- **Long PDFs and split pages.** PDFs over 500 pages are parsed in `pageRange` slices instead of failing; `split_pages` assembles folders of page files.
- **`<prefix>_progress`.** With parsing in its own task, the chunk task needs to know what it has chunked. It records `(path, parsed_at)` per document, so a document with no chunks isn't re-chunked (and re-billed) on every run.
- **`full_refresh` job parameter.** Module 02 used to say "run once with `--full-refresh`", which a bundle run can't pass to one task. It's now `databricks bundle run doc_qa_ingest --params full_refresh=true`.
- **Chart insights** (optional). Charts are classified from their parse descriptions, cropped from page images, read by `ai_query` with `files =>`, and appended to the figure description before `ai_prep_search`, so the readings keep their page numbers.
- **Knowledge-base decisions.** The knowledge-base pipeline's classify-and-route stage is a `document_type` metadata filter on one index. New decision points cover an index-only scope and separate indexes per domain.

## Changes in v0.4.0

- **Split.** Modules 01–02 (ingest and parse, chunk and index) and their scripts moved to the new `forge-unstructured-doc-extraction` skill, which adds a retrieval evaluation as its gate. This skill keeps preflight, the agent, the app, and answer evaluation, renumbered 00–03. Rationale and the handoff contract are in `unstructured-doc-extraction-design.md`.
- **Two state files.** `doc-qa.json` now holds only agent values (`index_state`, `name`, `chat_model`, `eval_set`, then `experiment`, `app_url`). Index values (`profile`, `catalog`, `schema`, `index_name`, `metadata_schema`, `filter_values`, `corpus_description`) are read from `doc-index.json` at render time, never copied, so they can't go stale.
- **`render.py`** renders only `agent/retrieval.py`; the `ingest` and `agent` subcommands are gone (`render.py --state doc-qa.json`). It no longer imports `prep_chunks.py`: the index skill validated the metadata types, so it only checks that `filter_values` names metadata columns.
- **`evaluate.py`** no longer synthesizes questions. The index skill writes `evals.jsonl` with `expected_facts`, and module 03 copies it into the project before adding out-of-scope questions.
- **`requires` in `ucs.yaml`.** The first use-case skill that depends on another. `check_base_skills.py` (shared) checks `requires` entries like base skills and reports them separately.
- **Coverage gaps** for the `ai_prep_search` schema option, the embedding model, `ai_classify` v2.1, and the page limit moved with the ingestion modules. Knowledge Assistant and the AI Search rename stay here, and are recorded in both skills.
- **Renamed `forge-doc-qa-agent`,** and normally started by the `forge` router. Its description no longer competes with the base skills; its routing evals moved to `skills/forge/evals/routing.yaml`. See `forge-design.md`.
