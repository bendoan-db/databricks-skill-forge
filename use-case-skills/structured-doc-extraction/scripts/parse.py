"""Incrementally parse the documents in a UC volume with ai_parse_document (shared Forge parse stage, module 01).

Every use-case skill that parses documents carries an identical copy of this file and parse.sql;
tests/test_shared_parse.py fails if the copies drift. Runs as the first task of the skill's job, as a
serverless ``spark_python_task``, or locally through Databricks Connect:

    uv run --with "databricks-connect>=17" --with pypdf python parse.py --catalog main --schema docs \
        --prefix contracts --volume-path /Volumes/main/docs/contracts

Only new or changed files are parsed (keyed on path + modification time), and files removed from the
volume are dropped. Beyond the plain call it handles two input shapes:

- PDFs over 500 pages, which ai_parse_document rejects without pageRange: parsed in 500-page slices
  (pageRange must be a literal, so one statement per slice) and stitched into one document.
- --split-pages folder: every folder holds the single-page files of one document. Each file is parsed
  once, then the folder's pages are assembled into one document. No AI function runs on assembly.

Either way <prefix>_parsed keeps one ai_parse_document-shaped row per document, so downstream stages
don't care how a document arrived.
"""

import argparse
import io
import json
import os
import re
from functools import reduce
from pathlib import Path

SUPPORTED_EXTENSIONS = ("pdf", "jpg", "jpeg", "png", "tif", "tiff", "doc", "docx", "ppt", "pptx")
MAX_FILE_BYTES = 100 * 1024 * 1024  # ai_parse_document file size limit
MAX_PAGES = 500  # ai_parse_document page limit per call; longer PDFs are parsed in slices
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SECTION_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$")
LAST_NUMBER_RE = re.compile(r"(\d+)(?!.*\d)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: <prefix>_parsed")
    p.add_argument("--volume-path", required=True, help="/Volumes/<catalog>/<schema>/<volume>[/subdir]")
    p.add_argument("--description-element-types", default="*", help="'*', 'figure', or '' to skip descriptions")
    p.add_argument("--page-images-path", default="", help="Volume directory for rendered page images")
    p.add_argument("--split-pages", choices=["none", "folder"], default="none",
                   help="folder: each folder holds the single-page files of one document")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--sql-dir", default="", help="Directory holding parse.sql (default: next to this file)")
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


def parse_options(args: argparse.Namespace, page_range: str = "", image_subdir: str = "") -> str:
    options = ["'version'", "'2.0'", "'descriptionElementTypes'", sql_string(args.description_element_types)]
    if args.page_images_path:
        images = args.page_images_path.rstrip("/") + "/" + (image_subdir + "/" if image_subdir else "")
        options += ["'imageOutputPath'", sql_string(images)]
    if page_range:
        options += ["'pageRange'", sql_string(page_range)]
    return f"map({', '.join(options)})"


def build_params(args: argparse.Namespace) -> dict[str, str]:
    for name in (args.catalog, args.schema, args.prefix):
        if not NAME_RE.match(name):
            raise ValueError(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731
    return {
        "parsed_table": table("parsed"),
        "files_table": table("page_files") if args.split_pages == "folder" else table("parsed"),
        "parse_options": parse_options(args),
        "max_file_bytes": str(MAX_FILE_BYTES),
        "max_file_mb": str(MAX_FILE_BYTES // (1024 * 1024)),
        "max_pages": str(MAX_PAGES),
    }


def page_ranges(page_count: int, limit: int = MAX_PAGES) -> list[str]:
    """1-based pageRange literals covering page_count pages: ['1-500', '501-1000', '1001-1203']."""
    return [f"{start + 1}-{min(start + limit, page_count)}" for start in range(0, page_count, limit)]


def slice_params(args: argparse.Namespace, index: int, page_range: str) -> dict[str, str]:
    # Each slice writes its page images to its own folder, so image names can't collide across slices.
    subdir = f"slice_{index}" if args.page_images_path else ""
    return {"start_page": str(index * MAX_PAGES), "slice_options": parse_options(args, page_range, subdir)}


def pdf_page_count(content: bytes) -> int:
    """Page count of a PDF, or -1 when pypdf can't read it (ai_parse_document then reports the error)."""
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(content)).pages)
    except Exception:  # noqa: BLE001 - encrypted or malformed files surface as parse errors downstream
        return -1


def _shift(value, by: int):
    return value + by if isinstance(value, int) and not isinstance(value, bool) else value


def stitch(parts: list[tuple[int, dict | None]], expected_pages: int | None = None) -> dict:
    """Join ai_parse_document outputs into one, as if the whole document had been parsed in one call.

    ``parts`` holds (start_page, parsed) pairs: start_page is the 0-based page index where that part's
    first page belongs, and parsed is its output (None if the call failed). Page ids, element ids, and
    the page_ids in bounding boxes and error_status are renumbered. A part whose page ids already start
    at start_page is left as is, so this works whether a pageRange slice numbers its pages from 0 or
    from its first page. Metadata and any other top-level keys come from the first part that has them.
    """
    out: dict = {}
    pages, elements, errors = [], [], []
    next_element = 0
    for start, parsed in sorted(parts, key=lambda part: part[0]):
        if not parsed:
            errors.append({"page_id": start, "error_message": "no output for the pages starting here"})
            continue
        for key, value in parsed.items():
            if key not in ("document", "error_status"):
                out.setdefault(key, value)
        doc = parsed.get("document") or {}
        for key, value in doc.items():
            if key not in ("pages", "elements"):
                out.setdefault("document", {}).setdefault(key, value)
        part_pages = [dict(page) for page in doc.get("pages") or []]
        ids = [page["id"] for page in part_pages if isinstance(page.get("id"), int)]
        shift = start - min(ids) if ids and min(ids) < start else 0
        for page in part_pages:
            page["id"] = _shift(page.get("id"), shift)
        pages += part_pages

        part_elements = [dict(element) for element in doc.get("elements") or []]
        element_ids = [e["id"] for e in part_elements if isinstance(e.get("id"), int)]
        first_id = min(element_ids) if element_ids else 0
        for position, element in enumerate(part_elements):
            element["id"] = (element["id"] - first_id if isinstance(element.get("id"), int) else position) + next_element
            element["bbox"] = [
                {**box, "page_id": _shift(box.get("page_id"), shift)} if isinstance(box, dict) else box
                for box in element.get("bbox") or []
            ]
        if part_elements:
            next_element = max(e["id"] for e in part_elements) + 1
        elements += part_elements

        for error in parsed.get("error_status") or []:
            errors.append({**error, "page_id": _shift(error.get("page_id"), shift)} if isinstance(error, dict) else error)

    if expected_pages:
        present = {page.get("id") for page in pages}
        missing = [i for i in range(expected_pages) if i not in present]
        if missing:
            errors.append({
                "page_id": missing[0],
                "error_message": f"{len(missing)} of {expected_pages} pages missing after stitching",
            })
    out.setdefault("document", {}).update({"pages": pages, "elements": elements})
    out["error_status"] = errors
    return out


def printed_page_number(parsed: dict | None) -> int | None:
    """The page number printed on a single-page document (its first page_number element), if any."""
    for element in ((parsed or {}).get("document") or {}).get("elements") or []:
        if element.get("type") == "page_number":
            match = re.search(r"\d+", str(element.get("content") or ""))
            if match:
                return int(match.group(0))
    return None


def assemble(files: list[tuple[str, dict | None, str | None]]) -> tuple[dict, str]:
    """Stitch the page files of one document, given (file_name, parsed, parse_error) for each.

    Order comes from the last number in each file name when every name has a distinct one, else from
    the printed page numbers when every page has a distinct one, else from the file names. Returns the
    stitched document and the ordering used.
    """
    by_name = sorted(files, key=lambda f: f[0])
    numbers = [LAST_NUMBER_RE.search(Path(name).stem) for name, _, _ in by_name]
    printed = [printed_page_number(parsed) for _, parsed, _ in by_name]
    if all(numbers) and len({int(m.group(1)) for m in numbers}) == len(numbers):
        ordering = "file_name_number"
        ordered = [f for _, f in sorted(zip([int(m.group(1)) for m in numbers], by_name), key=lambda x: x[0])]
    elif all(n is not None for n in printed) and len(set(printed)) == len(printed):
        ordering = "printed_page_number"
        ordered = [f for _, f in sorted(zip(printed, by_name), key=lambda x: x[0])]
    else:
        ordering, ordered = "file_name", by_name

    parts, failed, start = [], [], 0
    for name, parsed, error in ordered:
        if parsed:
            parts.append((start, parsed))
            start += max(1, len((parsed.get("document") or {}).get("pages") or []))
        else:
            failed.append({"page_id": start, "error_message": f"{name}: {error or 'not parsed'}"})
            start += 1
    stitched = stitch(parts)
    stitched["error_status"] = sorted(stitched["error_status"] + failed, key=lambda e: e.get("page_id") or 0)
    return stitched, ordering


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
    params = build_params(args)
    sql_dir = Path(args.sql_dir) if args.sql_dir else Path(__file__).resolve().parent
    sections = load_sections(sql_dir / "parse.sql")

    spark = get_spark()
    from pyspark.sql import functions as F

    def stitch_udf(parts, page_count):
        loaded = [(p["start_page"], json.loads(p["parsed_json"]) if p["parsed_json"] else None) for p in parts]
        return json.dumps(stitch(loaded, page_count))

    def assemble_udf(pages):
        loaded = [(p["file_name"], json.loads(p["parsed_json"]) if p["parsed_json"] else None, p["parse_error"])
                  for p in pages]
        return json.dumps(assemble(loaded)[0])

    spark.udf.register("forge_stitch", stitch_udf, "string")
    spark.udf.register("forge_assemble", assemble_udf, "string")
    count_pages = F.udf(lambda content: pdf_page_count(content), "int")

    def run(name: str, **extra: str):
        return spark.sql(render(sections[name], {**params, **extra}))

    def set_batch_paths(paths: list[str]) -> None:
        spark.createDataFrame([(p,) for p in paths], "path STRING").createOrReplaceTempView("batch_paths")

    def batches(items: list[str]):
        for start in range(0, len(items), args.batch_size):
            yield items[start : start + args.batch_size]

    run("create_parsed_table")
    run("create_files_table")

    # Listing reads only file metadata: binaryFile skips `content` when it isn't selected.
    files = (
        spark.read.format("binaryFile")
        .option("recursiveFileLookup", "true")
        .load(args.volume_path)
        .select("path", "modificationTime", "length")
        .where(F.lower(F.regexp_extract("path", r"\.([^./]+)$", 1)).isin(*SUPPORTED_EXTENSIONS))
    )
    if args.page_images_path:  # rendered page images may live under the source volume; never parse them
        files = files.where(~F.col("path").contains(args.page_images_path.rstrip("/") + "/"))
    files.createOrReplaceTempView("volume_files")
    run("changed_files")

    changed = spark.table("changed_files").select("path", "file_size").collect()
    oversize = [r.path for r in changed if r.file_size > MAX_FILE_BYTES]
    to_parse = [r.path for r in changed if r.file_size <= MAX_FILE_BYTES]
    if oversize:
        set_batch_paths(oversize)
        run("delete_batch_files")
        run("insert_oversize")

    long_files = 0
    done = 0
    for batch in batches(to_parse):
        content = spark.read.format("binaryFile").load(batch)
        content.createOrReplaceTempView("batch_files")
        counts = (
            content.where(F.lower(F.col("path")).endswith(".pdf"))
            .select("path", count_pages("content").alias("page_count"))
            .collect()
        )
        spark.createDataFrame([(r.path, r.page_count) for r in counts], "path STRING, page_count INT") \
            .createOrReplaceTempView("batch_page_counts")
        set_batch_paths(batch)
        run("delete_batch_files")
        run("insert_batch_single")
        longest = max((r.page_count for r in counts if r.page_count > MAX_PAGES), default=0)
        if longest:
            long_files += sum(1 for r in counts if r.page_count > MAX_PAGES)
            slices = [run("select_slice", **slice_params(args, i, rng)) for i, rng in enumerate(page_ranges(longest))]
            reduce(lambda a, b: a.unionByName(b), slices).createOrReplaceTempView("long_slices")
            run("insert_batch_slices")
        done += len(batch)
        print(f"parsed {done}/{len(to_parse)} files")

    # An empty listing usually means a wrong path or missing grant; never treat it as "delete everything".
    listing_empty = files.limit(1).count() == 0
    if listing_empty:
        print(f"WARNING: no supported files under {args.volume_path}; skipping deletion of removed files")
    else:
        run("delete_removed_files")

    assembled = 0
    if args.split_pages == "folder":
        run("volume_documents")
        run("changed_documents")
        changed_documents = [r.path for r in spark.table("changed_documents").collect()]
        for batch in batches(changed_documents):
            set_batch_paths(batch)
            run("delete_batch_documents")
            run("insert_batch_documents")
        assembled = len(changed_documents)
        if not listing_empty:
            run("delete_removed_documents")

    summary = spark.sql(
        f"SELECT count(*) AS documents, count_if(parse_error IS NOT NULL) AS with_errors, "
        f"count_if(parsed IS NULL) AS skipped FROM {params['parsed_table']}"
    ).first()
    print(json.dumps({
        "changed_files": len(changed),
        "oversize_skipped": len(oversize),
        "parsed_in_slices": long_files,
        "documents_assembled": assembled,
        "documents": summary.documents,
        "documents_with_errors": summary.with_errors,
        "documents_skipped": summary.skipped,
    }))


if __name__ == "__main__":
    main()
