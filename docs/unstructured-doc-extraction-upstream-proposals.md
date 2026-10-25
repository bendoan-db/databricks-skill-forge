# Draft change proposals for databricks-agent-skills (forge-unstructured-doc-extraction)

Drafts for the base-skill gaps that `skills/forge-unstructured-doc-extraction` works around (and, for proposal 2, `forge-doc-qa-agent` too), in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Proposals 1–3 moved here from `doc-qa-agent-upstream-proposals.md` when the skill was split out of `doc-qa-agent` 0.3.0; they were checked against upstream v0.2.29, and proposal 4 against v0.2.34. Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in each `ucs.yaml`.

The Knowledge Assistant deprecation is left out, because there's no public notice to cite yet. Gaps shared with `forge-structured-doc-extraction` (`ai_classify` version 2.1, PDFs over 500 pages) are drafted in `structured-doc-extraction-upstream-proposals.md`.

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

## 4. Cover synthetic evaluation sets

**Problem or opportunity.** No base skill mentions `generate_evals_df`. `databricks-mlflow-evaluation/references/patterns-datasets.md` builds datasets by hand or from traces, so agents without labeled questions skip retrieval and answer evaluation, or write their own question generators.

**Proposed change.** Add a pattern for `databricks.agents.evals.generate_evals_df`: its input (`content`, `doc_uri`), its MLflow 3 output (`inputs`, `expectations.expected_facts`, `expectations.expected_retrieved_context`), and how to use `expected_retrieved_context` to score retrieval as well as answers.

**Affected skill or area.** `databricks-mlflow-evaluation` (`references/patterns-datasets.md`).

**Additional context.** [Synthesize evaluation sets](https://docs.databricks.com/aws/en/generative-ai/agent-evaluation/synthesize-evaluation-set) (updated 2026-09-11).

---

## 5. Document AI Search endpoint permissions

**Problem or opportunity.** `databricks-vector-search/references/troubleshooting-and-operations.md` says permission errors on create mean the user "needs `CREATE TABLE` on the schema and `USE CATALOG`/`USE SCHEMA` privileges". Those Unity Catalog privileges govern indexes. Endpoints have their own access-control list (`CAN CREATE`, `CAN USE`, `CAN MANAGE`), and no skill mentions it, so agents can't tell a user why endpoint creation failed, or that an index needs `CAN USE` on its endpoint.

**Proposed change.** Separate the two in the troubleshooting table, add the endpoint permission levels and what each allows, and show `databricks vector-search-endpoints get-permissions` for checking access to an existing endpoint.

**Affected skill or area.** `databricks-vector-search` (`SKILL.md`, `references/troubleshooting-and-operations.md`).

**Additional context.** [Access control lists](https://docs.databricks.com/aws/en/security/auth/access-control/) (updated 2026-10-20), AI Search endpoint ACLs. The docs don't say who holds `CAN CREATE` by default, or how to check it without creating an endpoint; ask the docs owner before filing.

