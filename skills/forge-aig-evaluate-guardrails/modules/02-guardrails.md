# 02 · Guardrails

Check the model services under evaluation before the job sends them anything: each has exactly the policies its entry lists, every policy blocks, nobody else uses it, and a plain prompt passes while one with an SSN is blocked.

**Docs:** [Service policies for AI securables](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/), [Create and attach a service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy), [Detect sensitive data](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data).

## Inputs → outputs

- **Inputs:** `model_services` from `guardrail-eval.json`, and `<workdir>/<job_name>/src/eval_config.json` from module 01, which lists each service's policies as `forge-aig-create-model-service`'s `model-services.yaml` does.
- **Outputs:** every model service checked and probed, with nothing left to fix. This module changes nothing.

## The guardrails

The evaluation's own definitions of the built-ins, from module 00, step 7:

| Policy | Type (`service-policies.yaml`) | Built-in | Checks | Evaluator |
|---|---|---|---|---|
| `eval_sensitive_data` | `detect_sensitive_data` | `system.ai.detect_sensitive_data` | requests and responses, for 15 kinds of personal data (email and IP addresses, card, bank, and IBAN numbers, US SSN, ITIN, and passport, phone numbers, and UK and Indian IDs), by pattern and checksum | none |
| `eval_unsafe_content` | `block_unsafe_content` | `system.ai.block_unsafe_content` | requests and responses, for unsafe or harmful content | its own, `system.ai.gemma-3-12b` by default |
| `eval_jailbreak` | `block_jailbreak` | `system.ai.block_jailbreak` | requests, for jailbreaks and prompt injection | `default_evaluator_model` |

Custom policies are evaluated the same way: an `llm_judge` runs `system.ai.invoke_llm_judge` on its evaluator with the user's instruction, and a `sql` policy is a Unity Catalog function in the services' schema. A request goes through a service's policies in rank order, and the first that denies it blocks it, so a later policy only sees what earlier ones allowed. The evaluation reads each verdict from the blocked response, which is why every policy must block: one that only logs, or redacts, leaves no verdict to read. The LLM-judge policies look at the last 10 turns, which for a one-prompt request is the prompt.

## Steps

1. **Check the services.** **Load:** `databricks-unity-gateway` `references/permissions.md` (who holds privileges on a model service, and how they're inherited). Then:

   ```bash
   uv run <skill>/scripts/check_services.py --state <workdir>/guardrail-eval.json
   ```

   For each service, it reads the service and who holds privileges on it, changes nothing, and prints its `problems` and `warnings`. It exits 1 on any problem:
   - the service doesn't exist, or its live policies aren't the ones its entry lists;
   - a policy only logs (`dry_run`), or redacts instead of blocking;
   - someone else holds a privilege on the service itself, so other people use it.

   Warnings: someone else can call it through a grant on its schema or catalog, another user owns it, or it keeps an inference table or rate limits. Tell the user each one.

   To fix a problem, change the services with `forge-aig-create-model-service`: edit its files with the user (its module 01, step 2), then plan and apply (its modules 02 and 03). Then render here again (module 01, step 1), deploy, and check again.

2. **Probe them,** a minute or two after the services last changed: policy changes take that long to take effect.

   ```bash
   uv run <skill>/scripts/check_services.py --state <workdir>/guardrail-eval.json --probe
   ```

   It also sends each service two prompts through the gateway, the way an application would (**Load:** `databricks-unity-gateway` `references/querying.md`, the unified chat completions API): a plain question, which must pass, and, when the service has a blocking Sensitive Data Detection policy that covers US Social Security numbers, a prompt with one, which that policy must block. A blocked call comes back as HTTP 200, with a `databricks_service_policy` object naming the policy and its reason.

## Verify

```bash
databricks api get /api/2.1/unity-catalog/model-services/<service> --profile <profile> | jq '[.config.service_policies[] | select(.is_deleted | not) | {name, rank, handler, dry_run: .options.dry_run}]'
```

In a notebook, read the same with `w.api_client.do("GET", "/api/2.1/unity-catalog/model-services/<service>")`, and pick those fields in Python.

- For each service in `model_services`, the live policies are the ones its entry in `model-services.yaml` lists, in rank order, and none is a dry run.
- `check_services.py --probe` exits 0. If an SSN prompt passed, wait a few minutes and run it again before changing anything.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doesn't exist` | `forge-aig-create-model-service` hasn't applied it: resume that skill at its module 03 |
| `has the policies [...], not [...]` | The service changed since it was applied, or the files did. Plan and apply with `forge-aig-create-model-service` (its modules 02 and 03), then render here again |
| `only logs (mode: log)` or `redacts instead of blocking` | Its verdicts can't be read from the reply. Take the policy off the evaluation's service, or give the service a blocking definition of its own, named `eval_...` |
| `holds ... on it, so other people use it` | The service is shared. Evaluate a copy instead (module 00, step 1). Revoking someone's access is for its owner to decide, through `forge-aig-create-model-service` (its module 03, Removing access or a service) |
| `can call it through a grant on its schema or catalog` | They're not told about it, and only the evaluation calls it. If they might, put the evaluation's services in a schema of their own |
| `who else can call it couldn't be read` | The user can't read the service's grants. Ask its owner to confirm nobody else has been granted it |
| The policies update failed in the model services skill | The Service Policies preview isn't on, or attaching by API changed: its module 03 Troubleshooting attaches them in the UI instead |
| `--probe`: the SSN prompt wasn't blocked | Policies take a minute or two to propagate; run it again. If it still passes, read the policies (Verify) |
| `--probe`: the plain question was blocked | Read the policy and reason it printed. A fail-closed block (`failed to evaluate`) means the evaluator errored: try again, or another evaluator (SKILL.md, Decision points) |
