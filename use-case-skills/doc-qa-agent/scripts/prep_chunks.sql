-- Chunk stage for the doc-qa-agent use-case skill (module 02).
-- ingest.py builds the metadata fragments from --metadata-schema; each key becomes a typed, filterable column.
-- Temp view provided by ingest.py: batch_paths(path).

-- name: create_chunks_table
CREATE TABLE IF NOT EXISTS {{chunks_table}} (
  chunk_id STRING NOT NULL COMMENT 'md5(source_uri#chunk_position); index primary key',
  source_uri STRING COMMENT 'Source file path',
  file_name STRING,
  chunk_position INT COMMENT '0-based chunk position within the file',
  page_numbers ARRAY<INT> COMMENT '1-based pages the chunk spans',
  chunk_to_retrieve STRING COMMENT 'Raw chunk text',
  chunk_to_embed STRING COMMENT 'Context-enriched text from ai_prep_search; embedding source',
  prepped_at TIMESTAMP{{metadata_column_ddl}}
)
COMMENT 'Search chunks for the doc-qa-agent AI Search index (ai_prep_search)'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: delete_batch_chunks
DELETE FROM {{chunks_table}} WHERE source_uri IN (SELECT path FROM batch_paths);

-- name: insert_batch_chunks
INSERT INTO {{chunks_table}} BY NAME
WITH prepped AS (
  SELECT path, file_name, ai_prep_search({{prep_args}}) AS prep
  FROM {{parsed_table}}
  WHERE path IN (SELECT path FROM batch_paths) AND parsed IS NOT NULL
),
exploded AS (
  SELECT p.path, p.file_name, c.pos, c.value AS chunk
  FROM prepped p, LATERAL variant_explode(p.prep:document.contents) AS c
),
positioned AS (
  SELECT *, coalesce(chunk:chunk_position::INT, pos) AS position FROM exploded
)
SELECT
  md5(concat(path, '#', CAST(position AS STRING))) AS chunk_id,
  path AS source_uri,
  file_name,
  position AS chunk_position,
  transform(
    try_variant_get(chunk, '$.pages', 'ARRAY<STRUCT<page_id: INT, image_uri: STRING>>'),
    pg -> pg.page_id + 1
  ) AS page_numbers,
  chunk:chunk_to_retrieve::STRING AS chunk_to_retrieve,
  -- Delta Sync caps the embedding source at 32,764 bytes; 8,000 chars is safe for any UTF-8 text.
  CASE
    WHEN octet_length(chunk:chunk_to_embed::STRING) > 32000 THEN left(chunk:chunk_to_embed::STRING, 8000)
    ELSE chunk:chunk_to_embed::STRING
  END AS chunk_to_embed,
  current_timestamp() AS prepped_at{{metadata_column_select}}
FROM positioned
WHERE chunk:chunk_to_embed::STRING IS NOT NULL;

-- name: delete_orphan_chunks
-- Chunks whose file was removed from the volume, or whose latest parse failed.
DELETE FROM {{chunks_table}} c
WHERE NOT EXISTS (
  SELECT 1 FROM {{parsed_table}} p WHERE p.path = c.source_uri AND p.parsed IS NOT NULL
);

-- name: delete_all_chunks
DELETE FROM {{chunks_table}};
