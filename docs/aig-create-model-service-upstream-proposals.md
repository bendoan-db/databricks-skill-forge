# Draft change proposals for databricks-agent-skills (forge-aig-create-model-service)

Drafts for the base-skill gaps that `skills/forge-aig-create-model-service` works around, in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Proposals 1 and 2 (service policies, and reading them) are in `evaluate-guardrails-upstream-proposals.md`; this skill's `coverage_gaps` point at them too. Checked against upstream v0.2.45 on 2026-11-10. Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in `skills/forge-aig-create-model-service/ucs.yaml`.

---

## 3. Say that a system.ai destination is the model, not its model service

**Problem or opportunity.** `references/model-services.md` shows a pay-per-token destination as `"pay_per_token_config": {"model": "models/<catalog>.<schema>.<model>"}`, but doesn't say which name that is for a Databricks-hosted model. In `system.ai`, a model and the model service of the same model have different names: the model usually starts with `databricks-` and its service doesn't (`system.ai.databricks-claude-sonnet-5-5` and `system.ai.claude-sonnet-5-5`), and some are the same (`system.ai.gpt-oss-120b`). An agent that copies the service name it uses for querying into a destination gets a create that fails. Policy evaluators take the model service's name, the other way around.

**Proposed change.** In the Destination types section, add one sentence and a lookup: a `system.ai` destination is the Unity Catalog model, found with `databricks registered-models list --catalog-name system --schema-name ai`, or from an existing service's `config.routing.destinations[].pay_per_token_config.model`.

**Affected skill or area.** `databricks-unity-gateway` (`references/model-services.md`).

**Additional context.** From unity-ai-gateway-utilities (2026-11), where it was the most common configuration mistake. Before filing, confirm the naming in a workspace, and whether the docs' supported-models page lists the model names.

---

## 4. Cover custom service policies: LLM judges and SQL functions

**Problem or opportunity.** Proposal 1 covers the built-in policies. Custom policies are documented too: an LLM judge with a plain-language instruction, and a SQL policy, a Unity Catalog function `(event VARIANT) RETURNS VARIANT` that returns `ALLOW` or `DENY` and that the gateway transpiles to CEL. Some SQL can't be transpiled, such as `ai_query`, subqueries, `BETWEEN`, aggregates, and `COALESCE` or `CONCAT` with more than two arguments. A comparison on a missing field fails closed. An agent writing one without that guidance gets an attach that fails, or a policy that denies everything.

**Proposed change.** In the proposed `references/service-policies.md`, add a Custom policies section covering:
- the LLM-judge instruction;
- the SQL function's signature, with a minimal example;
- the constructs the function reference supports;
- wrapping event fields in `CAST(COALESCE(event:<path>, '') AS STRING)`;
- testing the expression with a `SELECT` on a sample event before attaching it.

**Affected skill or area.** `databricks-unity-gateway` (the proposed `references/service-policies.md`).

**Additional context.** [Policy function reference](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/policy-function-reference). The unsupported constructs are from unity-ai-gateway-utilities (2026-11), observed as "SQL cannot be transpiled to CEL"; before filing, check them against the reference.
