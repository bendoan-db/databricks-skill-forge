# 04 · Tables and review

Check the typed tables the job publishes, walk the user through the review queue, and share the tables only with the people the user names.

## Inputs → outputs

- **Inputs:** the module 03 run.
- **Outputs:** confirmed `<prefix>_<type>` tables (and `<prefix>_<type>__<field>` child tables), the `<prefix>_review` view, and any grants the user asked for.

## What the job publishes

For each document type, `extract.py` writes:
- **`<prefix>_<type>`:** one row per document (or packet sub-document), with `path`, `file_name`, `segment_id`, and 1-based `page_numbers`. Every scalar field is a typed column, with a `<column>__confidence` column next to it. Nested objects flatten with `__`, so `gross.current` becomes `gross__current`. Arrays of scalars are `ARRAY` columns.
- **`<prefix>_<type>__<field>`:** for each array of objects, one row per item, keyed by `path`, `segment_id`, and `item_index`.

All typed tables have Change Data Feed on, so downstream jobs, or an AI Search index, can follow them. Rows are replaced when their document is re-parsed, and removed when it leaves the volume.

`<prefix>_review` is a view over everything a person should check:

| `reason` | Meaning |
|---|---|
| `low_confidence` | A value with `confidence` below `review_threshold` |
| `unclassified` | A segment labeled `other`, or with no label |
| `low_class_confidence` | A segment whose classification confidence is below `review_threshold` |
| `extract_error` | `ai_extract` failed on a segment; `value` holds the error |
| `parse_failed` | The document didn't parse; `value` holds the reason |

## Steps

1. **Report the tables:**

   ```sql
   SHOW TABLES IN <catalog>.<schema> LIKE '<prefix>_*';
   SELECT reason, doc_type, count(*) AS items FROM <catalog>.<schema>.<prefix>_review GROUP BY ALL ORDER BY items DESC;
   ```

2. **Walk through the review queue with the user.** Show the fields with the most low-confidence values, and open one or two documents for each to see why. Typical causes are a vague description, a field that is often absent, or poor scans. Fix descriptions in module 03. Changing `review_threshold` only needs a render and a deploy: the view is recreated on the next run.

3. **Share only on request.** Ask who should read the tables. Grant `SELECT` only to the users or groups the user names. **Load:** `databricks-unity-catalog` `references/1-access-control.md` for the grant syntax and the `USE CATALOG`/`USE SCHEMA` it needs. The review view shows values from every document, so grant it as narrowly as the tables.

## Verify

```sql
SELECT count(*) AS rows, count(DISTINCT path) AS documents FROM <catalog>.<schema>.<prefix>_<type>;
```

- For each type, `rows` equals that type's segment count in `<prefix>_segments`.
- For each child table, the items of a sample document match its array in `<prefix>_extracted`.
- The user has seen the review counts by reason, and any grants went only to the principals they named.

## Troubleshooting

| Symptom | Fix |
|---|---|
| A typed column is NULL everywhere but `<prefix>_fields` has values | The value doesn't cast to the column type, such as "$1,884.00" into DOUBLE. Ask for the plain format in the field description |
| A table you expected is missing | Its type has no `schema` in `extraction.json`, or `render.py` wasn't run again after adding it |
| Old rows after a document was deleted | Removal happens on the next job run; an empty volume listing skips deletions on purpose |
