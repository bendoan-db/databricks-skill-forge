-- Word-level localization stage of the forge-structured-doc-extraction use-case skill (module 06), run by
-- localize.py after extract.py. Temp views provided by localize.py: batch_paths(path), batch_localized(...).
-- A document is localized once per parse: <prefix>_progress records (path, parsed_at) for stage 'localize'.

-- name: create_localized_table
CREATE TABLE IF NOT EXISTS {{localized_table}} (
  path STRING NOT NULL,
  segment_id INT,
  doc_type STRING,
  field STRING COMMENT 'Dotted path, as in <prefix>_fields',
  value STRING,
  kind STRING COMMENT 'numeric or text: how the value was matched',
  citation_id INT,
  page_id INT COMMENT '0-based page id',
  element_bbox ARRAY<DOUBLE> COMMENT 'Cited element box [x0, y0, x1, y1] from ai_extract, in page-image pixels',
  word_bbox ARRAY<DOUBLE> COMMENT 'Box around the matched words, in page-image pixels; NULL when no match',
  matched_text STRING,
  match_score DOUBLE COMMENT '0-100',
  localized BOOLEAN COMMENT 'match_score met the threshold for its kind',
  error STRING,
  parsed_at TIMESTAMP,
  localized_at TIMESTAMP
)
COMMENT 'Word-level boxes for extracted values (forge-structured-doc-extraction)';

-- name: pending_documents
-- Documents extracted for their current parse but not yet localized.
CREATE OR REPLACE TEMP VIEW pending_documents AS
SELECT DISTINCT x.path
FROM {{extracted_table}} x
WHERE x.extracted IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM {{progress_table}} g
    WHERE g.stage = 'extract' AND g.path = x.path AND g.parsed_at = x.parsed_at
  )
  AND NOT EXISTS (
    SELECT 1 FROM {{progress_table}} g
    WHERE g.stage = 'localize' AND g.path = x.path AND g.parsed_at = x.parsed_at
  );

-- name: batch_values
-- Extracted values that cite at least one bounding box, with their segment's citations.
SELECT
  f.path, f.segment_id, f.doc_type, f.field, f.value, f.citation_ids, f.parsed_at,
  to_json(x.extracted:metadata.citations) AS citations_json
FROM {{fields_table}} f
JOIN {{extracted_table}} x ON x.path = f.path AND x.segment_id = f.segment_id
WHERE f.path IN (SELECT path FROM batch_paths) AND f.value IS NOT NULL AND size(f.citation_ids) > 0;

-- name: batch_documents
SELECT path, to_json(parsed:document.elements) AS elements_json, to_json(parsed:document.pages) AS pages_json
FROM {{parsed_table}}
WHERE path IN (SELECT path FROM batch_paths) AND parsed IS NOT NULL;

-- name: delete_batch_localized
DELETE FROM {{localized_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_batch_localized
-- batch_localized(...) is the temp view localize.py builds from the OCR matches of one batch.
INSERT INTO {{localized_table}} BY NAME
SELECT *, current_timestamp() AS localized_at FROM batch_localized;

-- name: record_batch_progress
MERGE INTO {{progress_table}} t
USING (SELECT path, parsed_at FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths)) s
ON t.stage = 'localize' AND t.path = s.path
WHEN MATCHED THEN UPDATE SET t.parsed_at = s.parsed_at, t.done_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (path, parsed_at, stage, done_at)
  VALUES (s.path, s.parsed_at, 'localize', current_timestamp());

-- name: delete_orphan_localized
DELETE FROM {{localized_table}} t
WHERE NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = t.path AND p.parsed IS NOT NULL);

-- name: delete_stale_progress
DELETE FROM {{progress_table}} g
WHERE g.stage = 'localize' AND NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = g.path);
