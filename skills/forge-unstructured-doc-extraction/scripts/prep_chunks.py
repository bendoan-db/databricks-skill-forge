# Databricks notebook source
# MAGIC %md
# MAGIC # Prepare search chunks
# MAGIC
# MAGIC The `chunk` task of the forge-unstructured-doc-extraction index job: it splits newly parsed documents into
# MAGIC search chunks with `ai_prep_search`, optionally reading charts with a vision model first. The job passes its
# MAGIC settings as the widgets at the top; to run it by hand, fill them in. To keep a change you make here, export
# MAGIC this notebook into the bundle's `src/` folder before the next deploy, which otherwise replaces it (module 01).

# COMMAND ----------

# MAGIC %pip install -q "pillow>=10" "pypdfium2>=4"

# COMMAND ----------

"""Prepare search chunks from parsed documents, optionally with chart insights (forge-unstructured-doc-extraction module 02).

Second task of the index job, after parse.py. It's a Databricks notebook: it runs as a serverless notebook
task whose parameters arrive as widgets, or locally through Databricks Connect, with the same settings as
flags:

    uv run --with "databricks-connect>=17" --with pillow --with pypdfium2 python prep_chunks.py --catalog main \
        --schema docs --prefix contracts

The SQL lives in prep_chunks.sql. Each document is chunked once per parse: <prefix>_progress records
the (path, parsed_at) this stage has processed, so ai_prep_search never reruns on an unchanged document,
even one that yields no chunks. With --chart-insights true, each chart gets a reading, which is appended to
its figure's description before chunking (ai_prep_search embeds figure descriptions, not the chart data in
figure content). With --chart-source vision, the chart is cropped from its page image, or from the PDF page
rendered at the parser's 200 DPI, and read by a vision model, with the page's title, units, and legend and the
parser's chart data as context. With --chart-source builtin, the reading is the parser's chart data as a table,
with that context: no crops and no model calls.
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
    "notable changes; and any footnotes or sources. Don't guess values you can't read: say so instead. "
    "The crop often leaves out the title, units, and legend: take them from the page context below, and "
    "don't invent them."
)
CHART_SOURCES = ("vision", "builtin")
MIN_CROP_PIXELS = 32
CROP_PAD = 20         # boxes touch their content and move 2-3 px between parses
PARSE_DPI = 200       # bbox coords are pixels of the page rendered at 200 DPI, like the saved page images
CONTEXT_TYPES = ("title", "section_header", "caption")
NEARBY_TYPES = ("text", "footnote")  # legends are often parsed as footnotes beside the chart
MAX_CONTEXT_CHARS = 80               # short text anywhere on the page: units such as "In Millions"
ADJACENT_GAP = 150                   # nearby text within this many pixels is context ...
MAX_ADJACENT_CHARS = 400             # ... unless it's long body text

# COMMAND ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])  # a notebook has no __doc__
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: reads <prefix>_parsed, writes <prefix>_chunks")
    p.add_argument("--metadata-schema", default="", help="JSON array of field names, or ai_extract-style object")
    p.add_argument("--chart-insights", choices=["true", "false"], default="false")
    p.add_argument("--chart-source", choices=CHART_SOURCES, default="vision",
                   help="vision: read each chart crop with --chart-model; builtin: use the parser's chart data")
    p.add_argument("--chart-model", default="", help=f"system.ai model for chart reading (default {DEFAULT_CHART_MODEL})")
    p.add_argument("--chart-prompt", default="", help="Prompt for chart reading (default: a generic prompt)")
    p.add_argument("--full-refresh", choices=["true", "false"], default="false",
                   help="Re-chunk every parsed document (no re-parse)")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--sql-dir", default="", help="Directory holding prep_chunks.sql (default: next to this file)")
    return p


def notebook_argv(parser: argparse.ArgumentParser) -> list[str]:
    """The notebook's widgets as command-line flags. A job's notebook task passes each setting as a widget named
    like its flag (--volume-path is volume_path); a required one left empty is reported as missing."""
    argv = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public list of options
        if not action.option_strings or action.dest == "help":
            continue
        dbutils.widgets.text(action.dest, "" if action.default is None else str(action.default))  # noqa: F821
        value = dbutils.widgets.get(action.dest)  # noqa: F821 - dbutils exists in Databricks notebooks
        if value or action.default is not None:
            argv += [action.option_strings[0], value]
    return argv


def script_dir() -> Path:
    """This file's folder: next to the script, or a notebook's own workspace folder, its working directory."""
    return Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


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

# COMMAND ----------

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
        "builtin_only": "true" if args.chart_source == "builtin" else "false",
    }

# COMMAND ----------

def crop_box(coord: list[float] | None, size: tuple[int, int], pad: int = CROP_PAD) -> tuple[int, int, int, int] | None:
    """Pixel box for a figure's bbox coord on a 200-DPI page image of ``size``, padded and clamped; None if too small."""
    if not coord or len(coord) < 4:
        return None
    x0, x1 = sorted((float(coord[0]), float(coord[2])))
    y0, y1 = sorted((float(coord[1]), float(coord[3])))
    width, height = size
    box = (max(0, int(x0) - pad), max(0, int(y0) - pad), min(width, int(x1) + pad), min(height, int(y1) + pad))
    if box[2] - box[0] < MIN_CROP_PIXELS or box[3] - box[1] < MIN_CROP_PIXELS:
        return None
    return box


_rendered: dict = {}  # the last PDF page rendered by this worker; a document's charts usually share pages


def pdf_page(path: str, page_id: int):
    """One page of a PDF in a volume, rendered at the parser's 200 DPI (pixel-aligned with its bbox coords)."""
    import pypdfium2 as pdfium

    key = (path, page_id)
    if key not in _rendered:
        _rendered.clear()  # a 200-DPI page is tens of MB: keep one
        pdf = pdfium.PdfDocument(path.removeprefix("dbfs:"))
        _rendered[key] = pdf[page_id].render(scale=PARSE_DPI / 72).to_pil()
    return _rendered[key]


def crop_figure(image_uri: str | None, coord: list[float] | None, path: str | None = None,
                page_id: int | None = None) -> bytes | None:
    """JPEG bytes of one figure cropped from its page image or, without one, from its PDF page rendered at
    200 DPI; None when it can't be cropped (no image and not a PDF, an unreadable file, or a tiny figure)."""
    from PIL import Image

    try:
        if image_uri:
            with Image.open(image_uri.removeprefix("dbfs:")) as image:
                page = image.convert("RGB")
        elif path and path.lower().endswith(".pdf") and page_id is not None and page_id >= 0:
            page = pdf_page(path, page_id).convert("RGB")
        else:
            return None
    except (OSError, RuntimeError, IndexError):  # unreadable image or PDF (PdfiumError is a RuntimeError), bad page
        return None
    box = crop_box(coord, page.size)
    if box is None:
        return None
    buffer = io.BytesIO()
    page.crop(box).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _adjacent(a: list[float], b: list[float], gap: float) -> bool:
    """Boxes overlap on one axis and are within ``gap`` pixels on the other."""
    ax0, ax1 = sorted((a[0], a[2]))
    ay0, ay1 = sorted((a[1], a[3]))
    bx0, bx1 = sorted((b[0], b[2]))
    by0, by1 = sorted((b[1], b[3]))
    x_gap, y_gap = max(bx0 - ax1, ax0 - bx1, 0), max(by0 - ay1, ay0 - by1, 0)
    return (ay0 < by1 and by0 < ay1 and x_gap <= gap) or (ax0 < bx1 and bx0 < ax1 and y_gap <= gap)


def figure_context(page_elements: list[dict], figure_id: int) -> str:
    """Text a chart crop usually lacks, from the elements on its page ({id, type, content, coord}), in reading
    order: titles, section headers, and captions; short text anywhere on the page (units such as "In Millions");
    and text or footnotes beside the figure (legends)."""
    figure = next((e for e in page_elements if e.get("id") == figure_id), None)
    if figure is None or not figure.get("coord"):
        return ""
    picked = []
    for element in page_elements:
        text = (element.get("content") or "").strip()
        kind, coord = element.get("type"), element.get("coord")
        if element is figure or not text or not coord or kind == "figure":
            continue
        if (kind in CONTEXT_TYPES or (kind == "text" and len(text) <= MAX_CONTEXT_CHARS)
                or (kind in NEARBY_TYPES and len(text) <= MAX_ADJACENT_CHARS
                    and _adjacent(figure["coord"], coord, ADJACENT_GAP))):
            picked.append((min(coord[1], coord[3]), min(coord[0], coord[2]), text))
    return "\n".join(text for *_, text in sorted(picked))


def chart_table(content: str | None) -> str | None:
    """The chart data ai_parse_document writes into a chart figure's content (with figure descriptions on), as a
    markdown table; None when the content isn't chart data (a logo's text, or descriptions off)."""
    try:
        data = json.loads(content or "")
    except ValueError:
        return None
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict) or not values:
        return None
    cell = lambda v: "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    if all(isinstance(v, dict) for v in values.values()):  # series -> {category: value}
        categories = list(dict.fromkeys(c for series in values.values() for c in series))
        rows = [[name, *(series.get(c) for c in categories)] for name, series in values.items()]
        header = ["Series", *categories]
    else:  # one series: {category: value}
        header, rows = ["Category", "Value"], [[c, v] for c, v in values.items()]
    lines = ["| " + " | ".join(cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    notes = [f"{key}: {data[key]}" for key in ("title", "additional_info") if str(data.get(key, "None")) not in ("None", "")]
    return "\n".join(notes + lines)


def add_chart_insights(parsed: dict, insights: list[dict]) -> dict:
    """Append each chart insight to the description of the figure element it came from."""
    by_id = {item["element_id"]: item["insight"] for item in insights if item.get("insight")}
    for element in (parsed.get("document") or {}).get("elements") or []:
        insight = by_id.get(element.get("id"))
        if insight and element.get("type") == "figure":
            description = (element.get("description") or "").strip()
            element["description"] = f"{description}\n\nChart reading:\n{insight}".strip()
    return parsed

# COMMAND ----------

def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksEnv, DatabricksSession

    # Run locally, the chart UDFs execute on serverless workers, which need the packages the notebook %pip installs.
    env = DatabricksEnv().withDependencies(["pillow>=10", "pypdfium2>=4"])
    return DatabricksSession.builder.serverless().withEnvironment(env).getOrCreate()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    columns = metadata_columns(args.metadata_schema)
    params = build_params(args, columns)
    charts = args.chart_insights == "true"
    sql_dir = Path(args.sql_dir) if args.sql_dir else script_dir()
    sections = load_sections(sql_dir / "prep_chunks.sql")

    spark = get_spark()
    if charts:
        spark.udf.register("forge_crop", crop_figure, "binary")
        spark.udf.register("forge_chart_table", chart_table, "string")
        spark.udf.register(
            "forge_figure_context",
            lambda page_json, figure_id: figure_context(json.loads(page_json or "[]"), figure_id),
            "string",
        )
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

# COMMAND ----------

# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.
if __name__ == "__main__":
    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)
