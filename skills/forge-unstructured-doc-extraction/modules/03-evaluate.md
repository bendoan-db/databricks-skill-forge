# 03 · Evaluate retrieval

Measure how often a search returns the document and page that answer a question, before an agent or app relies on the index.

## Inputs → outputs

- **Inputs:** the index `<catalog>.<schema>.<index_name>` (module 02), plus `eval_set`, `warehouse_id`, and `corpus_description` from `doc-index.json`.
- **Outputs:** `<workdir>/evals.jsonl`, an MLflow run with a row per question, and a pass/fail result against the thresholds.

## 1. Eval set

- **The user has questions with known sources:** convert them to JSON Lines, one line per question: `{"question": ..., "file_name": ..., "pages": [N]}`. `file_name` is the file's name without its path, and `pages` (1-based) is optional. Keep any `expected_response` or `expected_facts`: `forge-doc-qa-agent` grades answers with them. If the conversion isn't straightforward, **load** `databricks-mlflow-evaluation` `references/patterns-datasets.md`.
- **No eval set:** synthesize one (step 2, `--synthesize 30`). `evaluate.py` samples chunks, writes a question and expected facts from each with `generate_evals_df`, and labels each question with its chunk's file and pages. Tell the user that synthetic questions are a starting point: each comes from a single passage, so they're easier than real questions. A domain expert should review and extend them.

A row can carry `filters` (for example `{"fiscal_year": 2022}`) to test a filtered search. Rows without `file_name`, such as questions the corpus can't answer, are skipped here.

## 2. Run

Record an MLflow experiment path as `eval_experiment` in `doc-index.json`, such as `/Users/<user>/<name>-retrieval-eval`. Confirm the cost first: one search per question, plus one LLM call per synthetic question.

```bash
uv run <skill>/scripts/evaluate.py --state <workdir>/doc-index.json --eval-file <workdir>/evals.jsonl [--synthesize 30]
```

| Metric | Measures | Threshold |
|---|---|---|
| `doc_hit_rate` | Share of questions where one of the top `k` results comes from the expected file | ≥ 0.9 |
| `page_hit_rate` | Share of questions with `pages` where such a result also covers an expected page | ≥ 0.8 |
| `doc_mrr` | Mean of 1 / rank of the first result from the expected file | reported |

Searches are hybrid, like `forge-doc-qa-agent`'s tool, and unfiltered unless the row has `filters`, so the index is measured without help from the model. `k` defaults to 8, the number of results `forge-doc-qa-agent` retrieves; pass `--k` to match another consumer. The script runs as your CLI identity, which needs `SELECT` on the index. It prints the run ID and metrics, and exits with status 1 when a threshold is missed. If you change how it queries the index, **load** `databricks-vector-search` `references/search-modes.md` first.

## 3. Tune: change one thing at a time and re-run

The run's `questions.json` table shows each question's rank and the files it retrieved.

| Failing signal | Look at | Change |
|---|---|---|
| Right file, wrong pages | The expected page's text in `<prefix>_parsed` | Text missing: parsing failed on that page (module 01, Troubleshooting). Text present in a table or figure: keep `description_element_types` at `*` |
| A similar document wins (another year, version, or entity) | The retrieved files | Add the field that tells them apart (such as `fiscal_year`) to `metadata_schema` (module 02, step 1), so consumers can filter, and add `filters` to the rows that name it |
| Questions about chart values miss | The chunks for the chart's page | Turn on `chart_insights` (module 02, step 3) and re-chunk with `full_refresh=true` |
| Misses across the board | `chunk_to_retrieve` for a few missed files | Little or garbled text: parsing failed (module 01, Troubleshooting). Documents not in English on `databricks-gte-large-en`: switch to `databricks-qwen3-embedding-0-6b`, which means rebuilding the index (confirm first) |

After a change passes, re-run the eval with the same `evals.jsonl`, so the numbers are comparable.

## Verify

- The thresholds are met, and the run ID is reported to the user.
- If any question missed, you looked at one, and its cause matches a row in the tuning table.
- `evals.jsonl` is in `<workdir>`, so later runs, the Forge certification run, and `forge-doc-qa-agent` can reuse it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no row has a file_name` | The eval set has no source labels; add `file_name` to each question, or synthesize |
| Synthetic questions have no `file_name` | `generate_evals_df` returned no `expected_retrieved_context`; label a few questions by hand, and report it to the skill owner |
| `PERMISSION_DENIED` on the query | Your identity needs `SELECT` on `<catalog>.<schema>.<index_name>`; for other errors, **load** `databricks-vector-search` `references/troubleshooting-and-operations.md` |
| `databricks-agents` won't install | `uv` installs the script's dependencies from its header; behind a package mirror, set `UV_INDEX_URL` |
