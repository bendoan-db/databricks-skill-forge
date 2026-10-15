# Draft change proposals for databricks-agent-skills

Drafts for the base-skill gaps that `use-case-skills/doc-qa-agent` works around, in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Checked against upstream v0.2.29. Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in `ucs.yaml`.

The Knowledge Assistant deprecation is left out, because there's no public notice to cite yet.

---

## 1. Document the `ai_prep_search` `schema` option

**Problem or opportunity.** `databricks-ai-functions/references/1-task-functions.md` lists only `version` under `ai_prep_search` options. The function also takes a `schema` option that extracts typed, document-level metadata into each chunk's `metadata`. Agents that follow the skill can't build filterable indexes from it.

**Proposed change.** Add the `schema` option to the `ai_prep_search` section, with a short example and the supported field types, and show how per-chunk `metadata` becomes filter columns in the custom RAG pipeline pattern.

**Affected skill or area.** `databricks-ai-functions` (`references/1-task-functions.md`, `ai_prep_search` section).

**Additional context.** [ai_prep_search docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_prep_search).

---

## 2. Rename Vector Search to AI Search

**Problem or opportunity.** `databricks-vector-search` still says "Databricks Vector Search" and refers to the `databricks-vectorsearch` package. The docs now call the product AI Search and use the `databricks-ai-search` client.

**Proposed change.** Use the AI Search name, note that the CLI command groups and SDK classes keep the `vector-search` names, and point to the `databricks-ai-search` client.

**Affected skill or area.** `databricks-vector-search` (`SKILL.md` and references).

**Additional context.** [AI Search docs](https://docs.databricks.com/aws/en/ai-search/ai-search).

---

## 3. Update the embedding model recommendation

**Problem or opportunity.** `databricks-vector-search` uses `databricks-gte-large-en` in its examples and model table and doesn't mention `databricks-qwen3-embedding-0-6b`. The docs recommend qwen3 for production on standard endpoints.

**Proposed change.** Recommend `databricks-qwen3-embedding-0-6b` for production, keep `databricks-gte-large-en` as a fallback, and note that the embedding model can't change without rebuilding the index.

**Affected skill or area.** `databricks-vector-search` (`SKILL.md`, embedding model table).

**Additional context.** [Create an AI Search index](https://docs.databricks.com/aws/en/ai-search/create-ai-search).

---

## 4. Retire Model Serving agent deployment patterns

**Problem or opportunity.** `databricks-ml-training/references/genai-agents.md` deploys LangGraph agents with `agents.deploy()` to Model Serving, and `databricks-vector-search/references/end-to-end-rag.md` builds a `ChatAgent`. Agent Runtime on Databricks Apps is now the documented runtime for custom agents.

**Proposed change.** Mark both patterns as legacy, and point to Agent Runtime on Databricks Apps, plus the migration guide for existing Model Serving agents.

**Affected skill or area.** `databricks-ml-training` (`references/genai-agents.md`), `databricks-vector-search` (`references/end-to-end-rag.md`).

**Additional context.** [Agent Runtime](https://docs.databricks.com/aws/en/agents/deploy/agent-runtime), [Migrate an agent to Apps](https://docs.databricks.com/aws/en/agents/custom-agents/migrate-agent-to-apps).

---

## 5. Cover the Agent Bricks CLI

**Problem or opportunity.** No skill covers the Agent Bricks CLI: `agentbricks init`, `agent.toml`, DurableAgentServer, Agent Runtime, or managed sessions and memory. Agents fall back to the Model Serving patterns above.

**Proposed change.** Add a skill, or a reference in an existing agent skill, covering the init → dev → doctor → deploy loop, `agent.toml` resources, session and memory stores, and how deployed agents are queried.

**Affected skill or area.** New skill, or `databricks-agent-bricks`.

**Additional context.** [Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli), [Agent memory and sessions](https://docs.databricks.com/aws/en/agents/custom-agents/stateful-agents), [Agent Server](https://docs.databricks.com/aws/en/agents/deploy/agent-server).
