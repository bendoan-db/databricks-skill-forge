-- Parse stage for the doc-qa-agent use-case skill (module 01).
-- ingest.py runs one named section at a time and fills {{placeholders}}; unknown placeholders fail fast.
-- Temp views provided by ingest.py: volume_files(path, modificationTime, length),
-- batch_files(path, modificationTime, length, content), batch_paths(path).

-- name: create_parsed_table
CREATE TABLE IF NOT EXISTS {{parsed_table}} (
  path STRING NOT NULL COMMENT 'dbfs:/Volumes/... path from read_files',
  file_name STRING,
  modification_time TIMESTAMP COMMENT 'Change-detection key together with path',
  file_size BIGINT,
  parsed VARIANT COMMENT 'ai_parse_document v2 output; NULL when skipped',
  parse_error STRING COMMENT 'JSON error_status, or the skip reason',
  parsed_at TIMESTAMP
)
COMMENT 'One row per source file, parsed with ai_parse_document (doc-qa-agent)';

-- name: changed_files
-- New files, plus files whose modification time differs from the last parse.
CREATE OR REPLACE TEMP VIEW changed_files AS
SELECT f.path, f.modificationTime AS modification_time, f.length AS file_size
FROM volume_files f
LEFT ANTI JOIN {{parsed_table}} p
  ON f.path = p.path AND f.modificationTime = p.modification_time;

-- name: delete_batch_parsed
DELETE FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_batch_parsed
-- Delete-then-insert rather than MERGE so the AI function is evaluated exactly once per file.
INSERT INTO {{parsed_table}} BY NAME
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  modification_time,
  file_size,
  parsed,
  CASE
    WHEN parsed IS NULL THEN 'ai_parse_document returned NULL'
    ELSE nullif(nullif(to_json(parsed:error_status), '[]'), 'null')
  END AS parse_error,
  current_timestamp() AS parsed_at
FROM (
  SELECT
    path,
    modificationTime AS modification_time,
    length AS file_size,
    ai_parse_document(content, {{parse_options}}) AS parsed
  FROM batch_files
);

-- name: insert_oversize
-- Record files over the ai_parse_document limit so they aren't retried until they change.
INSERT INTO {{parsed_table}} BY NAME
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  modification_time,
  file_size,
  CAST(NULL AS VARIANT) AS parsed,
  'skipped: file larger than {{max_file_mb}} MB' AS parse_error,
  current_timestamp() AS parsed_at
FROM changed_files
WHERE file_size > {{max_file_bytes}};

-- name: delete_removed_files
DELETE FROM {{parsed_table}} t
WHERE NOT EXISTS (SELECT 1 FROM volume_files f WHERE f.path = t.path);
