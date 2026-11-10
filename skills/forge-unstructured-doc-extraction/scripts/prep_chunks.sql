-- Chunk stage for the forge-unstructured-doc-extraction use-case skill (module 02), run by prep_chunks.py after parse.py.
-- prep_chunks.py builds the metadata fragments from --metadata-schema; each key becomes a typed, filterable column.
-- Temp views provided by prep_chunks.py: batch_paths(path).
-- A document is chunked once per parse: <prefix>_progress records (path, parsed_at) for stage 'chunk'.

-- name: create_chunks_table
CREATE TABLE IF NOT EXISTS {{chunks_table}} (
  chunk_id STRING NOT NULL COMMENT 'md5(source_uri#chunk_position); index primary key',
  source_uri STRING COMMENT 'Source document path',
  file_name STRING,
  chunk_position INT COMMENT '0-based chunk position within the document',
  page_numbers ARRAY<INT> COMMENT '1-based pages the chunk spans',
  chunk_to_retrieve STRING COMMENT 'Raw chunk text',
  chunk_to_embed STRING COMMENT 'Context-enriched text from ai_prep_search; embedding source',
  prepped_at TIMESTAMP{{metadata_column_ddl}}
)
COMMENT 'Search chunks for an AI Search index (forge-unstructured-doc-extraction, ai_prep_search)'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- name: create_progress_table
CREATE TABLE IF NOT EXISTS {{progress_table}} (
  path STRING NOT NULL,
  parsed_at TIMESTAMP NOT NULL COMMENT 'parsed_at of the <prefix>_parsed row this stage processed',
  stage STRING NOT NULL,
  done_at TIMESTAMP
)
COMMENT 'Which parse of each document each downstream stage has processed';

-- name: create_chart_insights_table
CREATE TABLE IF NOT EXISTS {{chart_table}} (
  path STRING NOT NULL,
  parsed_at TIMESTAMP,
  element_id INT COMMENT 'Figure element id in the parsed document',
  page_id INT,
  insight STRING COMMENT 'Vision model reading of the chart: values, axes, trends',
  error STRING,
  analyzed_at TIMESTAMP
)
COMMENT 'Chart insights spliced into figure descriptions before chunking (forge-unstructured-doc-extraction)';

-- name: pending_documents
-- Parsed documents whose current parse hasn't been chunked yet.
CREATE OR REPLACE TEMP VIEW pending_documents AS
SELECT p.path
FROM {{parsed_table}} p
WHERE p.parsed IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM {{progress_table}} g
    WHERE g.stage = 'chunk' AND g.path = p.path AND g.parsed_at = p.parsed_at
  );

-- name: delete_batch_chart_insights
DELETE FROM {{chart_table}} WHERE path IN (SELECT path FROM batch_paths);

-- name: insert_batch_chart_insights
-- One reading per chart figure. A figure whose content holds the parser's chart data (with figure descriptions
-- on) is a chart; with chart_source vision, other described figures are classified as chart or not from their
-- description. Each chart gets its page context (forge_figure_context: title, units, legend), because a crop
-- loses them and the parser's chart data has no title. builtin_only: the reading is the parser's chart data as
-- a table (forge_chart_table), with no crop and no model call. Otherwise the chart is cropped from its page
-- image, or from its PDF page rendered at 200 DPI (forge_crop), and read by a vision model, with the context
-- and the parser's data as hints. failOnError => false keeps one bad image from failing the batch, and a chart
-- that can't be cropped gets an error row instead of being skipped.
INSERT INTO {{chart_table}} BY NAME
WITH elements AS (
  SELECT
    p.path,
    p.parsed_at,
    p.parsed,
    e.value:id::INT AS element_id,
    e.value:type::STRING AS type,
    e.value:bbox[0].page_id::INT AS page_id,
    try_variant_get(e.value, '$.bbox[0].coord', 'ARRAY<DOUBLE>') AS coord,
    e.value:content::STRING AS content,
    e.value:description::STRING AS description
  FROM {{parsed_table}} p, LATERAL variant_explode(p.parsed:document.elements) AS e
  WHERE p.path IN (SELECT path FROM batch_paths) AND p.parsed IS NOT NULL
),
page_elements AS (
  SELECT path, page_id,
    to_json(collect_list(named_struct('id', element_id, 'type', type, 'content', content, 'coord', coord))) AS page_json
  FROM elements
  GROUP BY path, page_id
),
figures AS (
  SELECT f.*, forge_chart_table(f.content) AS builtin, pe.page_json
  FROM elements f
  JOIN page_elements pe ON pe.path = f.path AND pe.page_id <=> f.page_id
  WHERE f.type = 'figure'
),
charts AS (
  SELECT
    path, parsed_at, element_id, page_id, coord, builtin,
    forge_figure_context(page_json, element_id) AS context,
    variant_get(
      try_element_at(filter(try_cast(parsed:document.pages AS ARRAY<VARIANT>), pg -> pg:id::INT = page_id), 1),
      '$.image_uri', 'STRING'
    ) AS image_uri
  FROM figures
  WHERE CASE
    WHEN builtin IS NOT NULL THEN true
    WHEN {{builtin_only}} OR description IS NULL THEN false
    ELSE ai_classify(description, {{chart_labels}}, map('version', '2.1')):response[0].value::STRING = 'chart'
  END
),
cropped AS (
  SELECT *, CASE WHEN {{builtin_only}} THEN NULL ELSE forge_crop(image_uri, coord, path, page_id) END AS image
  FROM charts
),
analyzed AS (
  SELECT *,
    CASE WHEN NOT {{builtin_only}} AND image IS NOT NULL THEN
      ai_query(
        {{chart_model}},
        concat({{chart_prompt}}, '\n\nPage context (title, units, legend):\n', coalesce(context, ''),
               coalesce(concat('\n\nThe parser read this chart as this table, which may lack the title, units, and series names, or be wrong:\n', builtin), '')),
        files => image,
        failOnError => false
      )
    END AS answer
  FROM cropped
)
SELECT
  path, parsed_at, element_id, page_id,
  CASE WHEN {{builtin_only}} THEN concat_ws('\n\n', nullif(context, ''), builtin) ELSE answer.result END AS insight,
  CASE
    WHEN {{builtin_only}} THEN NULL
    WHEN image IS NULL THEN 'not cropped: no page image and not a readable PDF page, or the figure is too small'
    ELSE answer.errorMessage
  END AS error,
  current_timestamp() AS analyzed_at
FROM analyzed;

-- name: batch_documents
CREATE OR REPLACE TEMP VIEW batch_documents AS
SELECT path, file_name, parsed
FROM {{parsed_table}}
WHERE path IN (SELECT path FROM batch_paths) AND parsed IS NOT NULL;

-- name: batch_documents_with_charts
-- Same documents, with each chart's insight appended to its figure description (forge_add_chart_insights,
-- registered by prep_chunks.py). ai_prep_search reads figure descriptions, so the insights reach
-- chunk_to_embed and chunk_to_retrieve, with the chart's page number intact.
CREATE OR REPLACE TEMP VIEW batch_documents AS
WITH insights AS (
  SELECT path, to_json(collect_list(named_struct('element_id', element_id, 'insight', insight))) AS insights_json
  FROM {{chart_table}}
  WHERE path IN (SELECT path FROM batch_paths) AND insight IS NOT NULL
  GROUP BY path
)
SELECT
  p.path,
  p.file_name,
  CASE
    WHEN i.insights_json IS NULL THEN p.parsed
    ELSE coalesce(try_parse_json(forge_add_chart_insights(to_json(p.parsed), i.insights_json)), p.parsed)
  END AS parsed
FROM {{parsed_table}} p
LEFT JOIN insights i ON i.path = p.path
WHERE p.path IN (SELECT path FROM batch_paths) AND p.parsed IS NOT NULL;

-- name: delete_batch_chunks
DELETE FROM {{chunks_table}} WHERE source_uri IN (SELECT path FROM batch_paths);

-- name: insert_batch_chunks
INSERT INTO {{chunks_table}} BY NAME
WITH prepped AS (
  SELECT path, file_name, ai_prep_search({{prep_args}}) AS prep
  FROM batch_documents
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

-- name: record_batch_progress
MERGE INTO {{progress_table}} t
USING (SELECT path, parsed_at FROM {{parsed_table}} WHERE path IN (SELECT path FROM batch_paths)) s
ON t.stage = 'chunk' AND t.path = s.path
WHEN MATCHED THEN UPDATE SET t.parsed_at = s.parsed_at, t.done_at = current_timestamp()
WHEN NOT MATCHED THEN INSERT (path, parsed_at, stage, done_at)
  VALUES (s.path, s.parsed_at, 'chunk', current_timestamp());

-- name: delete_orphan_chunks
-- Chunks whose document was removed from the volume, or whose latest parse failed.
DELETE FROM {{chunks_table}} c
WHERE NOT EXISTS (
  SELECT 1 FROM {{parsed_table}} p WHERE p.path = c.source_uri AND p.parsed IS NOT NULL
);

-- name: delete_orphan_chart_insights
DELETE FROM {{chart_table}} c
WHERE NOT EXISTS (
  SELECT 1 FROM {{parsed_table}} p WHERE p.path = c.path AND p.parsed_at = c.parsed_at
);

-- name: delete_stale_progress
DELETE FROM {{progress_table}} g
WHERE g.stage = 'chunk' AND NOT EXISTS (SELECT 1 FROM {{parsed_table}} p WHERE p.path = g.path);

-- name: reset_progress
DELETE FROM {{progress_table}} WHERE stage = 'chunk';

-- name: delete_all_chunks
DELETE FROM {{chunks_table}};
