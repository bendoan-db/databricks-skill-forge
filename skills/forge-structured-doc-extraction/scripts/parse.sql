-- Shared parse stage (module 01) for Forge use-case skills. Every skill that parses documents carries an
-- identical copy; tests/test_shared_parse.py fails if the copies drift.
-- parse.py runs one named section at a time and fills {{placeholders}}; unknown placeholders fail fast.
-- Temp views provided by parse.py: volume_files(path, modificationTime, length),
-- batch_files(path, modificationTime, length, content), batch_page_counts(path, page_count),
-- batch_paths(path), long_slices(path, start_page, parsed).
-- {{files_table}} is {{parsed_table}} unless split pages are on; then it holds one row per page file and
-- {{parsed_table}} holds the documents assembled from them.

-- name: create_parsed_table
CREATE TABLE IF NOT EXISTS {{parsed_table}} (
  path STRING NOT NULL COMMENT 'dbfs:/Volumes/... path of the file, or of the folder for assembled documents',
  file_name STRING,
  modification_time TIMESTAMP COMMENT 'Change-detection key together with path',
  file_size BIGINT,
  source_files INT COMMENT '1, or the number of page files assembled into this document',
  page_count INT COMMENT 'Pages in the source (PDF page count before parsing; parsed pages for assembled documents)',
  parse_mode STRING COMMENT 'single, page_range (parsed in slices and stitched), assembled, or skipped',
  parsed VARIANT COMMENT 'ai_parse_document v2 output; NULL when skipped',
  parse_error STRING COMMENT 'JSON error_status, or the skip reason',
  parsed_at TIMESTAMP COMMENT 'Downstream stages key their progress on (path, parsed_at)'
)
COMMENT 'One row per source document, parsed with ai_parse_document (Forge parse stage)';

-- name: create_files_table
-- A no-op unless split pages are on.
CREATE TABLE IF NOT EXISTS {{files_table}} LIKE {{parsed_table}};

-- name: changed_files
-- New files, plus files whose modification time differs from the last parse.
CREATE OR REPLACE TEMP VIEW changed_files AS
SELECT f.path, f.modificationTime AS modification_time, f.length AS file_size
FROM volume_files f
LEFT ANTI JOIN {{files_table}} p
  ON f.path = p.path AND f.modificationTime = p.modification_time;

-- name: delete_batch_files
DELETE FROM {{files_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_batch_single
-- Delete-then-insert rather than MERGE so the AI function is evaluated exactly once per file.
-- PDFs over {{max_pages}} pages go through select_slice and insert_batch_slices instead.
INSERT INTO {{files_table}} BY NAME
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  modification_time,
  file_size,
  1 AS source_files,
  page_count,
  'single' AS parse_mode,
  parsed,
  CASE
    WHEN parsed IS NULL THEN 'ai_parse_document returned NULL'
    ELSE nullif(nullif(to_json(parsed:error_status), '[]'), 'null')
  END AS parse_error,
  current_timestamp() AS parsed_at
FROM (
  SELECT
    f.path,
    f.modificationTime AS modification_time,
    f.length AS file_size,
    c.page_count,
    ai_parse_document(f.content, {{parse_options}}) AS parsed
  FROM batch_files f
  LEFT JOIN batch_page_counts c ON c.path = f.path
  WHERE c.page_count IS NULL OR c.page_count <= {{max_pages}}
);

-- name: select_slice
-- One slice of every long PDF in the batch. pageRange must be a literal, so parse.py renders this once
-- per slice (slice_options carries pageRange '1-500', '501-1000', ...) and unions the results into long_slices.
SELECT
  f.path,
  {{start_page}} AS start_page,
  ai_parse_document(f.content, {{slice_options}}) AS parsed
FROM batch_files f
JOIN batch_page_counts c ON c.path = f.path
WHERE c.page_count > {{start_page}};

-- name: insert_batch_slices
-- forge_stitch (registered by parse.py) joins the slices into one ai_parse_document-shaped document,
-- renumbering page and element ids, and adds an error_status entry for any page no slice returned.
INSERT INTO {{files_table}} BY NAME
WITH stitched AS (
  SELECT
    s.path,
    try_parse_json(forge_stitch(
      collect_list(named_struct('start_page', s.start_page, 'parsed_json', to_json(s.parsed))),
      max(c.page_count)
    )) AS parsed
  FROM long_slices s
  JOIN batch_page_counts c ON c.path = s.path
  GROUP BY s.path
)
SELECT
  f.path,
  regexp_extract(f.path, '[^/]+$', 0) AS file_name,
  f.modificationTime AS modification_time,
  f.length AS file_size,
  1 AS source_files,
  c.page_count,
  'page_range' AS parse_mode,
  s.parsed,
  CASE
    WHEN s.parsed IS NULL THEN 'stitched slices could not be stored as one VARIANT'
    ELSE nullif(nullif(to_json(s.parsed:error_status), '[]'), 'null')
  END AS parse_error,
  current_timestamp() AS parsed_at
FROM batch_files f
JOIN batch_page_counts c ON c.path = f.path
JOIN stitched s ON s.path = f.path;

-- name: insert_oversize
-- Record files over the ai_parse_document size limit so they aren't retried until they change.
INSERT INTO {{files_table}} BY NAME
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  modification_time,
  file_size,
  1 AS source_files,
  CAST(NULL AS INT) AS page_count,
  'skipped' AS parse_mode,
  CAST(NULL AS VARIANT) AS parsed,
  'skipped: file larger than {{max_file_mb}} MB' AS parse_error,
  current_timestamp() AS parsed_at
FROM changed_files
WHERE file_size > {{max_file_bytes}};

-- name: delete_removed_files
DELETE FROM {{files_table}} t
WHERE NOT EXISTS (SELECT 1 FROM volume_files f WHERE f.path = t.path);

-- name: volume_documents
-- Split pages: every folder is one document, built from the page files currently in it.
CREATE OR REPLACE TEMP VIEW volume_documents AS
SELECT
  regexp_extract(path, '^(.*)/[^/]+$', 1) AS path,
  max(modification_time) AS modification_time,
  sum(file_size) AS file_size,
  CAST(count(*) AS INT) AS source_files
FROM {{files_table}}
GROUP BY 1;

-- name: changed_documents
-- Folders with a new, changed, or removed page file since they were last assembled.
CREATE OR REPLACE TEMP VIEW changed_documents AS
SELECT d.path
FROM volume_documents d
LEFT ANTI JOIN {{parsed_table}} p
  ON d.path = p.path AND d.modification_time = p.modification_time AND d.source_files = p.source_files;

-- name: delete_batch_documents
DELETE FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_batch_documents
-- forge_assemble (registered by parse.py) orders the pages by the number in the file name, else by the
-- printed page number, else by file name, then stitches them like insert_batch_slices. No AI function
-- runs here: each page file was parsed once in insert_batch_single.
INSERT INTO {{parsed_table}} BY NAME
WITH grouped AS (
  SELECT
    regexp_extract(path, '^(.*)/[^/]+$', 1) AS path,
    collect_list(named_struct('file_name', file_name, 'parsed_json', to_json(parsed), 'parse_error', parse_error)) AS pages,
    max(modification_time) AS modification_time,
    sum(file_size) AS file_size,
    CAST(count(*) AS INT) AS source_files
  FROM {{files_table}}
  WHERE regexp_extract(path, '^(.*)/[^/]+$', 1) IN (SELECT path FROM batch_paths)
  GROUP BY 1
),
assembled AS (
  SELECT *, try_parse_json(forge_assemble(pages)) AS parsed FROM grouped
)
SELECT
  path,
  regexp_extract(path, '[^/]+$', 0) AS file_name,
  modification_time,
  file_size,
  source_files,
  CAST(size(try_cast(parsed:document.pages AS ARRAY<VARIANT>)) AS INT) AS page_count,
  'assembled' AS parse_mode,
  parsed,
  CASE
    WHEN parsed IS NULL THEN 'assembled pages could not be stored as one VARIANT'
    ELSE nullif(nullif(to_json(parsed:error_status), '[]'), 'null')
  END AS parse_error,
  current_timestamp() AS parsed_at
FROM assembled;

-- name: delete_removed_documents
DELETE FROM {{parsed_table}} t
WHERE NOT EXISTS (SELECT 1 FROM volume_documents d WHERE d.path = t.path);
