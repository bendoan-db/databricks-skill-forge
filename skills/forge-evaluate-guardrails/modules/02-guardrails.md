# 02 · Guardrails

Create the evaluation's own model service, attach the chosen built-in guardrails to it, and check one prompt passes and one is blocked.

**Docs:** [Service policies for AI securables](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/), [Create and attach a service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy), [Detect sensitive data](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data).

## Inputs → outputs

- **Inputs:** `model_service`, `base_model_service`, `guardrails`, and `evaluators` from `guardrail-eval.json`.
- **Outputs:** model service `<model_service>`, routed like `<base_model_service>`, whose service policies are exactly the chosen guardrails.

## The guardrails

| Rank | Policy | Built-in | Checks | Evaluator |
|---|---|---|---|---|
| 1 | `sensitive_data_detection` | `system.ai.detect_sensitive_data` | requests and responses, for 15 kinds of personal data (email and IP addresses, card, bank, and IBAN numbers, US SSN, ITIN, and passport, phone numbers, and UK and Indian IDs), by pattern and checksum | none |
| 2 | `unsafe_content_detection` | `system.ai.block_unsafe_content` | requests and responses, for unsafe or harmful content | `evaluators.unsafe_content` |
| 3 | `jailbreak_detection` | `system.ai.block_jailbreak` | requests, for jailbreaks and prompt injection | `evaluators.jailbreak` |

A request goes through them in rank order, and the first that denies it blocks it, so a later guardrail only sees what earlier ones allowed. All of them enforce (no Log mode): the evaluation reads each verdict from the blocked response. The LLM-judge guardrails look at the last 10 turns, which for a one-prompt request is the prompt.

## Steps

1. **Plan.** **Load:** `databricks-unity-gateway` `references/model-services.md` (resource names, routing, and the privileges a model service needs). Then:

   ```bash
   uv run <skill>/scripts/guardrail_service.py --state <workdir>/guardrail-eval.json
   ```

   It reads the model service and the base model service, changes nothing, and prints the plan: create the service (routed like the base), or update its routing, and attach, keep, update, or detach each policy. Its policies become exactly the chosen guardrails, so a policy on it that isn't one of them is detached. Show the plan to the user.

2. **Apply, after the user confirms:**

   ```bash
   uv run <skill>/scripts/guardrail_service.py --state <workdir>/guardrail-eval.json --apply
   ```

   It creates or updates the service, attaches the policies through its `config.service_policies` field, then reads the service back and fails if the policies differ from the plan.

3. **Check it,** a minute or two later: policy changes take that long to take effect.

   ```bash
   uv run <skill>/scripts/guardrail_service.py --state <workdir>/guardrail-eval.json --check
   ```

   It sends two prompts through the gateway, the way an application would (**Load:** `databricks-unity-gateway` `references/querying.md`, the unified chat completions API): a plain question, which must pass, and one with a US SSN, which `sensitive_data_detection` must block when it's attached. A blocked call comes back as HTTP 200, with a `databricks_service_policy` object naming the policy and its reason.

## Verify

```bash
databricks api get /api/2.1/unity-catalog/model-services/<model_service> --profile <profile> | jq '[.config.service_policies[] | select(.is_deleted | not) | {name, rank, handler}]'
```

- The live policies are the chosen guardrails, in rank order, and nothing else.
- `--check` exits 0. If the SSN prompt passed, wait a few minutes and run it again before changing anything.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The policies PATCH fails with a feature or preview error | The Unity Gateway Service Policies preview isn't on: a workspace admin enables it on the Previews page |
| The policies PATCH is rejected though the preview is on | Attaching by API isn't documented, and may have changed. Attach the same policies in the UI instead (AI Gateway, the model service, Policies, New policy), with the ranks, phases, and evaluators in the table above, then run `--check` |
| `PERMISSION_DENIED` creating the service | `CREATE SERVICE` on the schema is missing. **Load:** `databricks-unity-gateway` `references/permissions.md` |
| `PERMISSION_DENIED` attaching a policy, or the evaluator is rejected | The user needs `MANAGE` on the service (they have it as its owner) and `EXECUTE` on each evaluator, with `USE CATALOG`/`USE SCHEMA` on its schema |
| `base_model_service ... isn't a model service you can read` | Use one from `databricks ai-gateway list-model-services --parent schemas/system.ai --profile <profile>` |
| `--check`: the SSN prompt wasn't blocked | Policies take a minute or two to propagate; run it again. If it still passes, read the policies (Verify) |
| `--check`: the plain question was blocked | Read the policy and reason it printed. A fail-closed block (`failed to evaluate`) means the evaluator errored: try again, or another evaluator (SKILL.md, Decision points) |
| The service already exists with other policies | It may be someone else's: never attach to a service other people call. Pick another `prefix` or `model_service`, and plan again |
