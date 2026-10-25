# 03 · Evaluate

Measure answer quality with MLflow GenAI judges, gate on thresholds, and tune until they pass.

Evaluate the agent **locally** by importing it, as `databricks-mlflow-evaluation` recommends, not through the deployed app. `evaluate.py` builds the same graph the app serves, through `create_agent_graph`. It runs as your identity and records full traces. Before changing `evaluate.py`, **load** `databricks-mlflow-evaluation` and read `references/GOTCHAS.md` and `references/CRITICAL-interfaces.md`.

## Inputs → outputs

- **Inputs:** the `<name>/` project, and `eval_set` and `experiment` from `doc-qa.json`.
- **Outputs:** an MLflow evaluation run with per-question traces and judge feedback, plus a pass/fail result against the thresholds.

## 1. Eval set

- **Default:** `<workdir>/evals.jsonl`, written by `forge-unstructured-doc-extraction` module 03. Synthetic rows carry `expected_facts`. If its questions were synthetic, remind the user that a domain expert should review and extend them.
- **The user has their own:** convert it to JSON Lines, one line per question: `{"question": ..., "expected_response": ...}`. `expected_facts: [...]` also works and is easier to grade. If the conversion isn't straightforward, **load** `databricks-mlflow-evaluation` `references/patterns-datasets.md`.
- **Either way,** copy it to `<workdir>/<name>/evals.jsonl`, and add 3–5 questions the corpus can't answer, with `"expected_response": "The documents do not contain this information."`. Groundedness doesn't catch made-up answers to out-of-scope questions; Correctness does. Leave the index's copy unchanged.

## 2. Run

```bash
cp <skill>/scripts/evaluate.py <workdir>/<name>/
cd <workdir>/<name>
uv run python evaluate.py --experiment "<experiment>" --eval-file evals.jsonl
```

| Scorer | Checks | Threshold |
|---|---|---|
| `RetrievalGroundedness` | Claims are supported by the retrieved passages (needs the RETRIEVER span, which `search_documents` emits) | ≥ 0.8 |
| `RelevanceToQuery` | Answer addresses the question | ≥ 0.8 |
| `Correctness` | Matches `expected_response` / `expected_facts` (only when every row has expectations) | ≥ 0.6 |
| `Safety` | No harmful content | reported |
| `Guidelines(cites_sources)` | Every claim carries `[file_name, p. N]`, or the answer says it isn't in the documents | reported |

The script prints the run ID and metrics, and exits with status 1 when a threshold is missed.

## 3. Tune: change one thing at a time and re-run

| Failing signal | Look at | Change |
|---|---|---|
| Low groundedness | Traces: is the answer using passages at all? | Tighten the prompt's "only from documents" rule; lower temperature if you set one |
| Correct facts missing from retrieval | Retriever span inputs/outputs | Raise `num_results` in `doc-qa.json` (8 → 12) and render again (module 01, step 2). If the index's retrieval eval (`forge-unstructured-doc-extraction` module 03) is below its thresholds, fix the index there first |
| Wrong filters (zero hits) | `filters` argument in the tool span | Normalize metadata casing in the schema descriptions and regenerate `filter_values` (`forge-unstructured-doc-extraction` module 02), then render again |
| Right passages, wrong numbers | Final answer vs passage | Prompt: show arithmetic, quote units; consider a stronger `MODEL` |
| Multi-hop questions fail | Number of `search_documents` calls | Prompt to decompose first; if still failing, add the planner node (module 01) |
| Questions about chart values fail | Retrieved chunk for the chart's page | Turn on `chart_insights` (`forge-unstructured-doc-extraction` module 02, step 3) and re-chunk with `full_refresh=true` |
| Noisy context (table summaries, related questions) | Tool output | Add a `DatabricksReranker(columns_to_rerank=["chunk_to_embed"])` via the tool's `reranker=` argument in `agent/retrieval.py`. That's a hand edit, so later renders need `--force` and re-adding it |

To compare runs, use the experiment UI, or **load** `databricks-mlflow-evaluation` `references/patterns-evaluation.md` (named runs). After a change passes, redeploy with `agentbricks deploy` (module 02).

## 4. Smoke-check the deployed app

Send 3–5 eval questions to the deployed app with `agentbricks endpoint invoke` (module 02, step 3). Confirm that its answers match the local ones. If they diverge, the usual cause is the index grant, or a model or constant that differs between environments.

## 5. Production monitoring (optional)

Deployed traces go to the same experiment. To run scorers continuously on production traffic, **load** `databricks-mlflow-evaluation` `references/patterns-trace-ingestion.md` and follow patterns 12–13. Register `RetrievalGroundedness` and the citation `Guidelines` with sampling.

## Verify

- The thresholds are met, and the run ID is reported to the user.
- You reviewed at least one failing trace (if there was one), and the cause matches a row in the tuning table.
- The eval set is saved next to the project (`evals.jsonl`), so later runs and the Forge certification run can reuse it.
