---
name: forge-doc-qa-agent
description: "Forge use case, normally started by the forge skill: builds and deploys a cited document Q&A (RAG) agent over the documents in a Unity Catalog volume, as a LangGraph agent on Databricks Apps (Agent Runtime) that runs hybrid searches with metadata filters on an AI Search index and cites [file_name, p. N], with managed sessions and MLflow evaluation. Runs forge-unstructured-doc-extraction first when the documents aren't indexed yet. Use when forge hands off to it, or when the user names this skill."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, databricks-agentbricks (>= 0.5.0, < 0.6), the forge-unstructured-doc-extraction use-case skill, and the databricks-agent-skills base skills (>= v0.2.27), all installed in the same skills directory as this skill
metadata:
  version: "0.4.0"
  forge_kind: use-case
---

# Document Q&A agent on Databricks Apps

Builds an end-to-end document Q&A agent over an AI Search index of the user's documents. The result is a LangGraph agent, served by Agent Runtime on Databricks Apps, that answers questions with citations to the files and pages it used.

The index comes from the `forge-unstructured-doc-extraction` use-case skill: it parses the volume, chunks the documents, keeps the index fresh, and measures retrieval. This skill runs it first when there's no index yet (module 00), then builds, deploys, and evaluates the agent.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
forge-unstructured-doc-extraction (run first; writes doc-index.json)
  /Volumes/... ─► parse ─► chunk ─► <index_name>   AI Search index: managed embeddings, metadata columns,
                                          │          refreshed on file arrival with continuous updates
                                          │ SELECT (app service principal)
Browser chat UI / API client              ▼
  ──► agent-bricks-<name>  (Databricks App, Agent Runtime, DurableAgentServer)
        LangGraph create_agent ── search_documents tool (hybrid search + LLM-chosen metadata filters)
        model: <chat_model> via Unity Gateway                sessions: managed Session Store
        traces: MLflow experiment  ◄── 03-evaluate (mlflow.genai.evaluate, local run_agent)
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills and `forge-unstructured-doc-extraction` are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `doc-qa.json`, the index's `doc-index.json`, and the agent project live. Default: the current directory.
- `<workdir>/doc-qa.json` holds every value this skill resolves. Module 00 creates it, each module reads it first, and you record what a module adds before you move on.
- `<workdir>/doc-index.json` belongs to `forge-unstructured-doc-extraction`: read it, but never write it. `<profile>`, `<catalog>`, `<schema>`, `<index_name>`, `<filter_values>`, and `<corpus_description>` come from it. Every other `<key>` in commands means the value of `key` in `doc-qa.json`, for example `<name>`.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.

## Inputs

Collect these together in one question during module 00, after the index exists. Offer the defaults.

| Input | Default | Notes |
|---|---|---|
| `index_state` | `doc-index.json` in `<workdir>` | Written by `forge-unstructured-doc-extraction`. Missing? Module 00 runs that skill first, which needs the `volume_path` |
| `name` | the index's `name` | Lowercase letters, digits, hyphens, **≤17 chars** (`agent-bricks-<name>` must fit the 30-char app limit) |
| `chat_model` | Claude Sonnet 5.5: `system.ai.claude-sonnet-5-5` | Confirmed in `databricks ai-gateway list-model-services`. Change it only if the user asks or it isn't listed |
| `eval_set` | `evals.jsonl` in `<workdir>` | From `forge-unstructured-doc-extraction` module 03; JSONL with `question` plus optional `expected_response` or `expected_facts` |

## Guardrails

- **Confirm before billable or shared actions:** running `forge-unstructured-doc-extraction` (it asks for its own go-ahead), deploying the app, and evaluation runs. Show what will be created and wait for an explicit go-ahead.
- **Never delete** apps, indexes, tables, or endpoints without separate confirmation that names the exact resource.
- **Don't share the app** with anyone unless the user names the users or groups.
- **Never ask for or store tokens.** Use the CLI profile and the app's service principal. `doc-qa.json` holds names and settings only.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override), the doc pages modules cite, and the files `agentbricks init` generates. Those files are versioned with the CLI.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `doc-qa.json` when its Verify passes:

- [ ] 00 Preflight: index found (or built with `forge-unstructured-doc-extraction`), inputs resolved, base skills present, cost approved, `doc-qa.json` written
- [ ] 01 Agent: local answers cite `[file_name, p. N]`, and `agentbricks doctor .` passes
- [ ] 02 Deploy app: the app is RUNNING, and a live answer matches the local one
- [ ] 03 Evaluate: thresholds met, run ID reported

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | the index (via `forge-unstructured-doc-extraction`), `doc-qa.json`, tooling, cost go-ahead | databricks-core, databricks-unity-gateway, databricks-unity-catalog |
| 01 | [Agent](modules/01-agent.md) | `<name>/` Agent Bricks LangGraph project, tested locally | databricks-unity-gateway, databricks-vector-search, *Agent Bricks docs* |
| 02 | [Deploy app](modules/02-deploy-app.md) | `agent-bricks-<name>` app with index access | databricks-apps-python, databricks-unity-catalog, *Agent Runtime docs* |
| 03 | [Evaluate](modules/03-evaluate.md) | MLflow evaluation run, pass/fail against thresholds | databricks-mlflow-evaluation |

The working directory ends up with both skills' state files, the index's bundle, and the agent project:

```
<workdir>/
├── doc-index.json   # the index's values (forge-unstructured-doc-extraction); read-only here
├── evals.jsonl      # the index's eval set; module 03 copies it into the project
├── <name>-index/    # forge-unstructured-doc-extraction's bundle: parse → chunk → sync_index job
├── doc-qa.json      # this skill's values; render.py reads it
└── <name>/          # agentbricks init --framework langgraph project (agent.toml, agent/, runtime/, app.yaml)
```

**Resuming.** If `<workdir>/doc-qa.json` exists, read it and `doc-index.json`, confirm their values with the user, and rerun module 00's base-skill check (step 3). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Change the prompt, model, or retrieval settings | 01, then 02 and 03 |
| Re-run evaluation only | 03 |
| New documents in the volume | Nothing to do if the index has continuous updates. Otherwise rerun its job (`forge-unstructured-doc-extraction` resume table) |
| The index's metadata schema or `filter_values` changed | 01, step 2 (render again), then 02 |
| Change what's indexed (metadata, chart insights, parse options) | `forge-unstructured-doc-extraction`'s resume table, then 01, step 2 here |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Agent server and runtime | Agent Bricks CLI → DurableAgentServer on Agent Runtime (Databricks Apps) | The workspace can't install `databricks-agentbricks` (Beta). Then use the `agent-migration-from-model-serving` app template, which uses the legacy MLflow AgentServer, and follow [Migrate an agent to Apps](https://docs.databricks.com/aws/en/agents/custom-agents/migrate-agent-to-apps) |
| Agent topology | One tool-calling agent that runs parallel searches | Evaluation shows multi-hop failures that better prompting doesn't fix. Then add a planner node (module 01) |
| Separate agents per domain | One agent over one index | Domains need different access (for example claims vs. underwriting). Then build one index per domain with `forge-unstructured-doc-extraction`, each in its own `<workdir>`, and run this skill in each |
| Memory | Sessions only (multi-turn) | Users need preferences remembered across conversations. Then add `agentbricks memory bind` |
| Tool identity | App service principal (one shared index ACL) | Users must see only documents they can access. Then use per-user indexes, or OBO retrieval through `[auth.user]` |

## Done when

- `agentbricks endpoint invoke` on the deployed app returns a cited answer.
- Module 03 meets its thresholds: groundedness ≥ 0.8, relevance ≥ 0.8, and correctness ≥ 0.6 when expectations exist. Report the run URL.
- You told the user the app URL, the index it searches, and anything you skipped. If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **Knowledge Assistant:** `databricks-agent-bricks` offers Knowledge Assistants for document Q&A. Knowledge Assistant is being deprecated, so don't build or suggest one. Build the agent this skill describes.
- **Agent Bricks CLI** (`agent.toml`, DurableAgentServer, Agent Runtime, managed sessions and memory): no base skill covers it. Follow modules 01–02, the [docs](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli), and the files `agentbricks init` generates.
- **Legacy agent patterns:** ignore `databricks-ml-training/references/genai-agents.md` (`agents.deploy()` to Model Serving) and `databricks-vector-search/references/end-to-end-rag.md` (`ChatAgent`).
- **AI Search** is the new name for Vector Search. `databricks-vector-search`, the CLI (`vector-search-*`), and the SDK still use the old name for the same service.
