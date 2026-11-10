-- Classify, extract, and publish stages of the forge-structured-doc-extraction use-case skill (modules 02-04),
-- plus the review app's queue and decisions (module 07), run by extract.py after parse.py. extract.py fills
-- {{placeholders}} one section at a time; unknown placeholders fail fast. Temp views provided by extract.py:
-- batch_paths(path), and field_specs(doc_type, pattern, field_type, field_description, enum_labels) with the review app.
-- {{document_input}}, {{page_input}}, and {{extract_input}} are expressions over the columns `parsed` and
-- `page_ids`: the parsed VARIANT (or one page's or one segment's slice of it), or its text.
-- A document is processed once per parse: <prefix>_progress records (path, parsed_at) for stage 'extract', and
-- for stage 'decisions' when review decisions were last applied to it.

-- name: create_progress_table
CREATE TABLE IF NOT EXISTS {{progress_table}} (
  path STRING NOT NULL,
  parsed_at TIMESTAMP NOT NULL COMMENT 'parsed_at of the <prefix>_parsed row this stage processed',
  stage STRING NOT NULL,
  done_at TIMESTAMP
)
COMMENT 'Which parse of each document each downstream stage has processed';

-- name: create_pages_table
CREATE TABLE IF NOT EXISTS {{pages_table}} (
  path STRING NOT NULL,
  page_id INT COMMENT '0-based page id in the parsed document',
  doc_type STRING COMMENT 'ai_classify label, or other',
  class_confidence DOUBLE,
  classification VARIANT COMMENT 'ai_classify v2.1 output',
  parsed_at TIMESTAMP
)
COMMENT 'Page classification for document packets (forge-structured-doc-extraction)';

-- name: create_segments_table
CREATE TABLE IF NOT EXISTS {{segments_table}} (
  path STRING NOT NULL,
  segment_id INT COMMENT '0 for whole documents; position of the sub-document within a packet',
  doc_type STRING COMMENT 'Document type, or other for pages that match no type',
  page_ids ARRAY<INT> COMMENT '0-based page ids the segment spans',
  class_confidence DOUBLE COMMENT 'Lowest classification confidence among its pages',
  classification VARIANT COMMENT 'ai_classify v2.1 output, for whole-document classification',
  parsed_at TIMESTAMP
)
COMMENT 'Documents, or sub-documents of packets, to extract (forge-structured-doc-extraction)';

-- name: create_extracted_table
CREATE TABLE IF NOT EXISTS {{extracted_table}} (
  path STRING NOT NULL,
  segment_id INT,
  doc_type STRING,
  page_ids ARRAY<INT>,
  extracted VARIANT COMMENT 'ai_extract v2.1 output with citations and confidence scores',
  extract_error STRING,
  parsed_at TIMESTAMP,
  extracted_at TIMESTAMP
)
COMMENT 'Raw ai_extract output per segment (forge-structured-doc-extraction)';

-- name: create_fields_table
CREATE TABLE IF NOT EXISTS {{fields_table}} (
  path STRING NOT NULL,
  segment_id INT,
  doc_type STRING,
  field STRING COMMENT 'Dotted path, e.g. gross.current_amount or line_items[2].amount',
  value STRING COMMENT 'Extracted value as text; NULL when the model found none',
  confidence DOUBLE,
  citation_ids ARRAY<INT> COMMENT 'ids into extracted:metadata.citations',
  parsed_at TIMESTAMP
)
COMMENT 'One row per extracted value, for review and confidence profiling (forge-structured-doc-extraction)';

-- name: create_decisions_table
-- Empty unless the review app (module 07) is on. The typed tables carry these values in place of the model's.
CREATE TABLE IF NOT EXISTS {{decisions_table}} (
  review_id STRING NOT NULL,
  path STRING NOT NULL,
  segment_id INT,
  doc_type STRING,
  field STRING COMMENT 'Dotted path, as in <prefix>_fields',
  parsed_at TIMESTAMP COMMENT 'The parse the decision was made on; a re-parsed document needs new decisions',
  ai_value STRING COMMENT 'The extracted value the reviewer saw',
  decision STRING COMMENT 'approved or corrected',
  value STRING COMMENT 'The value the typed tables carry, as text; NULL means the document has none',
  comment STRING,
  reviewed_by STRING,
  reviewed_at TIMESTAMP,
  decision_request_id STRING,
  row_version BIGINT,
  merged_at TIMESTAMP COMMENT 'When this version of the decision reached this table'
)
COMMENT 'Review decisions from the review app, latest per value (forge-structured-doc-extraction module 07)';

-- name: create_candidates_table
-- The review app reads this through a Lakebase synced table, so it has no VARIANT columns, and its change data
-- feed is on for TRIGGERED sync.
CREATE TABLE IF NOT EXISTS {{candidates_table}} (
  review_id STRING NOT NULL,
  path STRING NOT NULL,
  file_name STRING,
  segment_id INT,
  doc_type STRING,
  field STRING COMMENT 'Dotted path, as in <prefix>_fields',
  field_type STRING COMMENT 'string, number, integer, boolean, or enum',
  field_description STRING,
  enum_labels ARRAY<STRING>,
  page_numbers ARRAY<INT> COMMENT '1-based pages of the document or sub-document',
  ai_value STRING,
  confidence DOUBLE,
  parsed_at TIMESTAMP,
  queued_at TIMESTAMP
)
COMMENT 'Review app queue: values below their threshold, or unscored (forge-structured-doc-extraction module 07)'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: pending_documents
-- Parsed documents whose current parse hasn't been extracted yet.
CREATE OR REPLACE TEMP VIEW pending_documents AS
SELECT p.path
FROM {{parsed_table}} p
WHERE p.parsed IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM {{progress_table}} g
    WHERE g.stage = 'extract' AND g.path = p.path AND g.parsed_at = p.parsed_at
  );

-- name: delete_batch_rows
DELETE FROM {{target_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_segments_whole
-- Layout single_type: each document is one segment of the only type. No classification.
INSERT INTO {{segments_table}} BY NAME
SELECT
  path,
  0 AS segment_id,
  {{single_type_literal}} AS doc_type,
  transform(try_cast(parsed:document.pages AS ARRAY<VARIANT>), pg -> pg:id::INT) AS page_ids,
  CAST(NULL AS DOUBLE) AS class_confidence,
  CAST(NULL AS VARIANT) AS classification,
  parsed_at
FROM {{parsed_table}}
WHERE path IN (SELECT path FROM batch_paths) AND parsed IS NOT NULL;

-- name: insert_segments_classified
-- Layout one_per_file: each document is one segment, classified as a whole.
INSERT INTO {{segments_table}} BY NAME
WITH documents AS (
  SELECT
    path,
    parsed_at,
    parsed,
    transform(try_cast(parsed:document.pages AS ARRAY<VARIANT>), pg -> pg:id::INT) AS page_ids
  FROM {{parsed_table}}
  WHERE path IN (SELECT path FROM batch_paths) AND parsed IS NOT NULL
),
results AS (
  SELECT path, parsed_at, page_ids, ai_classify({{document_input}}, {{classify_labels}}, {{classify_options}}) AS classification
  FROM documents
)
SELECT
  path,
  0 AS segment_id,
  classification:response[0].value::STRING AS doc_type,
  page_ids,
  classification:response[0].confidence_score::DOUBLE AS class_confidence,
  classification,
  parsed_at
FROM results;

-- name: insert_batch_pages
-- Layout packet: classify every page on its own.
INSERT INTO {{pages_table}} BY NAME
WITH exploded AS (
  SELECT p.path, p.parsed_at, p.parsed, pg.value AS page
  FROM {{parsed_table}} p, LATERAL variant_explode(p.parsed:document.pages) AS pg
  WHERE p.path IN (SELECT path FROM batch_paths) AND p.parsed IS NOT NULL
),
pages AS (
  SELECT path, parsed_at, parsed, page:id::INT AS page_id, array(page:id::INT) AS page_ids FROM exploded
),
results AS (
  SELECT path, parsed_at, page_id, ai_classify({{page_input}}, {{classify_labels}}, {{classify_options}}) AS classification
  FROM pages
)
SELECT
  path,
  page_id,
  classification:response[0].value::STRING AS doc_type,
  classification:response[0].confidence_score::DOUBLE AS class_confidence,
  classification,
  parsed_at
FROM results;

-- name: insert_segments_from_pages
-- Layout packet: consecutive pages of a multi-page type ({{multi_page_types}}) form one sub-document;
-- every page of any other type is its own. A change of type always starts a new segment.
INSERT INTO {{segments_table}} BY NAME
WITH ordered AS (
  SELECT *, lag(doc_type) OVER (PARTITION BY path ORDER BY page_id) AS previous_type
  FROM {{pages_table}}
  WHERE path IN (SELECT path FROM batch_paths)
),
marked AS (
  SELECT *,
    CASE
      WHEN previous_type IS NULL OR doc_type IS NULL OR previous_type <> doc_type THEN 1
      WHEN coalesce(doc_type IN ({{multi_page_types}}), false) THEN 0
      ELSE 1
    END AS starts_segment
  FROM ordered
),
numbered AS (
  SELECT *,
    CAST(sum(starts_segment) OVER (
      PARTITION BY path ORDER BY page_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) - 1 AS INT) AS segment_id
  FROM marked
)
SELECT
  path,
  segment_id,
  first(doc_type) AS doc_type,
  array_sort(collect_list(page_id)) AS page_ids,
  min(class_confidence) AS class_confidence,
  CAST(NULL AS VARIANT) AS classification,
  max(parsed_at) AS parsed_at
FROM numbered
GROUP BY path, segment_id;

-- name: insert_batch_extracted
-- One statement per document type, because the schema is a literal. Delete-then-insert (by the caller)
-- rather than MERGE, so ai_extract runs exactly once per segment.
INSERT INTO {{extracted_table}} BY NAME
WITH segments AS (
  SELECT s.path, s.segment_id, s.doc_type, s.page_ids, s.parsed_at, p.parsed
  FROM {{segments_table}} s
  JOIN {{parsed_table}} p ON p.path = s.path
  WHERE s.path IN (SELECT path FROM batch_paths) AND s.doc_type = {{doc_type_literal}} AND p.parsed IS NOT NULL
),
results AS (
  SELECT path, segment_id, doc_type, page_ids, parsed_at,
    ai_extract({{extract_input}}, {{extract_schema}}, {{extract_options}}) AS extracted
  FROM segments
)
SELECT
  path,
  segment_id,
  doc_type,
  page_ids,
  extracted,
  CASE
    WHEN extracted IS NULL THEN 'ai_extract returned NULL'
    ELSE nullif(extracted:error_message::STRING, '')
  END AS extract_error,
  parsed_at,
  current_timestamp() AS extracted_at
FROM results;

-- name: insert_batch_fields
-- forge_fields (registered by extract.py) walks the response and returns one row per scalar value.
INSERT INTO {{fields_table}} BY NAME
SELECT path, segment_id, doc_type, f.field, f.value, f.confidence, f.citation_ids, parsed_at
FROM (
  SELECT path, segment_id, doc_type, parsed_at, explode(forge_fields(to_json(extracted:response))) AS f
  FROM {{extracted_table}}
  WHERE path IN (SELECT path FROM batch_paths) AND extracted IS NOT NULL
);

-- name: create_gold_table
CREATE TABLE IF NOT EXISTS {{gold_table}} (
  path STRING NOT NULL,
  file_name STRING,
  segment_id INT,
  page_numbers ARRAY<INT> COMMENT '1-based pages the document spans',{{gold_column_ddl}}
  reviewed_fields ARRAY<STRING> COMMENT 'Fields a person approved or corrected in the review app; their confidence is 1.0',
  reviewed_at TIMESTAMP,
  parsed_at TIMESTAMP,
  published_at TIMESTAMP
)
COMMENT {{gold_comment}}
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: add_review_columns
-- Typed tables created by version 0.2 of this skill, before the review app, lack these columns.
ALTER TABLE {{target_table}} ADD COLUMNS (
  reviewed_fields ARRAY<STRING> COMMENT 'Fields a person approved or corrected in the review app; their confidence is 1.0',
  reviewed_at TIMESTAMP
);

-- name: batch_reviewed
-- The batch's segments of one type, each with `reviewed`: a map from field path to the value a person decided
-- in the review app (module 07) for this parse, or NULL when there are no decisions.
CREATE OR REPLACE TEMP VIEW batch_reviewed AS
SELECT x.path, x.segment_id, x.page_ids, x.parsed_at, x.extracted, r.reviewed, r.reviewed_at
FROM {{extracted_table}} x
LEFT JOIN (
  SELECT path, segment_id, parsed_at, map_from_entries(collect_list(struct(field, value))) AS reviewed,
         max(reviewed_at) AS reviewed_at
  FROM {{decisions_table}}
  WHERE doc_type = {{doc_type_literal}} AND path IN (SELECT path FROM batch_paths)
  GROUP BY path, segment_id, parsed_at
) r ON r.path = x.path AND r.segment_id = x.segment_id AND r.parsed_at = x.parsed_at
WHERE x.path IN (SELECT path FROM batch_paths) AND x.doc_type = {{doc_type_literal}} AND x.extracted IS NOT NULL;

-- name: gold_rows
CREATE OR REPLACE TEMP VIEW gold_rows AS
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  segment_id,
  transform(page_ids, i -> i + 1) AS page_numbers,{{gold_column_select}}
  array_sort(map_keys(reviewed)) AS reviewed_fields,
  reviewed_at,
  parsed_at,
  current_timestamp() AS published_at
FROM batch_reviewed;

-- name: insert_batch_gold
INSERT INTO {{gold_table}} BY NAME SELECT * FROM gold_rows;

-- name: merge_batch_gold
-- New decisions change values, not rows, so they're applied in place. A concurrent extraction run rewrites
-- other documents' rows.
MERGE INTO {{gold_table}} t
USING gold_rows s
ON t.path = s.path AND t.segment_id = s.segment_id
WHEN MATCHED THEN UPDATE SET *;

-- name: create_child_table
-- One row per item of an array-of-objects field, e.g. a statement's transactions.
CREATE TABLE IF NOT EXISTS {{child_table}} (
  path STRING NOT NULL,
  segment_id INT,
  item_index INT COMMENT '0-based position in the array',{{child_column_ddl}}
  reviewed_fields ARRAY<STRING> COMMENT 'Item fields a person approved or corrected in the review app; their confidence is 1.0',
  reviewed_at TIMESTAMP,
  parsed_at TIMESTAMP,
  published_at TIMESTAMP
)
COMMENT {{child_comment}}
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: child_rows
-- Decisions on an item are keyed <array path>[<item_index>].<field>, as in <prefix>_fields.
CREATE OR REPLACE TEMP VIEW child_rows AS
WITH items AS (
  SELECT x.path, x.segment_id, x.parsed_at, x.reviewed, x.reviewed_at, i.pos, i.value AS item,
    filter(map_keys(x.reviewed),
           k -> startswith(k, concat({{array_key_literal}}, '[', CAST(i.pos AS STRING), '].'))) AS item_keys
  FROM batch_reviewed x,
    LATERAL variant_explode({{array_expr}}) AS i
)
SELECT
  path,
  segment_id,
  CAST(pos AS INT) AS item_index,{{child_column_select}}
  CASE WHEN size(item_keys) > 0
    THEN transform(item_keys, k -> substr(k, length({{array_key_literal}}) + length(CAST(pos AS STRING)) + 4))
  END AS reviewed_fields,
  CASE WHEN size(item_keys) > 0 THEN reviewed_at END AS reviewed_at,
  parsed_at,
  current_timestamp() AS published_at
FROM items;

-- name: insert_batch_child
INSERT INTO {{child_table}} BY NAME SELECT * FROM child_rows;

-- name: merge_batch_child
MERGE INTO {{child_table}} t
USING child_rows s
ON t.path = s.path AND t.segment_id = s.segment_id AND t.item_index = s.item_index
WHEN MATCHED THEN UPDATE SET *;

-- name: record_batch_progress
MERGE INTO {{progress_table}} t
USING (SELECT path, parsed_at FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths)) s
ON t.stage = {{stage_literal}} AND t.path = s.path
WHEN MATCHED THEN UPDATE SET t.parsed_at = s.parsed_at, t.done_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (path, parsed_at, stage, done_at)
  VALUES (s.path, s.parsed_at, {{stage_literal}}, current_timestamp());

-- name: merge_candidates
-- The review app's queue: every value below its document type's threshold (review_threshold, or the calibrated
-- level in review_cutoffs), and every value that came back without a score, in the current extraction, keyed so a decision
-- stays attached to its value across re-extractions of the same parse. Unchanged rows aren't touched, so the
-- synced table moves only what changed.
MERGE INTO {{candidates_table}} t
USING (
  SELECT
    sha2(concat_ws('|', f.path, CAST(unix_micros(f.parsed_at) AS STRING), CAST(f.segment_id AS STRING),
                   f.doc_type, f.field), 256) AS review_id,
    f.path,
    regexp_extract(f.path, '[^/]+$', 0) AS file_name,
    f.segment_id,
    f.doc_type,
    f.field,
    coalesce(spec.field_type, 'string') AS field_type,
    replace(spec.field_description, char(0), '') AS field_description,
    spec.enum_labels,
    transform(x.page_ids, i -> i + 1) AS page_numbers,
    replace(f.value, char(0), '') AS ai_value,
    f.confidence,
    f.parsed_at
  FROM {{fields_table}} f
  JOIN {{extracted_table}} x ON x.path = f.path AND x.segment_id = f.segment_id AND x.doc_type = f.doc_type
  LEFT JOIN field_specs spec
    ON spec.doc_type = f.doc_type AND spec.pattern = regexp_replace(f.field, '\\[[0-9]+\\]', '[]')
  WHERE f.confidence < {{field_threshold}} OR (f.confidence IS NULL AND f.value IS NOT NULL)
) s
ON t.review_id = s.review_id
WHEN MATCHED AND NOT (t.ai_value <=> s.ai_value AND t.confidence <=> s.confidence AND t.page_numbers <=> s.page_numbers
                      AND t.field_type <=> s.field_type AND t.field_description <=> s.field_description
                      AND t.enum_labels <=> s.enum_labels) THEN UPDATE SET
  t.field_type = s.field_type, t.field_description = s.field_description, t.enum_labels = s.enum_labels,
  t.page_numbers = s.page_numbers, t.ai_value = s.ai_value, t.confidence = s.confidence,
  t.queued_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (review_id, path, file_name, segment_id, doc_type, field, field_type, field_description,
                              enum_labels, page_numbers, ai_value, confidence, parsed_at, queued_at)
  VALUES (s.review_id, s.path, s.file_name, s.segment_id, s.doc_type, s.field, s.field_type, s.field_description,
          s.enum_labels, s.page_numbers, s.ai_value, s.confidence, s.parsed_at, current_timestamp())
WHEN NOT MATCHED BY SOURCE THEN DELETE;

-- name: merge_decisions
-- The latest state of each decision in the Lakehouse Sync history of the app's Postgres decisions table. Every
-- change in the app gets a new decision_request_id, and merged_at marks the document for re-publication.
MERGE INTO {{decisions_table}} t
USING (
  SELECT review_id, path, CAST(segment_id AS INT) AS segment_id, doc_type, field,
         CAST(parsed_at AS TIMESTAMP) AS parsed_at, ai_value, decision, value, comment, reviewed_by,
         CAST(reviewed_at AS TIMESTAMP) AS reviewed_at, decision_request_id, CAST(row_version AS BIGINT) AS row_version
  FROM (
    SELECT *, row_number() OVER (PARTITION BY review_id ORDER BY _sort_by DESC, _pg_lsn DESC) AS change_rank
    FROM {{history_table}}
    WHERE _pg_change_type IN ('insert', 'update_postimage', 'delete')
  )
  WHERE change_rank = 1 AND _pg_change_type <> 'delete'
) s
ON t.review_id = s.review_id
WHEN MATCHED AND NOT (t.decision_request_id <=> s.decision_request_id) THEN UPDATE SET
  t.path = s.path, t.segment_id = s.segment_id, t.doc_type = s.doc_type, t.field = s.field, t.parsed_at = s.parsed_at,
  t.ai_value = s.ai_value, t.decision = s.decision, t.value = s.value, t.comment = s.comment,
  t.reviewed_by = s.reviewed_by, t.reviewed_at = s.reviewed_at, t.decision_request_id = s.decision_request_id,
  t.row_version = s.row_version, t.merged_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (review_id, path, segment_id, doc_type, field, parsed_at, ai_value, decision, value,
                              comment, reviewed_by, reviewed_at, decision_request_id, row_version, merged_at)
  VALUES (s.review_id, s.path, s.segment_id, s.doc_type, s.field, s.parsed_at, s.ai_value, s.decision, s.value,
          s.comment, s.reviewed_by, s.reviewed_at, s.decision_request_id, s.row_version, current_timestamp());

-- name: pending_decisions
-- Parsed documents with a decision newer than the last time decisions were applied to their typed rows.
CREATE OR REPLACE TEMP VIEW pending_decisions AS
SELECT d.path
FROM {{decisions_table}} d
JOIN {{parsed_table}} p ON p.path = d.path AND p.parsed IS NOT NULL
LEFT JOIN {{progress_table}} g ON g.stage = 'decisions' AND g.path = d.path
GROUP BY d.path
HAVING max(d.merged_at) > coalesce(max(g.done_at), TIMESTAMP '1970-01-01');

-- name: delete_orphan_rows
-- Rows whose document was removed from the volume, or whose latest parse failed.
DELETE FROM {{target_table}} t
WHERE NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = t.path AND p.parsed IS NOT NULL);

-- name: delete_stale_progress
DELETE FROM {{progress_table}} g
WHERE g.stage IN ('extract', 'decisions') AND NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = g.path);

-- name: reset_progress
DELETE FROM {{progress_table}} WHERE stage IN ('extract', 'localize');

-- name: create_review_view
-- Everything a person should check: low-confidence and unscored values without a review decision, unclassified,
-- low-confidence, or unscored segments, extraction errors, and documents that didn't parse. Recreated every run,
-- so the thresholds stay current. A NULL value is a field the document lacks, not a review item by itself.
CREATE OR REPLACE VIEW {{review_view}}
COMMENT 'Review queue for forge-structured-doc-extraction: one row per item to check'
AS
SELECT f.path, f.segment_id, f.doc_type, f.field, f.value, f.confidence,
  CASE WHEN f.confidence IS NULL THEN 'unscored' ELSE 'low_confidence' END AS reason
FROM {{fields_table}} f
WHERE (f.confidence < {{field_threshold}} OR (f.confidence IS NULL AND f.value IS NOT NULL))
  AND NOT EXISTS (
    SELECT 1 FROM {{decisions_table}} d
    WHERE d.path = f.path AND d.segment_id = f.segment_id AND d.doc_type = f.doc_type AND d.field = f.field
      AND d.parsed_at = f.parsed_at
  )
UNION ALL
SELECT path, segment_id, doc_type, CAST(NULL AS STRING), CAST(NULL AS STRING), class_confidence,
  CASE
    WHEN doc_type IS NULL OR doc_type = 'other' THEN 'unclassified'
    WHEN class_confidence IS NULL THEN 'unscored_class'
    ELSE 'low_class_confidence'
  END
FROM {{segments_table}}
WHERE doc_type IS NULL OR doc_type = 'other' OR class_confidence < {{review_threshold}}
  OR (class_confidence IS NULL AND {{classified}})
UNION ALL
SELECT path, segment_id, doc_type, CAST(NULL AS STRING), extract_error, CAST(NULL AS DOUBLE), 'extract_error'
FROM {{extracted_table}}
WHERE extract_error IS NOT NULL
UNION ALL
SELECT path, CAST(NULL AS INT), CAST(NULL AS STRING), CAST(NULL AS STRING), parse_error, CAST(NULL AS DOUBLE), 'parse_failed'
FROM {{parsed_table}}
WHERE parsed IS NULL;
