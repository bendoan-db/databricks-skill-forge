"""Classify, extract, and publish structured fields from parsed documents (structured-doc-extraction modules 02-04).

Second task of the extraction job, after parse.py. Runs as a serverless ``spark_python_task``, or
locally through Databricks Connect:

    uv run --with "databricks-connect>=17" python extract.py --catalog main --schema loans \
        --prefix loan_files --config extraction_config.json

The SQL lives in extract.sql; the document types, schemas, and layout come from extraction_config.json,
which render.py writes from extraction.json. Each document is processed once per parse:
<prefix>_progress records the (path, parsed_at) this stage has handled, so ai_classify and ai_extract
never rerun on an unchanged document. Per batch it:

1. Splits documents into segments: whole documents (single_type, one_per_file) or runs of classified
   pages (packet). one_per_file and packet classify with ai_classify v2.1.
2. Runs ai_extract v2.1 with citations and confidence scores, once per segment, with its type's schema.
3. Writes one row per extracted value to <prefix>_fields, and typed tables per document type:
   <prefix>_<type>, plus <prefix>_<type>__<field> for each array-of-objects field.
"""

import argparse
import json
import os
import re
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")  # snake_case without '__', which joins table names
FIELD_RE = TYPE_RE
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SECTION_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$")

LAYOUTS = ("single_type", "one_per_file", "packet")
INPUTS = ("variant", "text")
OTHER = "other"
OTHER_DESCRIPTION = (
    "None of the other types: a cover or fax sheet, a blank or separator page, a signature-only page, "
    "boilerplate with no document-specific values, or a document type that isn't listed."
)
SCALAR_TYPES = {"string": "STRING", "enum": "STRING", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN"}
# Table suffixes the skill owns; a document type can't reuse one.
RESERVED_TYPES = {"parsed", "page_files", "pages", "segments", "extracted", "fields", "progress", "review", "localized", OTHER}
# Keys of ai_extract's per-value wrapper; a schema field can't reuse one, so wrappers stay unambiguous.
WRAPPER_KEYS = {"value", "confidence_score", "citation_ids"}
BASE_COLUMNS = {"path", "file_name", "segment_id", "page_numbers", "item_index", "parsed_at", "published_at"}
MAX_FIELDS = 256  # ai_extract schema limit
CHROME = "('page_header', 'page_footer', 'page_number')"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: reads <prefix>_parsed")
    p.add_argument("--config", required=True, type=Path, help="extraction_config.json rendered by render.py")
    p.add_argument("--full-refresh", choices=["true", "false"], default="false",
                   help="Classify and extract every parsed document again (re-bills the AI functions)")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--sql-dir", default="", help="Directory holding extract.sql (default: next to this file)")
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


# --- schema → table plan -----------------------------------------------------------------------------

def schema_errors(schema, where: str) -> list[str]:
    """Problems with one ai_extract schema, in the subset this skill can turn into typed tables."""
    if not isinstance(schema, dict) or not schema:
        return [f"{where}: schema must be a non-empty JSON object of fields"]
    errors, leaves = [], 0

    def check(fields: dict, path: str, in_array: bool) -> None:
        nonlocal leaves
        for name, spec in fields.items():
            here = f"{path}.{name}" if path else name
            if not FIELD_RE.match(name) or name in WRAPPER_KEYS:
                errors.append(f"{where}: field {here!r} must be snake_case without '__', and not "
                              f"{sorted(WRAPPER_KEYS)}")
            if not isinstance(spec, dict):
                errors.append(f"{where}: field {here!r} must be an object like {{\"type\": \"string\"}}")
                continue
            kind = spec.get("type")
            if kind in SCALAR_TYPES:
                leaves += 1
                if kind == "enum" and not (isinstance(spec.get("labels"), list) and spec["labels"]):
                    errors.append(f"{where}: enum field {here!r} needs a non-empty labels list")
            elif kind == "object":
                if not isinstance(spec.get("properties"), dict) or not spec["properties"]:
                    errors.append(f"{where}: object field {here!r} needs properties")
                else:
                    check(spec["properties"], here, in_array)
            elif kind == "array":
                items = spec.get("items")
                if in_array:
                    errors.append(f"{where}: {here!r} is an array inside an array item; flatten the schema")
                elif not isinstance(items, dict) or items.get("type") not in (*SCALAR_TYPES, "object"):
                    errors.append(f"{where}: array field {here!r} needs items of a scalar type or an object")
                elif items["type"] == "object":
                    if not isinstance(items.get("properties"), dict) or not items["properties"]:
                        errors.append(f"{where}: array field {here!r} needs item properties")
                    else:
                        check(items["properties"], here, True)
                else:
                    leaves += 1
            else:
                errors.append(f"{where}: field {here!r} has type {kind!r}; use one of "
                              f"{sorted([*SCALAR_TYPES, 'object', 'array'])}")

    check(schema, "", False)
    if leaves > MAX_FIELDS:
        errors.append(f"{where}: {leaves} fields; ai_extract accepts at most {MAX_FIELDS}")
    return errors


def flatten(fields: dict, path: tuple[str, ...] = ()) -> tuple[list, list, list]:
    """Plan the typed tables for a (valid) schema.

    Returns (scalars, scalar_arrays, child_tables): scalars are (column, path, sql_type); scalar_arrays are
    (column, path, sql_type) for arrays of scalars; child_tables are (suffix, path, scalars) for arrays of
    objects. Paths are tuples of field names; nested objects flatten to columns joined with '__'.
    """
    scalars, arrays, children = [], [], []
    for name, spec in fields.items():
        here = (*path, name)
        kind = spec["type"]
        if kind in SCALAR_TYPES:
            scalars.append(("__".join(here), here, SCALAR_TYPES[kind]))
        elif kind == "object":
            sub_scalars, sub_arrays, sub_children = flatten(spec["properties"], here)
            scalars += sub_scalars
            arrays += sub_arrays
            children += sub_children
        elif spec["items"]["type"] == "object":
            item_scalars, _, _ = flatten(spec["items"]["properties"])
            children.append(("__".join(here), here, item_scalars))
        else:
            arrays.append(("__".join(here), here, SCALAR_TYPES[spec["items"]["type"]]))
    return scalars, arrays, children


def column_errors(doc_type: str, schema: dict) -> list[str]:
    scalars, arrays, children = flatten(schema)
    names = [c for c, _, _ in scalars] + [c for c, _, _ in arrays]
    errors = [f"{doc_type}: column {c!r} clashes with a column the skill adds" for c in names if c in BASE_COLUMNS]
    taken = set(names)
    errors += [f"{doc_type}: column {c}__confidence clashes with field {c}__confidence"
               for c, _, _ in scalars if f"{c}__confidence" in taken]
    for suffix, _, item_scalars in children:
        errors += [f"{doc_type}.{suffix}: column {c!r} clashes with a column the skill adds"
                   for c, _, _ in item_scalars if c in BASE_COLUMNS]
    return errors


def validate_config(config: dict) -> list[str]:
    """Every problem with extraction_config.json, so render.py can report them all at once."""
    errors = []
    layout = config.get("layout")
    if layout not in LAYOUTS:
        errors.append(f"layout={layout!r}: use one of {list(LAYOUTS)}")
    if config.get("extract_input", "variant") not in INPUTS:
        errors.append(f"extract_input={config.get('extract_input')!r}: use one of {list(INPUTS)}")
    threshold = config.get("review_threshold", 0.8)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
        errors.append(f"review_threshold={threshold!r}: use a number in (0, 1]")
    types = config.get("document_types")
    if not isinstance(types, dict) or not types:
        return errors + ["document_types must be a non-empty object of <type>: {description, schema}"]
    if layout == "single_type" and len(types) != 1:
        errors.append(f"layout single_type takes exactly one document type, got {sorted(types)}")
    for doc_type, spec in types.items():
        if not TYPE_RE.match(doc_type) or len(doc_type) > 40 or doc_type in RESERVED_TYPES:
            errors.append(f"document type {doc_type!r}: use snake_case up to 40 characters without '__', "
                          f"and not {sorted(RESERVED_TYPES)}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{doc_type}: must be an object with description and schema")
            continue
        if layout != "single_type" and not str(spec.get("description", "")).strip():
            errors.append(f"{doc_type}: description is required; ai_classify uses it to tell the types apart")
        if len(str(spec.get("description", ""))) > 1000:
            errors.append(f"{doc_type}: description is over ai_classify's 1,000-character limit")
        if spec.get("pages", "single") not in ("single", "multi"):
            errors.append(f"{doc_type}: pages={spec.get('pages')!r}: use single or multi")
        if spec.get("mode") not in (None, "precision"):
            errors.append(f"{doc_type}: mode={spec.get('mode')!r}: omit it, or use precision")
        found = schema_errors(spec.get("schema"), doc_type)
        errors += found or column_errors(doc_type, spec["schema"])
    return errors


# --- SQL fragments -----------------------------------------------------------------------------------

def json_path(path: tuple[str, ...], root: str = "$.response") -> str:
    return root + "".join(f".{name}" for name in path)


def unwrap(expr: str) -> str:
    """The value of an ai_extract wrapper ({value, citation_ids, confidence_score}), or expr if unwrapped."""
    return f"CASE WHEN schema_of_variant({expr}) LIKE 'OBJECT%' THEN try_variant_get({expr}, '$.value', 'VARIANT') ELSE {expr} END"


def elements_expr(scoped: bool) -> str:
    elements = "try_cast(parsed:document.elements AS ARRAY<VARIANT>)"
    return f"filter({elements}, e -> array_contains(page_ids, e:bbox[0].page_id::INT))" if scoped else elements


def input_expr(kind: str, scoped: bool) -> str:
    """An AI-function input over columns `parsed` and `page_ids`: the parsed VARIANT or its text,
    for the whole document or (scoped) only the pages in page_ids."""
    if kind == "text":
        return (
            f"array_join(transform(filter({elements_expr(scoped)}, e -> NOT (e:type::STRING IN {CHROME})), "
            "e -> CASE WHEN e:type::STRING = 'figure' AND e:description::STRING IS NOT NULL "
            "THEN concat('[figure] ', e:description::STRING) ELSE e:content::STRING END), '\\n')"
        )
    if not scoped:
        return "parsed"
    # Same shape as ai_parse_document output, restricted to the segment's pages (ids keep their values).
    return (
        "parse_json(to_json(named_struct("
        "'document', named_struct("
        "'pages', filter(try_cast(parsed:document.pages AS ARRAY<VARIANT>), pg -> array_contains(page_ids, pg:id::INT)), "
        f"'elements', {elements_expr(True)}), "
        "'metadata', variant_get(parsed, '$.metadata', 'VARIANT'), "
        "'error_status', filter(try_cast(parsed:error_status AS ARRAY<VARIANT>), x -> array_contains(page_ids, x:page_id::INT))"
        ")))"
    )


def scalar_columns(scalars: list, source: str, root: str) -> tuple[str, str]:
    """(DDL, SELECT) fragments for scalar columns plus their confidence columns, names backquoted."""
    ddl = "".join(f"\n  `{c}` {t},\n  `{c}__confidence` DOUBLE," for c, _, t in scalars)
    select = "".join(
        f"\n  try_variant_get({source}, '{json_path(p, root)}.value', '{t}') AS `{c}`,"
        f"\n  try_variant_get({source}, '{json_path(p, root)}.confidence_score', 'DOUBLE') AS `{c}__confidence`,"
        for c, p, t in scalars
    )
    return ddl, select


def array_columns(arrays: list) -> tuple[str, str]:
    """(DDL, SELECT) fragments for arrays of scalars; the array and each item may or may not be wrapped."""
    ddl = "".join(f"\n  `{c}` ARRAY<{t}>," for c, _, t in arrays)
    select = ""
    for column, path, sql_type in arrays:
        array = unwrap(f"try_variant_get(extracted, '{json_path(path)}', 'VARIANT')")
        item = unwrap("v")
        select += f"\n  transform(try_cast({array} AS ARRAY<VARIANT>), v -> try_variant_get({item}, '$', '{sql_type}')) AS `{column}`,"
    return ddl, select


def type_tables(prefix_table, doc_type: str, schema: dict) -> list[dict[str, str]]:
    """Placeholder values for the typed tables of one document type: the gold table first, then children."""
    scalars, arrays, children = flatten(schema)
    ddl, select = scalar_columns(scalars, "extracted", "$.response")
    array_ddl, array_select = array_columns(arrays)
    tables = [{
        "kind": "gold",
        "gold_table": prefix_table(doc_type),
        "gold_column_ddl": ddl + array_ddl,
        "gold_column_select": select + array_select,
        "gold_comment": sql_string(f"Extracted {doc_type} documents, one row per document (structured-doc-extraction)"),
    }]
    for suffix, path, item_scalars in children:
        item_ddl, item_select = scalar_columns(item_scalars, "item", "$")
        tables.append({
            "kind": "child",
            "child_table": prefix_table(f"{doc_type}__{suffix}"),
            "child_column_ddl": item_ddl,
            "child_column_select": item_select,
            "child_comment": sql_string(f"Items of {doc_type}.{'.'.join(path)}, one row per item (structured-doc-extraction)"),
            "array_expr": unwrap(f"try_variant_get(x.extracted, {sql_string(json_path(path))}, 'VARIANT')"),
        })
    return tables


def expected_columns(table: dict[str, str]) -> list[str]:
    ddl = table.get("gold_column_ddl", table.get("child_column_ddl", ""))
    return [line.strip().split()[0].strip("`") for line in ddl.strip().splitlines() if line.strip()]


def classify_labels(types: dict) -> str:
    labels = {name: spec["description"] for name, spec in types.items()}
    labels[OTHER] = OTHER_DESCRIPTION
    return sql_string(json.dumps(labels))


def classify_options(config: dict) -> str:
    options = ["'version'", "'2.1'", "'enableConfidenceScores'", "'true'"]
    if str(config.get("classify_instructions", "")).strip():
        options += ["'instructions'", sql_string(config["classify_instructions"].strip())]
    return f"map({', '.join(options)})"


def extract_options(spec: dict) -> str:
    options = ["'version'", "'2.1'", "'enableCitations'", "'true'", "'enableConfidenceScores'", "'true'"]
    if str(spec.get("instructions", "")).strip():
        options += ["'instructions'", sql_string(spec["instructions"].strip())]
    if spec.get("mode"):
        options += ["'mode'", sql_string(spec["mode"])]
    return f"map({', '.join(options)})"


def build_params(args: argparse.Namespace, config: dict) -> dict[str, str]:
    for name in (args.catalog, args.schema, args.prefix):
        if not NAME_RE.match(name):
            raise ValueError(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731
    types = config["document_types"]
    kind = config.get("extract_input", "variant")
    multi = [sql_string(t) for t, spec in types.items() if spec.get("pages") == "multi"]
    return {
        "parsed_table": table("parsed"),
        "pages_table": table("pages"),
        "segments_table": table("segments"),
        "extracted_table": table("extracted"),
        "fields_table": table("fields"),
        "progress_table": table("progress"),
        "review_view": table("review"),
        "classify_labels": classify_labels(types),
        "classify_options": classify_options(config),
        "single_type_literal": sql_string(next(iter(types))),
        "multi_page_types": ", ".join(multi) or "NULL",
        "review_threshold": repr(float(config.get("review_threshold", 0.8))),
        "document_input": input_expr(kind, scoped=False),
        "page_input": input_expr(kind, scoped=True),
        "extract_input": input_expr(kind, scoped=config["layout"] == "packet"),
    }


# --- extracted values ----------------------------------------------------------------------------------

def is_wrapper(node) -> bool:
    return isinstance(node, dict) and "value" in node and set(node) <= WRAPPER_KEYS


def leaf_fields(response) -> list[tuple[str, str | None, float | None, list[int] | None]]:
    """(field, value as text, confidence, citation_ids) for every scalar value in an ai_extract response."""
    out = []

    def text(value):
        return None if value is None else value if isinstance(value, str) else json.dumps(value)

    def walk(node, path: str) -> None:
        if is_wrapper(node):
            value = node.get("value")
            if isinstance(value, (dict, list)):
                walk(value, path)
                return
            confidence = node.get("confidence_score")
            citations = node.get("citation_ids")
            out.append((
                path,
                text(value),
                float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
                [int(c) for c in citations if isinstance(c, int)] if isinstance(citations, list) else None,
            ))
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")
        elif path:
            out.append((path, text(node), None, None))

    walk(response or {}, "")
    return out


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def main() -> None:
    args = parse_args()
    config = json.loads(args.config.read_text())
    if errors := validate_config(config):
        raise SystemExit("extraction_config.json: " + "; ".join(errors))
    params = build_params(args, config)
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731
    types = config["document_types"]
    layout = config["layout"]
    plans = {doc_type: type_tables(table, doc_type, spec["schema"]) for doc_type, spec in types.items()}
    sql_dir = Path(args.sql_dir) if args.sql_dir else Path(__file__).resolve().parent
    sections = load_sections(sql_dir / "extract.sql")

    spark = get_spark()
    spark.udf.register(
        "forge_fields",
        lambda response_json: leaf_fields(json.loads(response_json) if response_json else {}),
        "array<struct<field:string,value:string,confidence:double,citation_ids:array<int>>>",
    )

    def run(name: str, **extra: str):
        return spark.sql(render(sections[name], {**params, **extra}))

    def set_batch_paths(paths: list[str]) -> None:
        spark.createDataFrame([(p,) for p in paths], "path STRING").createOrReplaceTempView("batch_paths")

    for name in ("create_progress_table", "create_segments_table", "create_extracted_table", "create_fields_table"):
        run(name)
    if layout == "packet":
        run("create_pages_table")
    mismatched = []
    for tables in plans.values():
        for t in tables:
            target = t.get("gold_table") or t["child_table"]
            run("create_gold_table" if t["kind"] == "gold" else "create_child_table", **t)
            have = set(spark.table(target).columns)
            if missing := [c for c in expected_columns(t) if c not in have]:
                mismatched.append(f"{target} lacks {missing}")
    if mismatched:
        raise SystemExit(
            "A document type's schema changed: " + "; ".join(mismatched) + ". With the user's confirmation, drop "
            "those tables and run the job with full_refresh=true (re-extracts every document)."
        )

    own_tables = [params["segments_table"], params["extracted_table"], params["fields_table"]]
    if layout == "packet":
        own_tables.append(params["pages_table"])
    typed_tables = [t.get("gold_table") or t["child_table"] for tables in plans.values() for t in tables]

    if args.full_refresh == "true":
        run("reset_progress")

    run("pending_documents")
    pending = [r.path for r in spark.table("pending_documents").collect()]
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        set_batch_paths(batch)
        for target in own_tables + typed_tables:
            run("delete_batch_rows", target_table=target)
        if layout == "single_type":
            run("insert_segments_whole")
        elif layout == "one_per_file":
            run("insert_segments_classified")
        else:
            run("insert_batch_pages")
            run("insert_segments_from_pages")
        for doc_type, spec in types.items():
            run(
                "insert_batch_extracted",
                doc_type_literal=sql_string(doc_type),
                extract_schema=sql_string(json.dumps(spec["schema"])),
                extract_options=extract_options(spec),
            )
        run("insert_batch_fields")
        for doc_type, tables in plans.items():
            for t in tables:
                run("insert_batch_gold" if t["kind"] == "gold" else "insert_batch_child",
                    doc_type_literal=sql_string(doc_type), **t)
        run("record_batch_progress")
        print(f"extracted {start + len(batch)}/{len(pending)} documents")

    for target in own_tables + typed_tables:
        run("delete_orphan_rows", target_table=target)
    run("delete_stale_progress")
    run("create_review_view")

    counts = spark.sql(
        f"SELECT doc_type, count(*) AS segments FROM {params['segments_table']} GROUP BY doc_type"
    ).collect()
    review = spark.sql(f"SELECT reason, count(*) AS n FROM {params['review_view']} GROUP BY reason").collect()
    print(json.dumps({
        "documents_processed": len(pending),
        "segments_by_type": {r.doc_type: r.segments for r in counts},
        "review_items": {r.reason: r.n for r in review},
    }))


if __name__ == "__main__":
    main()
