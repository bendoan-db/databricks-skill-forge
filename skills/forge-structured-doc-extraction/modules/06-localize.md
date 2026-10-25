# 06 · Localize

Only when `localize` is on. Find the exact words behind each extracted value: `ai_extract` cites the whole element a value came from (a table, a paragraph), and this module narrows that to a box around the value itself, for highlighting in a review UI.

## Inputs → outputs

- **Inputs:** `<prefix>_fields` and `<prefix>_extracted` with bounding-box citations (module 03, `citation_kind = bbox`), and page images (`page_images_path`, set in module 01 before parsing).
- **Outputs:** `ocr_cluster` in `extraction.json`, a `localize` task in the job, and `<prefix>_localized`: one row per (value, citation), with the cited element's box, the word box, and a match score.

## How it works

For each cited value, `localize.py` crops the cited element from its page image and OCRs the crop with Tesseract. It then finds the printed form of the value in the element's text ("$1,884.00" for 1884.0), and matches that against runs of OCR words. Numbers must match exactly after normalization; text needs a fuzzy score of 85. The matched words' box is converted back to page-image pixels.

Tesseract is a system binary, and serverless compute can't install one, so the task runs on a single-node classic job cluster whose init script installs `tesseract-ocr`. **Load:** `databricks-serverless-migration` `references/code-patterns.md` if the user asks why it can't run on serverless (Tesseract is listed as a classic-only dependency).

## Steps

1. **Pick the cluster.** **Load:** `databricks-jobs` `references/examples.md` (job clusters). Choose the latest LTS runtime and a general-purpose node with 4–8 cores:

   ```bash
   databricks clusters spark-versions --profile <profile> -o json | jq -r '.versions[] | select(.name | test("LTS") and (test("ML|Photon") | not)) | .key' | head -3
   databricks clusters list-node-types --profile <profile> -o json | jq -r '.node_types[] | select(.num_cores >= 4 and .num_cores <= 8 and (.is_deprecated | not)) | .node_type_id' | head -10
   ```

   Record `"ocr_cluster": {"spark_version": "<key>", "node_type_id": "<id>"}` and `"localize": true` in `extraction.json`.

2. **Confirm the cost, then render, deploy, and run.** Each job run now starts the cluster for the `localize` task, billed while it runs. The init script needs egress to the Ubuntu package mirrors.

   ```bash
   uv run <skill>/scripts/render.py --state <workdir>/extraction.json
   cd <workdir>/<name>-extract
   databricks bundle deploy --profile <profile>
   databricks bundle run doc_extraction --only localize --profile <profile>
   ```

   Later runs localize only documents whose extraction changed (`<prefix>_progress`, stage `localize`).

## Verify

```sql
SELECT coalesce(kind, 'all') AS kind, count(*) AS cited, count_if(localized) AS localized,
       round(count_if(localized) / count(*), 3) AS hit_rate, count_if(error IS NOT NULL) AS errors
FROM <catalog>.<schema>.<prefix>_localized GROUP BY ROLLUP(kind);
```

- The overall (`all`) hit rate is at least 0.8 (`MIN_HIT_RATE` in `scripts/localize.py`; the task prints `meets_gate`).
- Check two localized values by eye: crop `word_bbox` from the page image (`<prefix>_parsed` `parsed:document.pages[<page_id>].image_uri`), and confirm it holds the value.
- Below 0.8, look at the failures by `field` and `error` before changing anything, and tell the user which fields fall back to the element box (`element_bbox`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Tesseract isn't installed on this compute` | Check the init script logs on the job run's cluster; `apt-get` needs egress. Without egress, host the `tesseract-ocr` package on a mirror the workspace can reach, and change `install_tesseract.sh` to use it |
| `citation has no bounding box or page image` | Parsing ran without `page_images_path`, or extraction used text input. Set both up, then re-parse: replacing the files, or dropping `<prefix>_parsed` after confirming, re-bills parsing |
| Low hit rate on numbers | The document prints amounts differently from the value (for example, "1.884,00"): add the format to the field description so values match the print |
| Low hit rate on long text fields | Expected: long values span lines; use `element_bbox` for those fields |
| Cluster fails to start | Node type unavailable in the region or blocked by policy: pick another from step 1 |
