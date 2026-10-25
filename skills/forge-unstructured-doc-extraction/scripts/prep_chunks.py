"""Prepare search chunks from parsed documents, optionally with chart insights (forge-unstructured-doc-extraction module 02).

Second task of the index job, after parse.py. Runs as a serverless ``spark_python_task``, or locally
through Databricks Connect:

    uv run --with "databricks-connect>=17" --with pillow python prep_chunks.py --catalog main \
        --schema docs --prefix contracts

The SQL lives in prep_chunks.sql. Each document is chunked once per parse: <prefix>_progress records
the (path, parsed_at) this stage has processed, so ai_prep_search never reruns on an unchanged document,
even one that yields no chunks. With --chart-insights true, charts are cropped from the page images and
read by a vision model first, and each reading is appended to its figure's description before chunking.
"""

import argparse
import io
import json
import os
import re
from pathlib import Path

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
DEFAULT_CHART_MODEL = "system.ai.claude-sonnet-5-5"
CHART_LABELS = {
    "chart": "A chart or graph that plots data: bar, line, pie, area, scatter, combo, waterfall, or similar",
    "other": "Any other figure: a logo, photo, icon, illustration, or a diagram without plotted values",
}
DEFAULT_CHART_PROMPT = (
    "This image is a chart cropped from a document. Describe it so that someone who can't see it can "
    "answer questions about it. Give: the chart type and title; the axes, units, and time periods; every "
    "data point you can read, as a markdown table with exact values as printed; the main trends and any "
    "notable changes; and any footnotes or sources. Don't guess values you can't read: say so instead."
)
MIN_CROP_PIXELS = 32


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: reads <prefix>_parsed, writes <prefix>_chunks")
    p.add_argument("--metadata-schema", default="", help="JSON array of field names, or ai_extract-style object")
    p.add_argument("--chart-insights", choices=["true", "false"], default="false")
    p.add_argument("--chart-model", default="", help=f"system.ai model for chart reading (default {DEFAULT_CHART_MODEL})")
    p.add_argument("--chart-prompt", default="", help="Prompt for chart reading (default: a generic prompt)")
    p.add_argument("--full-refresh", choices=["true", "false"], default="false",
                   help="Re-chunk every parsed document (no re-parse)")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--sql-dir", default="", help="Directory holding prep_chunks.sql (default: next to this file)")
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

    prep_args = "parsed"
    if columns:
        prep_args += f", map('schema', {sql_string(args.metadata_schema)})"

    return {
        "parsed_table": table("parsed"),
        "chunks_table": table("chunks"),
        "progress_table": table("progress"),
        "chart_table": table("chart_insights"),
        "prep_args": prep_args,
        "metadata_column_ddl": "".join(f",\n  {key} {kind}" for key, kind in columns.items()),
        "metadata_column_select": "".join(
            f",\n  try_variant_get(chunk, '$.metadata.{key}', '{kind}') AS {key}" for key, kind in columns.items()
        ),
        "chart_labels": sql_string(json.dumps(CHART_LABELS)),
        "chart_model": sql_string(args.chart_model or DEFAULT_CHART_MODEL),
        "chart_prompt": sql_string(args.chart_prompt or DEFAULT_CHART_PROMPT),
    }


def crop_box(coord: list[float] | None, size: tuple[int, int], pad: int = 8) -> tuple[int, int, int, int] | None:
    """Pixel box for a figure's bbox coord on a page image of ``size``, padded and clamped; None if too small."""
    if not coord or len(coord) < 4:
        return None
    x0, x1 = sorted((float(coord[0]), float(coord[2])))
    y0, y1 = sorted((float(coord[1]), float(coord[3])))
    width, height = size
    box = (max(0, int(x0) - pad), max(0, int(y0) - pad), min(width, int(x1) + pad), min(height, int(y1) + pad))
    if box[2] - box[0] < MIN_CROP_PIXELS or box[3] - box[1] < MIN_CROP_PIXELS:
        return None
    return box


def crop_figure(image_uri: str | None, coord: list[float] | None) -> bytes | None:
    """JPEG bytes of one figure cropped from its page image, or None when it can't be cropped."""
    if not image_uri:
        return None
    from PIL import Image

    try:
        with Image.open(image_uri.removeprefix("dbfs:")) as page:
            box = crop_box(coord, page.size)
            if box is None:
                return None
            buffer = io.BytesIO()
            page.convert("RGB").crop(box).save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()
    except OSError:
        return None


def add_chart_insights(parsed: dict, insights: list[dict]) -> dict:
    """Append each chart insight to the description of the figure element it came from."""
    by_id = {item["element_id"]: item["insight"] for item in insights if item.get("insight")}
    for element in (parsed.get("document") or {}).get("elements") or []:
        insight = by_id.get(element.get("id"))
        if insight and element.get("type") == "figure":
            description = (element.get("description") or "").strip()
            element["description"] = f"{description}\n\nChart reading:\n{insight}".strip()
    return parsed


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def main() -> None:
    args = parse_args()
    columns = metadata_columns(args.metadata_schema)
    params = build_params(args, columns)
    charts = args.chart_insights == "true"
    sql_dir = Path(args.sql_dir) if args.sql_dir else Path(__file__).resolve().parent
    sections = load_sections(sql_dir / "prep_chunks.sql")

    spark = get_spark()
    if charts:
        spark.udf.register("forge_crop", crop_figure, "binary")
        spark.udf.register(
            "forge_add_chart_insights",
            lambda parsed_json, insights_json: json.dumps(add_chart_insights(json.loads(parsed_json), json.loads(insights_json))),
            "string",
        )

    def run(name: str):
        return spark.sql(render(sections[name], params))

    def set_batch_paths(paths: list[str]) -> None:
        spark.createDataFrame([(p,) for p in paths], "path STRING").createOrReplaceTempView("batch_paths")

    run("create_chunks_table")
    run("create_progress_table")
    if charts:
        run("create_chart_insights_table")
    existing = set(spark.table(params["chunks_table"]).columns)
    if missing := sorted(set(columns) - existing):
        raise SystemExit(
            f"{params['chunks_table']} lacks metadata columns {missing}. The metadata schema changed: drop the "
            "chunks table and its index (with the user's confirmation), then run the job with full_refresh=true."
        )

    if args.full_refresh == "true":
        run("delete_all_chunks")
        run("reset_progress")

    run("pending_documents")
    pending = [r.path for r in spark.table("pending_documents").collect()]
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        set_batch_paths(batch)
        if charts:
            run("delete_batch_chart_insights")
            run("insert_batch_chart_insights")
            run("batch_documents_with_charts")
        else:
            run("batch_documents")
        run("delete_batch_chunks")
        run("insert_batch_chunks")
        run("record_batch_progress")
        print(f"chunked {start + len(batch)}/{len(pending)} documents")

    run("delete_orphan_chunks")
    run("delete_stale_progress")
    if charts:
        run("delete_orphan_chart_insights")

    summary = {"documents_chunked": len(pending), "chunks": spark.table(params["chunks_table"]).count()}
    if charts:
        insights = spark.sql(
            f"SELECT count(*) AS charts, count_if(error IS NOT NULL) AS errors FROM {params['chart_table']}"
        ).first()
        summary.update(charts_read=insights.charts, chart_errors=insights.errors)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
