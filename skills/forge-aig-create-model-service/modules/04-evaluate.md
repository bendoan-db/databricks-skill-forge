# 04 · Evaluate

Check that what the YAML files describe is in place and working: each service matches its file and answers through the gateway, and each grant is in effect.

## Inputs → outputs

- **Inputs:** the services and grants from module 03, the YAML files, and `test_prompt` (optional) from `aig-model-services.json`.
- **Outputs:** a pass/fail report against the thresholds, with the services and grants that missed.

## 1. Wait, then confirm the cost

Policy changes take a minute or two to take effect, and grants a few minutes, so wait about two minutes after module 03. Then confirm the cost: one short test prompt per service, billed like any request, plus a call to the evaluator of each LLM-based policy on it.

## 2. Run

```bash
uv run <skill>/scripts/evaluate.py --state <workdir>/aig-model-services.json
```

| Metric | Measures | Threshold |
|---|---|---|
| `config_match_rate` | Share of the services in `model-services.yaml` that match it and `service-policies.yaml`, from the read-only plan | ≥ 1.0 |
| `answer_rate` | Share of those services that answer the test prompt through `/ai-gateway/mlflow/v1/chat/completions` | ≥ 1.0 |
| `grant_rate` | Share of the grants `permissions.yaml` needs that are in effect, from the read-only plan | ≥ 1.0 |

A metric whose part isn't in `parts` is left out. A reply blocked by a service policy counts as an answer: the service works, and the policy acted. A policy that fails to evaluate doesn't count: the gateway denies every request it checks (fail closed). Rate limits, and a new service that isn't ready, are retried. The script prints the report and exits 1 when a metric is below its threshold.

## 3. Show the user how to use it

Report each service's three-part name, its policies, and who can use it. To call one from code, **load** `databricks-unity-gateway` `references/querying.md` (the gateway's chat completions and Responses APIs, and the OpenAI client), and use the three-part name as `model`. Two limits to tell them:
- `ai_query` in SQL supports only Databricks' `system.ai` model services, not services you create.
- With an output-phase policy attached, a streamed response arrives all at once.

## Verify

- `evaluate.py` exits 0: every metric reported meets its threshold.
- You reported the services, their policies, and who can use each.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `config_match_rate` below 1.0 | `not_matching` lists the services and fields. The files changed since module 03, or an apply failed: plan again (module 02), then apply |
| A service doesn't answer: `NotFound` | It wasn't created, or isn't ready yet. Check `applied` in module 03's output, wait, and rerun |
| A service doesn't answer: `PERMISSION_DENIED` | The service's owner must keep `EXECUTE` on its destinations; queries run with the owner's privileges. Check `check_access.py --config` |
| `failed to evaluate` on every prompt | The policy's evaluator can't answer, for example a missing `EXECUTE` on it or a timeout. Set the policy to `mode: log` while you find out, then apply again |
| `grant_rate` below 1.0 | `grants_not_in_effect` lists them. Run `grant_access.py --apply` (module 03, step 3), and wait a few minutes |
| A principal gets `PERMISSION_DENIED` querying, though its grants are in effect | Model Serving `CAN_QUERY` doesn't grant access to a model service. The caller needs `EXECUTE` and must call the three-part name through the gateway. **Load:** `databricks-unity-gateway` `references/permissions.md` |
