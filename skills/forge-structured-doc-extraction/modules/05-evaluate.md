# 05 · Evaluate

Measure extraction quality: profile confidence across the corpus, then, with labeled documents, score accuracy against them, log the result to MLflow, and gate on thresholds.

## Inputs → outputs

- **Inputs:** `<prefix>_fields` and `<prefix>_extracted` (module 03), `warehouse_id`, and labeled documents if the user has or can make them.
- **Outputs:** a confidence profile and `review_cutoffs` in `extraction.json`; with labels, an MLflow run with per-field metrics, per-document scores, and a calibration table, plus a pass/fail result.

## 1. Confidence profile and review cutoffs (no labels needed)

`ai_extract` scores often come in a few levels (such as 0.95, 0.98, and 1.0) with rare low outliers, so a fixed `review_threshold` can flag almost nothing. Profile the scores, then raise the threshold per document type to a level observed in this corpus:

```bash
uv run <skill>/scripts/confidence_profile.py --state <workdir>/extraction.json
```

It reads `<prefix>_fields` through the warehouse and prints, per type: values by state (`scored`; `unscored`, a value without a score; `absent`, no value), the confidence levels and their counts, and a suggested cutoff: the 5th-percentile level (`--pct`), flagged strictly below. It also lists the fields with the most values to review under those cutoffs. A type with fewer than 200 scored values (`--min-scored`) gets the corpus-wide level (`cutoff_source: corpus`), and with fewer than that in the whole corpus, no cutoff (`floor`). It changes nothing.

1. Show the user the profile and `review_below` per type: the threshold the queue will use, `max(review_threshold, cutoff)`. A cutoff only raises the floor, so calibration never queues fewer values than before. With `discrete: false` (more than 20 levels, common on scans), the scores spread like probabilities, and the percentile is still a fair cutoff; say so.
2. With the user's go-ahead, record the printed `review_cutoffs` in `extraction.json`, then render and deploy (module 01, step 3). The review view and the app's queue use them from the next run. Don't recompute them on every run: a moving cutoff would move values in and out of the reviewers' queue.
3. Recalibrate after a schema change, or when a large batch of a different kind of document arrives (for example scans after born-digital files).

Confidence tells you where the model was unsure, not whether it was right. Treat the profile as triage, not as a quality measure; step 3's calibration shows whether it tracks correctness.

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

The script prints the run ID, the metrics, and a calibration table: the correctness rate per observed confidence level when there are at most 20 levels (`"by": "level"`), else per confidence bin. Array items count, matched to their labels as in the soft F1. It exits with status 1 when a threshold is missed.

## 4. Tune: change one thing at a time

| Failing signal | Look at | Change |
|---|---|---|
| One field fails across documents | `per_field.json` in the run | Sharpen its description or format in the schema (module 03) |
| Classification misses | `documents.json`: rows whose `doc_type` differs | Descriptions of the confused types (module 02) |
| Array soft F1 low | Item counts, predicted vs. labeled | Describe what counts as one item, and whether to include empty or subtotal rows |
| Long documents miss fields | Scores by document size | `"mode": "precision"` for the type |
| High confidence but wrong | Calibration: a high level with a low correct rate | Raise that type's `review_cutoffs` entry above the level, so its values go to people |
| Review queue flooded | `confidence_profile.py`: a type whose cutoff is 1.0 flags every value under 1.0 | Lower `--pct` and record the new cutoff, or leave the type out of `review_cutoffs` |

Changes to schemas or descriptions need a re-extraction with `full_refresh=true` (module 03, step 4), which re-bills. Confirm first, then rerun this module.

## Verify

- The confidence profile was reported, and `review_cutoffs` in `extraction.json` match what the user approved (or the user chose `review_threshold` alone).
- With labels: the thresholds are met, the run ID is reported, and `ground_truth.jsonl` is saved in the working directory, so later runs and the Forge certification run can reuse it.
- If calibration isn't monotonic, you told the user that confidence alone isn't a safe filter for this corpus.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no predicted segment` for a labeled document | `file_name` doesn't match `<prefix>_parsed.file_name`, or a packet label lacks `page` |
| Judge errors or timeouts | Rerun with `--no-judge` for exact matching, and report that strings were scored strictly |
| `PERMISSION_DENIED` reading `<prefix>_extracted` | The warehouse runs as you; check `SELECT` on the table and `CAN USE` on the warehouse |
| Unlabeled fields counted as wrong | They're present in `fields` as `null` or `""`; remove the key instead |
