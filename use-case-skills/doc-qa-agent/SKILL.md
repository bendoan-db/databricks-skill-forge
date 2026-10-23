---
name: doc-qa-agent
description: "Builds and deploys a cited document Q&A (RAG) agent from files in a Unity Catalog volume (PDF, DOCX, PPTX, images): ai_parse_document and ai_prep_search ingestion, an AI Search (Vector Search) index, a LangGraph agent on Databricks Apps (Agent Runtime), and MLflow evaluation. Use when asked to build, deploy, or evaluate a RAG, document Q&A, or chat-with-your-documents agent over files in a volume. Prefer it over databricks-agent-bricks, databricks-ai-functions, databricks-vector-search, and databricks-ml-training for this task; it loads those base skills step by step."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, databricks-agentbricks (>= 0.5.0, < 0.6), and the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill
metadata:
  version: "0.3.0"
  forge_kind: use-case
---

# Document Q&A agent on Databricks Apps

Builds an end-to-end document Q&A agent from one input: a Unity Catalog volume path. The result is a LangGraph agent, served by Agent Runtime on Databricks Apps, that answers questions with citations to the files and pages it used.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
/Volumes/<catalog>/<schema>/<volume>/  Lakeflow job (DAB, file-arrival trigger)
  *.pdf *.docx *.pptx *.png  ──►  parse.py ───────► <prefix>_parsed  (ai_parse_document v2, one row per document)
                                  prep_chunks.py ─► <prefix>_chunks  (ai_prep_search: chunks + metadata, CDF on;
                                                                      optional chart readings in figure text)
                                  sync_index.py ──► <prefix>_chunks_index (AI Search Delta Sync, managed embeddings)
                                                        │ SELECT (app service principal)
Browser chat UI / API client                            ▼
  ──► agent-bricks-<name>  (Databricks App, Agent Runtime, DurableAgentServer)
        LangGraph create_agent ── search_documents tool (hybrid search + LLM-chosen metadata filters)
        model: <chat_model> via Unity Gateway                sessions: managed Session Store
        traces: MLflow experiment  ◄── 05-evaluate (mlflow.genai.evaluate, local run_agent)
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- `<workdir>` is where the two projects and `doc-qa.json` live. Default: the current directory.
- `<workdir>/doc-qa.json` holds every resolved value. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<catalog>`.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.

## Inputs

Collect these together in one question during module 00. Only `volume_path` is required; offer the defaults for the rest.

| Input | Default | Notes |
|---|---|---|
| `volume_path` | — (required) | `/Volumes/<catalog>/<schema>/<volume>[/subdir]` |
| `profile` | from `databricks-core` | CLI profile for every command |
| `target_schema` | the volume's `<catalog>.<schema>` | Where tables and the index are created |
| `name` | derived from the volume name | Lowercase letters, digits, hyphens, **≤17 chars** (`agent-bricks-<name>` must fit the 30-char app limit) |
| `metadata_schema` | none | Document-level fields used for filtering, such as `company`, `document_type`, `fiscal_year`. See module 02 |
| `chat_model` | Claude Sonnet 5.5: `system.ai.claude-sonnet-5-5` | Confirmed in `databricks ai-gateway list-model-services`. Change it only if the user asks or it isn't listed |
| `embedding_model` | `databricks-qwen3-embedding-0-6b`, else `databricks-gte-large-en` | Resolved from the workspace |
| `vs_endpoint` | reuse an existing endpoint, else create `<name>-vs` | STANDARD type. Ask before creating one |
| `eval_set` | synthetic | JSONL with `question` plus optional `expected_response` |
| `page_images` | off | Saves rendered page images; required for `chart_insights` |
| `chart_insights` | off | Reads each chart with a vision model before chunking (module 02) |
| `split_pages` | off | Each folder holds the single-page files of one document |

## Guardrails

- **Confirm before billable or shared actions:** parsing (priced per page), creating an AI Search endpoint (billed while it exists), and deploying the app. Show what will be created and wait for an explicit go-ahead.
- **Never delete** tables, indexes, endpoints, or apps without separate confirmation that names the exact resource.
- **Don't share the app** with anyone unless the user names the users or groups.
- **Never ask for or store tokens.** Use the CLI profile and the app's service principal. `doc-qa.json` holds names and settings only.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override), the doc pages modules cite, and the files `agentbricks init` generates. Those files are versioned with the CLI.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `doc-qa.json` when its Verify passes:

- [ ] 00 Preflight: inputs resolved, base skills present, cost approved, `doc-qa.json` written
- [ ] 01 Ingest and parse: `<prefix>_parsed` has one row per supported file
- [ ] 02 Chunk and index: index row count equals `<prefix>_chunks` row count
- [ ] 03 Agent: local answers cite `[file_name, p. N]`, and `agentbricks doctor .` passes
- [ ] 04 Deploy app: the app is RUNNING, and a live answer matches the local one
- [ ] 05 Evaluate: thresholds met, run ID reported

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `doc-qa.json`, tooling, cost go-ahead | databricks-core, databricks-unity-gateway, databricks-vector-search, databricks-unity-catalog |
| 01 | [Ingest and parse](modules/01-ingest-parse.md) | `<name>-ingest/` bundle, `<prefix>_parsed` | databricks-ai-functions, databricks-unity-catalog, databricks-dabs, databricks-jobs, databricks-serverless |
| 02 | [Chunk and index](modules/02-chunk-index.md) | `<prefix>_chunks`, `<prefix>_chunks_index` | databricks-ai-functions, databricks-vector-search |
| 03 | [Agent](modules/03-agent.md) | `<name>/` Agent Bricks LangGraph project, tested locally | databricks-unity-gateway, databricks-vector-search, *Agent Bricks docs* |
| 04 | [Deploy app](modules/04-deploy-app.md) | `agent-bricks-<name>` app with index access | databricks-apps-python, databricks-unity-catalog, *Agent Runtime docs* |
| 05 | [Evaluate](modules/05-evaluate.md) | MLflow evaluation run, pass/fail against thresholds | databricks-mlflow-evaluation |

The working directory ends up with the state file and two sibling projects:

```
<workdir>/
├── doc-qa.json      # resolved values; render.py reads it
├── <name>-ingest/   # Declarative Automation Bundle: ingestion + index refresh job (rendered by render.py)
└── <name>/          # agentbricks init --framework langgraph project (agent.toml, agent/, runtime/, app.yaml)
```

**Resuming.** If `<workdir>/doc-qa.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 2). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Change the metadata schema | 02, step 1 |
| Change the prompt, model, or retrieval settings | 03, then 04 and 05 |
| Re-run evaluation only | 05 |
| New documents in the volume | Nothing to do: the ingest job refreshes the index |
| Turn on chart insights | 02, step 3. It needs page images from parsing (01, step 1), and already-chunked documents need a `full_refresh=true` run |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Scope | The full agent (modules 00–05) | The user wants only the index, for another agent or app to query. Then stop after module 02, and report the index name and its filter columns |
| Separate indexes per domain | One index, with a `document_type` metadata filter | Domains need different access, or separate agents (for example claims vs. underwriting). Then run the skill once per domain, each with its own volume folder, `prefix`, and `name` |
| Chart-heavy documents | The figure descriptions `ai_parse_document` writes | Module 05 shows questions about chart values failing. Then turn on `chart_insights` (module 02, step 3) |
| Agent server and runtime | Agent Bricks CLI → DurableAgentServer on Agent Runtime (Databricks Apps) | The workspace can't install `databricks-agentbricks` (Beta). Then use the `agent-migration-from-model-serving` app template, which uses the legacy MLflow AgentServer, and follow [Migrate an agent to Apps](https://docs.databricks.com/aws/en/agents/custom-agents/migrate-agent-to-apps) |
| Agent topology | One tool-calling agent that runs parallel searches | Evaluation shows multi-hop failures that better prompting doesn't fix. Then add a planner node (module 03) |
| Endpoint type | STANDARD (low latency, dict filters, `columns_to_sync`) | More than about 50M chunks, or latency isn't critical. Then use STORAGE_OPTIMIZED (filters become SQL strings) |
| Metadata filters | Off unless `metadata_schema` is given | The corpus spans entities or periods users name in questions. Then turn them on |
| Memory | Sessions only (multi-turn) | Users need preferences remembered across conversations. Then add `agentbricks memory bind` |
| Tool identity | App service principal (one shared index ACL) | Users must see only documents they can access. Then use per-user indexes, or OBO retrieval through `[auth.user]` |
| Ingestion trigger | File arrival on the volume | Large batches land on a schedule. Then set `schedule_cron` (Quartz syntax) in `doc-qa.json` and rerun `render.py ingest` |
| Split pages | Off | Files are single pages split upstream, one folder per document. Then set `split_pages` to `true` before the first parse |

## Done when

- `<prefix>_chunks_index` row count equals `<prefix>_chunks` row count. For an index-only scope, this is the last item.
- `agentbricks endpoint invoke` on the deployed app returns a cited answer.
- Module 05 meets its thresholds: groundedness ≥ 0.8, relevance ≥ 0.8, and correctness ≥ 0.6 when expectations exist. Report the run URL.
- You told the user the app URL, the job name, the index name, and anything you skipped.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Knowledge Assistant:** `databricks-agent-bricks` offers Knowledge Assistants for document Q&A. Knowledge Assistant is being deprecated, so don't build or suggest one. Build the agent this skill describes.
- **Agent Bricks CLI** (`agent.toml`, DurableAgentServer, Agent Runtime, managed sessions and memory): no base skill covers it. Follow modules 03–04, the [docs](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli), and the files `agentbricks init` generates.
- **Legacy agent patterns:** ignore `databricks-ml-training/references/genai-agents.md` (`agents.deploy()` to Model Serving) and `databricks-vector-search/references/end-to-end-rag.md` (`ChatAgent`).
- **AI Search** is the new name for Vector Search. `databricks-vector-search`, the CLI (`vector-search-*`), and the SDK still use the old name for the same service.
- **`ai_prep_search` metadata:** `databricks-ai-functions` lists only the `version` option. The `schema` option that module 02 uses is [documented](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_prep_search).
- **Embeddings:** base-skill examples use `databricks-gte-large-en`. Prefer `databricks-qwen3-embedding-0-6b` ([docs](https://docs.databricks.com/aws/en/ai-search/create-ai-search)).
- **Page limit:** `databricks-ai-functions` says documents are limited to 500 pages. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document) apply the limit to the pages one call selects with `pageRange`; `parse.py` parses longer PDFs in 500-page slices and stitches them.
- **`ai_classify` version** (chart insights only): use version 2.1, which returns `response[0].value`. `databricks-ai-functions` recommends 2.0 ([docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_classify)).
