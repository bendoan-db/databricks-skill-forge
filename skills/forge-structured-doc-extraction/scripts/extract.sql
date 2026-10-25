-- Classify, extract, and publish stages of the forge-structured-doc-extraction use-case skill (modules 02-04),
-- run by extract.py after parse.py. extract.py fills {{placeholders}} one section at a time; unknown
-- placeholders fail fast. Temp view provided by extract.py: batch_paths(path).
-- {{document_input}}, {{page_input}}, and {{extract_input}} are expressions over the columns `parsed` and
-- `page_ids`: the parsed VARIANT (or one page's or one segment's slice of it), or its text.
-- A document is processed once per parse: <prefix>_progress records (path, parsed_at) for stage 'extract'.

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
  parsed_at TIMESTAMP,
  published_at TIMESTAMP
)
COMMENT {{gold_comment}}
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: insert_batch_gold
INSERT INTO {{gold_table}} BY NAME
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  segment_id,
  transform(page_ids, i -> i + 1) AS page_numbers,{{gold_column_select}}
  parsed_at,
  current_timestamp() AS published_at
FROM {{extracted_table}}
WHERE path IN (SELECT path FROM batch_paths) AND doc_type = {{doc_type_literal}} AND extracted IS NOT NULL;

-- name: create_child_table
-- One row per item of an array-of-objects field, e.g. a statement's transactions.
CREATE TABLE IF NOT EXISTS {{child_table}} (
  path STRING NOT NULL,
  segment_id INT,
  item_index INT COMMENT '0-based position in the array',{{child_column_ddl}}
  parsed_at TIMESTAMP,
  published_at TIMESTAMP
)
COMMENT {{child_comment}}
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: insert_batch_child
INSERT INTO {{child_table}} BY NAME
WITH items AS (
  SELECT x.path, x.segment_id, x.parsed_at, i.pos, i.value AS item
  FROM {{extracted_table}} x,
    LATERAL variant_explode({{array_expr}}) AS i
  WHERE x.path IN (SELECT path FROM batch_paths) AND x.doc_type = {{doc_type_literal}} AND x.extracted IS NOT NULL
)
SELECT
  path,
  segment_id,
  CAST(pos AS INT) AS item_index,{{child_column_select}}
  parsed_at,
  current_timestamp() AS published_at
FROM items;

-- name: record_batch_progress
MERGE INTO {{progress_table}} t
USING (SELECT path, parsed_at FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths)) s
ON t.stage = 'extract' AND t.path = s.path
WHEN MATCHED THEN UPDATE SET t.parsed_at = s.parsed_at, t.done_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (path, parsed_at, stage, done_at)
  VALUES (s.path, s.parsed_at, 'extract', current_timestamp());

-- name: delete_orphan_rows
-- Rows whose document was removed from the volume, or whose latest parse failed.
DELETE FROM {{target_table}} t
WHERE NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = t.path AND p.parsed IS NOT NULL);

-- name: delete_stale_progress
DELETE FROM {{progress_table}} g
WHERE g.stage = 'extract' AND NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = g.path);

-- name: reset_progress
DELETE FROM {{progress_table}} WHERE stage IN ('extract', 'localize');

-- name: create_review_view
-- Everything a person should check: low-confidence values, unclassified or low-confidence segments,
-- extraction errors, and documents that didn't parse. Recreated every run, so the threshold stays current.
CREATE OR REPLACE VIEW {{review_view}}
COMMENT 'Review queue for forge-structured-doc-extraction: one row per item to check'
AS
SELECT path, segment_id, doc_type, field, value, confidence, 'low_confidence' AS reason
FROM {{fields_table}}
WHERE confidence < {{review_threshold}}
UNION ALL
SELECT path, segment_id, doc_type, CAST(NULL AS STRING), CAST(NULL AS STRING), class_confidence,
  CASE WHEN doc_type IS NULL OR doc_type = 'other' THEN 'unclassified' ELSE 'low_class_confidence' END
FROM {{segments_table}}
WHERE doc_type IS NULL OR doc_type = 'other' OR class_confidence < {{review_threshold}}
UNION ALL
SELECT path, segment_id, doc_type, CAST(NULL AS STRING), extract_error, CAST(NULL AS DOUBLE), 'extract_error'
FROM {{extracted_table}}
WHERE extract_error IS NOT NULL
UNION ALL
SELECT path, CAST(NULL AS INT), CAST(NULL AS STRING), CAST(NULL AS STRING), parse_error, CAST(NULL AS DOUBLE), 'parse_failed'
FROM {{parsed_table}}
WHERE parsed IS NULL;
