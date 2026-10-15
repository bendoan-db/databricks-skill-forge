"""Incrementally parse documents in a UC volume and prepare search chunks (doc-qa-agent modules 01-02).

Runs as a serverless ``spark_python_task`` from databricks.yml, or locally through Databricks Connect:

    uv run --with "databricks-connect>=17" python ingest.py --catalog main --schema docs \
        --prefix contracts --volume-path /Volumes/main/docs/contracts

The SQL lives in parse_documents.sql and prep_chunks.sql. This file only lists files, picks batches,
and fills placeholders. Only new or changed files are parsed (keyed on path + modification time),
and files removed from the volume are dropped from both tables.
"""

import argparse
import json
import os
import re
from pathlib import Path

SUPPORTED_EXTENSIONS = ("pdf", "jpg", "jpeg", "png", "tif", "tiff", "doc", "docx", "ppt", "pptx")
MAX_FILE_BYTES = 100 * 1024 * 1024  # ai_parse_document limit
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SECTION_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$")
# ai_extract-style schema types that map to filterable index columns.
METADATA_TYPES = {"string": "STRING", "enum": "STRING", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN"}
BASE_CHUNK_COLUMNS = {
    "chunk_id", "source_uri", "file_name", "chunk_position", "page_numbers",
    "chunk_to_retrieve", "chunk_to_embed", "prepped_at",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: <prefix>_parsed, <prefix>_chunks")
    p.add_argument("--volume-path", required=True, help="/Volumes/<catalog>/<schema>/<volume>[/subdir]")
    p.add_argument("--metadata-schema", default="", help="JSON array of field names, or ai_extract-style object")
    p.add_argument("--description-element-types", default="*", help="'*', 'figure', or '' to skip descriptions")
    p.add_argument("--page-images-path", default="", help="Volume directory for rendered page images")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--full-refresh", action="store_true", help="Re-chunk every parsed file (no re-parse)")
    p.add_argument("--sql-dir", default="", help="Directory holding the .sql files (default: next to this file)")
    return p.parse_args()


def sql_string(value: str) -> str:
    """Quote a value as a Databricks SQL string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_sections(path: Path) -> dict[str, str]:
    """Split a .sql file into its ``-- name: <section>`` statements."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in path.read_text().splitlines():
        if match := SECTION_RE.match(line):
            current = match.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip().rstrip(";") for name, lines in sections.items()}


def render(sql: str, params: dict[str, str]) -> str:
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in params:
            raise KeyError(f"No value for placeholder {{{{{key}}}}}")
        return params[key]

    return PLACEHOLDER_RE.sub(substitute, sql)


def metadata_columns(raw: str) -> dict[str, str]:
    """Return {column: SQL type} for the metadata schema, rejecting shapes the index can't filter on."""
    if not raw.strip():
        return {}
    spec = json.loads(raw)
    if isinstance(spec, list):
        spec = {key: {"type": "string"} for key in spec}
    if not isinstance(spec, dict) or not spec:
        raise ValueError("--metadata-schema must be a JSON array of names or an object of field specs")
    columns = {}
    for key, field in spec.items():
        kind = (field or {}).get("type", "string") if isinstance(field, dict) else "string"
        if not METADATA_KEY_RE.match(key) or key in BASE_CHUNK_COLUMNS:
            raise ValueError(f"Metadata key {key!r} must be snake_case and not clash with {sorted(BASE_CHUNK_COLUMNS)}")
        if kind not in METADATA_TYPES:
            raise ValueError(f"Metadata key {key!r} has type {kind!r}; use one of {sorted(METADATA_TYPES)}")
        columns[key] = METADATA_TYPES[kind]
    return columns


def build_params(args: argparse.Namespace, columns: dict[str, str]) -> dict[str, str]:
    for name in (args.catalog, args.schema, args.prefix):
        if not NAME_RE.match(name):
            raise ValueError(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731

    parse_options = ["'version'", "'2.0'", "'descriptionElementTypes'", sql_string(args.description_element_types)]
    if args.page_images_path:
        parse_options += ["'imageOutputPath'", sql_string(args.page_images_path.rstrip("/") + "/")]

    prep_args = "parsed"
    if columns:
        prep_args += f", map('schema', {sql_string(args.metadata_schema)})"

    return {
        "parsed_table": table("parsed"),
        "chunks_table": table("chunks"),
        "parse_options": f"map({', '.join(parse_options)})",
        "prep_args": prep_args,
        "metadata_column_ddl": "".join(f",\n  {key} {kind}" for key, kind in columns.items()),
        "metadata_column_select": "".join(
            f",\n  try_variant_get(chunk, '$.metadata.{key}', '{kind}') AS {key}" for key, kind in columns.items()
        ),
        "max_file_bytes": str(MAX_FILE_BYTES),
        "max_file_mb": str(MAX_FILE_BYTES // (1024 * 1024)),
    }


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def main() -> None:
    args = parse_args()
    if not re.match(r"^/Volumes/[^/]+/[^/]+/[^/]+", args.volume_path):
        raise ValueError("--volume-path must look like /Volumes/<catalog>/<schema>/<volume>[/subdir]")
    columns = metadata_columns(args.metadata_schema)
    params = build_params(args, columns)
    sql_dir = Path(args.sql_dir) if args.sql_dir else Path(__file__).resolve().parent
    parse_sql = load_sections(sql_dir / "parse_documents.sql")
    prep_sql = load_sections(sql_dir / "prep_chunks.sql")

    spark = get_spark()
    from pyspark.sql import functions as F

    def run(sections: dict[str, str], name: str):
        return spark.sql(render(sections[name], params))

    def set_batch_paths(paths: list[str]) -> None:
        spark.createDataFrame([(p,) for p in paths], "path STRING").createOrReplaceTempView("batch_paths")

    run(parse_sql, "create_parsed_table")
    run(prep_sql, "create_chunks_table")
    existing = set(spark.table(params["chunks_table"]).columns)
    if missing := sorted(set(columns) - existing):
        raise SystemExit(
            f"{params['chunks_table']} lacks metadata columns {missing}. The metadata schema changed: drop the "
            "chunks table and its index (with the user's confirmation), then rerun with --full-refresh."
        )

    # Listing reads only file metadata: binaryFile skips `content` when it isn't selected.
    files = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .load(args.volume_path)
        .select("path", "modificationTime", "length")
        .where(F.lower(F.regexp_extract("path", r"\.([^./]+)$", 1)).isin(*SUPPORTED_EXTENSIONS))
    )
    files.createOrReplaceTempView("volume_files")
    run(parse_sql, "changed_files")

    changed = spark.table("changed_files").select("path", "file_size").collect()
    oversize = [r.path for r in changed if r.file_size > MAX_FILE_BYTES]
    to_parse = [r.path for r in changed if r.file_size <= MAX_FILE_BYTES]
    if oversize:
        set_batch_paths(oversize)
        run(parse_sql, "delete_batch_parsed")
        run(parse_sql, "insert_oversize")

    for start in range(0, len(to_parse), args.batch_size):
        batch = to_parse[start : start + args.batch_size]
        spark.read.format("binaryFile").load(batch).createOrReplaceTempView("batch_files")
        set_batch_paths(batch)
        run(parse_sql, "delete_batch_parsed")
        run(parse_sql, "insert_batch_parsed")
        if not args.full_refresh:
            run(prep_sql, "delete_batch_chunks")
            run(prep_sql, "insert_batch_chunks")
        print(f"parsed and chunked {start + len(batch)}/{len(to_parse)} files")

    if args.full_refresh:
        run(prep_sql, "delete_all_chunks")
        parsed = spark.table(params["parsed_table"]).where("parsed IS NOT NULL").select("path")
        parsed_paths = [r.path for r in parsed.collect()]
        for start in range(0, len(parsed_paths), args.batch_size):
            set_batch_paths(parsed_paths[start : start + args.batch_size])
            run(prep_sql, "insert_batch_chunks")
            print(f"re-chunked {min(start + args.batch_size, len(parsed_paths))}/{len(parsed_paths)} files")

    # An empty listing usually means a wrong path or missing grant; never treat it as "delete everything".
    if files.limit(1).count() == 0:
        print(f"WARNING: no supported files under {args.volume_path}; skipping deletion of removed files")
    else:
        run(parse_sql, "delete_removed_files")
        run(prep_sql, "delete_orphan_chunks")

    summary = spark.sql(
        f"SELECT count(*) AS files, count_if(parse_error IS NOT NULL) AS files_with_errors, "
        f"count_if(parsed IS NULL) AS files_skipped FROM {params['parsed_table']}"
    ).first()
    chunks = spark.table(params["chunks_table"]).count()
    print(
        json.dumps(
            {
                "changed_files": len(to_parse) + len(oversize),
                "oversize_skipped": len(oversize),
                "files": summary.files,
                "files_with_errors": summary.files_with_errors,
                "files_skipped": summary.files_skipped,
                "chunks": chunks,
            }
        )
    )


if __name__ == "__main__":
    main()
