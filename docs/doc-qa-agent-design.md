# doc-qa-agent design notes

Maintainer notes for `use-case-skills/doc-qa-agent`. This file sits outside the skill folder, so it isn't published and agents never load it.

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

## Runtime design (v0.2.0)

- **Base skills.** For now the base skills are upstream databricks-agent-skills, installed in the same skills directory as this skill, not a Forge base release. `scripts/check_base_skills.py` checks presence and versions against `ucs.yaml`. The floor is v0.2.27, because earlier tags lack `databricks-unity-gateway` and `databricks-serverless`. Upstream rarely bumps `metadata.version` when content changes: from v0.2.29 to v0.2.31, `databricks-ai-functions` and `databricks-apps-python` changed but kept their versions. Drift detection should compare file hashes, not versions.
- **Load when, not load first.** Modules name the base skill and reference file at the step that needs it, and push troubleshooting-only references into the troubleshooting tables. Loading every cited skill and reference up front cost about 46K tokens across the workflow at v0.2.29.
- **`doc-qa.json` and `render.py`.** Resolved values live in one file in the working directory, so a later session or a compacted context can resume. Templates are rendered from it, never hand-filled. The motivating bug: module 02's example schema contains an apostrophe, which broke `databricks.yml` when pasted single-quoted, as the module used to say. `render.py` writes JSON strings, which are valid YAML scalars, and checks that every value round-trips.
- **Hand-edit protection.** Rendered files start with a checksum header. A re-render stops if the body no longer matches, unless `--force` is passed. Settings the decision table offers, such as `schedule_cron`, live in `doc-qa.json` so they don't need hand edits.
- **Named SQL sections.** `ingest.py` runs the `.sql` files one `-- name:` section at a time, so CI can run `EXPLAIN` on each section.
- **Knowledge Assistant.** Being deprecated (skill owner direction, 2026-10-15). The skill must not recommend it, even though `databricks-agent-bricks` advertises it for document Q&A. SKILL.md overrides that guidance, and the `no_knowledge_assistant` scenario check enforces it.
