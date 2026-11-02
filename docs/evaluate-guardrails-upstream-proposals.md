# Draft change proposals for databricks-agent-skills (forge-evaluate-guardrails)

Drafts for the base-skill gaps that `skills/forge-evaluate-guardrails` works around, in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Checked against upstream v0.2.40 on 2026-11-02. Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in the skill's `ucs.yaml`.

---

## 1. Cover guardrails (service policies) on model services

**Problem or opportunity.** `databricks-unity-gateway` covers creating, querying, and granting model services. It doesn't cover the guardrails attached to them. It mentions "guardrails implemented with service policies" only in `references/querying.md`, where it says `ai_query` doesn't apply them. The [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) describe the four built-in policies (`system.ai.detect_sensitive_data`, `system.ai.block_unsafe_content`, `system.ai.block_jailbreak`, `system.ai.block_hallucination`), how ranks order them, the fail-closed rule, Log mode, and the HTTP 200 response with a `databricks_service_policy` object that a blocked call returns. Agents asked to add guardrails to a model service, or to test them, have nothing to load.

**Proposed change.** Add `references/service-policies.md`, with these sections:
- **Built-ins:** what each detects; which phases it supports on model services; its evaluator, and the `EXECUTE` grant that evaluator needs.
- **Sensitive Data Detection:** its 15 classification tags, and the block and redact actions.
- **Ordering:** ranks and the stop-at-first-deny rule, the fail-closed rule, and Log mode.
- **What a caller sees:** a blocked response, and how to tell a block from an error.
- **Setup:** the Service Policies preview a workspace admin enables, and how long a change takes to propagate.
- **Attaching by API:** the `config.service_policies` field, if it's meant to be public.

Link to it from SKILL.md's Service Types table.

**Affected skill or area.** `databricks-unity-gateway` (`SKILL.md`, a new reference file).

**Additional context.**
- [Service policies for AI securables](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) and [Create and attach a service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy) (both updated 2026-10-29).
- [Detect sensitive data](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data) (updated 2026-10-19).

The docs show attaching only in the UI. The PATCH on `config.service_policies` with `update_mask=config.service_policies` worked in a workspace on 2026-10-01 (github.com/bendoan-db/uaig-guardrail-evals). Before filing, confirm whether that API is documented, and name it only if it is.

---

## 2. Say how to read a model service's service policies

**Problem or opportunity.** `references/model-services.md` reads a service with `databricks ai-gateway get-model-service`. In CLI 1.18.0, that command's output leaves out `config.service_policies`, while `databricks api get /api/2.1/unity-catalog/model-services/<name>` returns them. An agent that checks a service's guardrails with the typed command concludes there are none.

**Proposed change.** In the Read section, note that the typed command omits service policies, and show the `databricks api get` read. Drop the note if a newer CLI includes the field.

**Affected skill or area.** `databricks-unity-gateway` (`references/model-services.md`).

**Additional context.** Observed with CLI 1.18.0 on 2026-10-01. Before filing, recheck on the current CLI (1.20.0 here).
