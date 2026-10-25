# 02 · Classify

Decide the document types and how documents are split into them. For `one_per_file` and `packet` layouts, design the labels that `ai_classify` v2.1 routes on. For `single_type`, record the one type and go to module 03.

## Inputs → outputs

- **Inputs:** `<prefix>_parsed` (module 01), and `layout` and the user's field requests from `extraction.json`.
- **Outputs:** `document_types` (names, descriptions, `pages`) and `classify_instructions` in `extraction.json`. After the module 03 run: `<prefix>_segments`, plus `<prefix>_pages` for packets.

## 1. Look at the documents

Sample a few parsed documents, or a few packet pages, to see what kinds there are:

```sql
WITH pages AS (
  SELECT file_name, parsed, pg.value AS page
  FROM <catalog>.<schema>.<prefix>_parsed, LATERAL variant_explode(parsed:document.pages) AS pg
  WHERE parsed IS NOT NULL
)
SELECT file_name, page:id::INT AS page_id,
       left(array_join(transform(filter(try_cast(parsed:document.elements AS ARRAY<VARIANT>),
            e -> e:bbox[0].page_id::INT = page:id::INT), e -> e:content::STRING), ' '), 400) AS page_start
FROM pages ORDER BY rand() LIMIT 20;
```

## 2. Design the labels

**Load:** `databricks-ai-functions` `references/1-task-functions.md`, the `ai_classify` section (labels with descriptions, `instructions`). Use version 2.1, not the version the base skill recommends (see SKILL.md, Base-skill guidance to override).

Propose one document type for each kind the user needs fields from, and show the list to the user:
- **Names** are snake_case, at most 40 characters, without `__`. Each becomes a table, `<prefix>_<type>`.
- **Descriptions** (at most 1,000 characters) say what sets the type apart: the form name or number, the fields printed on it, and how it differs from its nearest neighbor. Example: "A pay stub for one pay period: employer, employee, pay period dates, gross and net pay, year-to-date totals. Not a W-2, which covers a whole tax year."
- **`other` is added for you.** Pages or documents that match no type (fax covers, separators, boilerplate, unlisted kinds) are labeled `other`, skipped for extraction, and listed in the review queue. Don't define it yourself.
- **Packets only:** set `"pages": "multi"` on types whose documents span several pages (bank statements, tax returns, appraisals). Consecutive pages of a multi-page type become one sub-document. Every page of a `single` type is its own document, so two pay stubs in a row stay separate.

Write `classify_instructions` as one or two sentences of corpus context, such as "Pages come from residential mortgage loan files. A page with a recognizable form takes that form's label even if a fax header is present."

Record them in `extraction.json`, with the schemas still to come:

```json
"classify_instructions": "Pages come from residential mortgage loan files. ...",
"document_types": {
  "paystub": {"description": "A pay stub for one pay period: ...", "pages": "single"},
  "bank_statement": {"description": "A depository account statement: ...", "pages": "multi"}
}
```

Module 03 adds a `schema` to each type. Classification runs in the same job task as extraction, so it happens on module 03's run.

## How classification runs

- **`single_type`:** each document is one segment of the only type. No `ai_classify` call.
- **`one_per_file`:** one `ai_classify` call per document, on the parsed VARIANT, with `enableConfidenceScores`. Each document is one segment.
- **`packet`:** one call per page, on a page-sized slice of the parsed VARIANT, into `<prefix>_pages`. Pages are then grouped into `<prefix>_segments`. A new segment starts when the type changes, and at every page of a `single` type.

`class_confidence` is the classifier's confidence (for packets, the lowest across a segment's pages). Segments below `review_threshold`, and segments labeled `other`, appear in `<prefix>_review`.

## Verify

Run after module 03's job run:

```sql
SELECT doc_type, count(*) AS segments, round(avg(class_confidence), 2) AS avg_confidence,
       count_if(class_confidence < <review_threshold>) AS low_confidence
FROM <catalog>.<schema>.<prefix>_segments GROUP BY doc_type ORDER BY segments DESC;
```

- Every type the user needs appears, and `other` holds only pages without fields the user needs. Open two or three `other` segments to check.
- For packets, spot-check one file: `SELECT segment_id, doc_type, page_ids FROM <prefix>_segments WHERE path = '<path>' ORDER BY segment_id` should match the document boundaries you see.
- If more than about 10% of segments are low confidence, sharpen the descriptions of the types they're confused between, then rerun with `full_refresh=true` (module 03, step 4). That re-bills classification and extraction, so confirm first.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doc_type` NULL everywhere | Response shape differs: check `classification` in `<prefix>_segments` (or `<prefix>_pages`). v2.1 returns `response[0].value`; v2.0 returned plain strings |
| Two types keep swapping | Their descriptions overlap; state what each has that the other lacks, and add the tie-break to `classify_instructions` |
| A multi-page document split into many segments | Its type is `single`, or one page in the middle was classified differently. Set `"pages": "multi"`, or describe that page's look in the type's description |
| `ai_classify` rejects the page VARIANT | Set `extract_input` to `text` in `extraction.json` and render again (SKILL.md decision table) |
| Too many labels to keep straight | `ai_classify` accepts up to 500, but accuracy drops with near-duplicates; merge types that share a schema |
