# Draft change proposals for databricks-agent-skills

Drafts for the base-skill gaps that `skills/forge-doc-qa-agent` works around, in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Checked against upstream v0.2.29. Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in `ucs.yaml`.

The Knowledge Assistant deprecation is left out, because there's no public notice to cite yet. The ingestion and index gaps (the `ai_prep_search` `schema` option, the AI Search rename, the embedding model) moved to `unstructured-doc-extraction-upstream-proposals.md` with that skill; the AI Search rename applies to this skill too.

---

## 1. Retire Model Serving agent deployment patterns

**Problem or opportunity.** `databricks-ml-training/references/genai-agents.md` deploys LangGraph agents with `agents.deploy()` to Model Serving, and `databricks-vector-search/references/end-to-end-rag.md` builds a `ChatAgent`. Agent Runtime on Databricks Apps is now the documented runtime for custom agents.

**Proposed change.** Mark both patterns as legacy, and point to Agent Runtime on Databricks Apps, plus the migration guide for existing Model Serving agents.

**Affected skill or area.** `databricks-ml-training` (`references/genai-agents.md`), `databricks-vector-search` (`references/end-to-end-rag.md`).

**Additional context.** [Agent Runtime](https://docs.databricks.com/aws/en/agents/deploy/agent-runtime), [Migrate an agent to Apps](https://docs.databricks.com/aws/en/agents/custom-agents/migrate-agent-to-apps).

---

## 2. Cover the Agent Bricks CLI

**Problem or opportunity.** No skill covers the Agent Bricks CLI: `agentbricks init`, `agent.toml`, DurableAgentServer, Agent Runtime, or managed sessions and memory. Agents fall back to the Model Serving patterns above.

**Proposed change.** Add a skill, or a reference in an existing agent skill, covering the init → dev → doctor → deploy loop, `agent.toml` resources, session and memory stores, and how deployed agents are queried.

**Affected skill or area.** New skill, or `databricks-agent-bricks`.

**Additional context.** [Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli), [Agent memory and sessions](https://docs.databricks.com/aws/en/agents/custom-agents/stateful-agents), [Agent Server](https://docs.databricks.com/aws/en/agents/deploy/agent-server).
