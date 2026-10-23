# 03 · Extract

Design an `ai_extract` schema for each document type, then run classification and extraction over the parsed documents. Every value comes back with a confidence score and a citation.

## Inputs → outputs

- **Inputs:** `document_types` (module 02), `<prefix>_parsed`, and the fields the user asked for.
- **Outputs:** a `schema` (and optional `instructions`, `mode`) for each type in `extraction.json`; tables `<prefix>_segments`, `<prefix>_extracted` (raw `ai_extract` output per segment), and `<prefix>_fields` (one row per value).

## 1. Design the schemas

**Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_extract` section (advanced schema, version 2.1 citations and confidence).

For each type, write the schema from the user's field list and one or two sample documents of that type. The job turns each schema into a table, so it accepts this subset, which `render.py` checks:
- **Field names** are snake_case without `__`, and not `value`, `confidence_score`, or `citation_ids` (the names of `ai_extract`'s per-value wrapper).
- **Types:** `string`, `integer`, `number`, `boolean`, `enum` (with `labels`), `object` (with `properties`), and `array` (with `items` of a scalar type or an object). No arrays inside array items. At most 256 fields.
- **Descriptions** steer the model. State the format you want, such as "MM/DD/YYYY" or "USD, without $ or commas", and say when a field may be missing.
- **Repeating rows** (line items, transactions, deductions) are an array of objects. Each becomes a child table, `<prefix>_<type>__<field>`, with one row per item.

```json
"paystub": {
  "description": "A pay stub for one pay period: ...",
  "instructions": "US pay stubs. Leave a field empty when it isn't printed; never infer it.",
  "schema": {
    "employee_name": {"type": "string"},
    "pay_period_end": {"type": "string", "description": "MM/DD/YYYY"},
    "gross": {"type": "object", "properties": {
      "current": {"type": "number", "description": "Gross pay this period, USD"},
      "year_to_date": {"type": "number"}}},
    "deductions": {"type": "array", "items": {"type": "object", "properties": {
      "description": {"type": "string"}, "amount": {"type": "number"}}}}
  }
}
```

Show each schema to the user before running. Optional per type: `"mode": "precision"`, for long documents or many fields (see the decision table).

## 2. Render and deploy

```bash
uv run <skill>/scripts/render.py --state <workdir>/extraction.json
cd <workdir>/<name>-extract
databricks bundle validate --profile <profile>
databricks bundle deploy --profile <profile>
```

With `document_types` complete, the job's file-arrival trigger is now `UNPAUSED`: new files are parsed, classified, and extracted on arrival.

## 3. Run, after the user confirms

Restate the cost (one `ai_extract` call per segment, plus `ai_classify` for `one_per_file` and `packet`) and get a yes. Then:

```bash
databricks bundle run doc_extraction --profile <profile>
```

The `parse` task finds nothing new, and the `extract` task processes every parsed document. For each batch, it:
1. Builds the segments (module 02).
2. Runs `ai_extract` once per segment, with its type's schema, on the parsed VARIANT. For packets, the VARIANT is cut down to the segment's pages, with the original page ids. Options: `version` 2.1, `enableCitations`, `enableConfidenceScores`, plus `instructions` and `mode` when set.
3. Writes every value to `<prefix>_fields`, and the typed tables (module 04).

`<prefix>_progress` records each document's parse, so later runs only process new or re-parsed documents.

## 4. Changing a schema later

`extract.py` stops if a typed table lacks a column the schema now needs. With the user's confirmation, drop that type's tables (`<prefix>_<type>` and its `__` child tables). Then render and deploy again, and re-extract everything:

```bash
databricks bundle run doc_extraction --params full_refresh=true --profile <profile>
```

`full_refresh` classifies and extracts every document again (no re-parse), so it re-bills both. Confirm before running it.

## Verify

```sql
SELECT doc_type, count(*) AS segments, count_if(extract_error IS NOT NULL) AS errors,
       max(extracted:metadata.chunk_type::STRING) AS citation_kind
FROM <catalog>.<schema>.<prefix>_extracted GROUP BY doc_type;

SELECT doc_type, field, count(*) AS n, count_if(value IS NULL) AS empty, round(avg(confidence), 2) AS avg_confidence
FROM <catalog>.<schema>.<prefix>_fields
WHERE field NOT LIKE '%[%'  -- top-level and object fields; array items repeat per row
GROUP BY doc_type, field ORDER BY doc_type, empty DESC;
```

- Each type has segments, and `errors` is 0, or every error is explained.
- `citation_kind` is `bbox` (VARIANT input). `span` means text input, so module 06 can't run.
- Fields that are empty in most documents either don't appear on that type, or need a better description. Open two documents where one is empty and check.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `render.py` rejects a schema | Follow its message: the rules in step 1 exist because each field becomes a column |
| `ai_extract` errors on the segment VARIANT | Set `extract_input` to `text` in `extraction.json`, render, deploy, and run with `full_refresh=true` (confirm first). Citations become spans |
| Values in the wrong format | Put the format in the field's `description`, and shared rules in `instructions` |
| Values from the wrong document in a packet | The segment spans two documents: check its `page_ids` (module 02, Verify) |
| Many misses on long documents | Set `"mode": "precision"` for the type, then `full_refresh=true` |
| `A document type's schema changed` | See step 4 |
