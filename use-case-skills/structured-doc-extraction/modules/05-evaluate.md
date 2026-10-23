# 05 · Evaluate

Measure extraction quality: profile confidence across the corpus, then, with labeled documents, score accuracy against them, log the result to MLflow, and gate on thresholds.

## Inputs → outputs

- **Inputs:** `<prefix>_fields` and `<prefix>_extracted` (module 03), `warehouse_id`, and labeled documents if the user has or can make them.
- **Outputs:** a confidence profile and, with labels, an MLflow run with per-field metrics, per-document scores, and a calibration table, plus a pass/fail result.

## 1. Confidence profile (no labels needed)

```sql
SELECT doc_type, field, count(*) AS n,
       round(percentile(confidence, 0.1), 2) AS p10, round(percentile(confidence, 0.5), 2) AS median,
       count_if(confidence < <review_threshold>) AS below_threshold
FROM <catalog>.<schema>.<prefix>_fields
WHERE confidence IS NOT NULL AND field NOT LIKE '%[%'
GROUP BY doc_type, field ORDER BY below_threshold DESC LIMIT 30;
```

Confidence tells you where the model was unsure, not whether it was right. Report the profile, and treat it as triage rather than a quality measure.

## 2. Labels

- **The user has labels:** convert them to JSON Lines in `<workdir>/ground_truth.jsonl`, one document per line: `{"file_name": "...", "doc_type": "...", "fields": {...}}`. `fields` uses the type's schema shape, with nested objects and arrays. For packets, add `"page"`: the 1-based number of any page in that sub-document.
- **No labels:** offer to label 10–20 documents per type with the user. Start each record from the extracted values, and have the user correct them; don't let them copy values unread. Without labels, stop after step 1, and report that accuracy is unmeasured.
- **Leave a field out when it isn't labeled. Set it to `null` when the document truly lacks it.** Only labeled fields are scored, so partial labels are fine.

Record an MLflow experiment path as `experiment` in `extraction.json`, such as `/Users/<user>/<name>-extraction-eval`. If you change `evaluate.py`, **load** `databricks-mlflow-evaluation` `references/CRITICAL-interfaces.md` first (judges and logging).

## 3. Run

```bash
uv run <skill>/scripts/evaluate.py --state <workdir>/extraction.json --ground-truth <workdir>/ground_truth.jsonl
```

`evaluate.py` reads the predictions for the labeled files through the SQL warehouse and scores each labeled field by its schema type:

| Type | Correct when | Metric |
|---|---|---|
| string | Equal after lowercasing and collapsing spaces; otherwise an LLM judge (`is_correct`) decides. `--no-judge` skips the judge | accuracy |
| number, integer | Numerically equal (`1884` = `1884.0`) | accuracy |
| boolean, enum | Equal | accuracy |
| object | Mean of its labeled fields | object score |
| array | Optimal one-to-one item matching; precision and recall over the matched scores | soft F1 |

| Threshold | Floor | Applies when |
|---|---|---|
| `classification_accuracy` | ≥ 0.95 | Labels include `doc_type` |
| `field_accuracy` | ≥ 0.9 | Always: the mean over all labeled scalar values |
| `array_soft_f1` | ≥ 0.8 | Labels include arrays |

The script prints the run ID, the metrics, and a calibration table (correctness rate per confidence bin), and it exits with status 1 when a threshold is missed.

## 4. Tune: change one thing at a time

| Failing signal | Look at | Change |
|---|---|---|
| One field fails across documents | `per_field.json` in the run | Sharpen its description or format in the schema (module 03) |
| Classification misses | `documents.json`: rows whose `doc_type` differs | Descriptions of the confused types (module 02) |
| Array soft F1 low | Item counts, predicted vs. labeled | Describe what counts as one item, and whether to include empty or subtotal rows |
| Long documents miss fields | Scores by document size | `"mode": "precision"` for the type |
| High confidence but wrong | Calibration: high bins with a low correct rate | Raise `review_threshold`, so more values go to people |

Changes to schemas or descriptions need a re-extraction with `full_refresh=true` (module 03, step 4), which re-bills. Confirm first, then rerun this module.

## Verify

- The confidence profile was reported.
- With labels: the thresholds are met, the run ID is reported, and `ground_truth.jsonl` is saved in the working directory, so later runs and the Forge certification run can reuse it.
- If calibration isn't monotonic, you told the user that confidence alone isn't a safe filter for this corpus.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no predicted segment` for a labeled document | `file_name` doesn't match `<prefix>_parsed.file_name`, or a packet label lacks `page` |
| Judge errors or timeouts | Rerun with `--no-judge` for exact matching, and report that strings were scored strictly |
| `PERMISSION_DENIED` reading `<prefix>_extracted` | The warehouse runs as you; check `SELECT` on the table and `CAN USE` on the warehouse |
| Unlabeled fields counted as wrong | They're present in `fields` as `null` or `""`; remove the key instead |
