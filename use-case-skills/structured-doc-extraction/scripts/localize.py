"""Localize extracted values to word-level boxes with OCR (structured-doc-extraction module 06).

Third task of the extraction job, after extract.py. ai_extract v2.1 cites the whole parsed element a
value came from (a table, a paragraph). This task crops each cited element from its page image,
OCRs the crop with Tesseract, and string-matches the value against the OCR words to find the box
around the value itself. It writes <prefix>_localized, one row per (value, citation).

Tesseract is a system binary that serverless compute doesn't have, so this task runs on a small classic
job cluster whose init script installs it (see install_tesseract.sh and databricks.yml). It needs page
images, so the parse stage must run with --page-images-path.

The matching helpers are adapted from the advance-document-processing word-level citation pipeline.
"""

import argparse
import json
import os
import re
from html import unescape
from pathlib import Path

from rapidfuzz import fuzz

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SECTION_RE = re.compile(r"^--\s*name:\s*(\w+)\s*$")
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
NUMERIC_TOKEN_RE = re.compile(r"[$(+-]?\d[\d,]*(?:\.\d+)?\)?")
MATCH_THRESHOLD = {"numeric": 95.0, "text": 85.0}  # match_score needed to count a value as localized
MIN_HIT_RATE = 0.8  # module 06 gate: share of cited values localized
OCR_SCALE = 2  # upscale crops before OCR; small print reads better
OCR_PSM = 11  # Tesseract page segmentation: sparse text, which suits table cells
CROP_PAD = 4
LOCALIZED_SCHEMA = (
    "path STRING, segment_id INT, doc_type STRING, field STRING, value STRING, kind STRING, citation_id INT, "
    "page_id INT, element_bbox ARRAY<DOUBLE>, word_bbox ARRAY<DOUBLE>, matched_text STRING, match_score DOUBLE, "
    "localized BOOLEAN, error STRING, parsed_at TIMESTAMP"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--sql-dir", default="", help="Directory holding localize.sql (default: next to this file)")
    return p.parse_args()


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


def build_params(args: argparse.Namespace) -> dict[str, str]:
    for name in (args.catalog, args.schema, args.prefix):
        if not NAME_RE.match(name):
            raise ValueError(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731
    return {
        "parsed_table": table("parsed"),
        "extracted_table": table("extracted"),
        "fields_table": table("fields"),
        "progress_table": table("progress"),
        "localized_table": table("localized"),
    }


# --- geometry ------------------------------------------------------------------------------------------

def normalize_box(coord):
    if not coord or len(coord) < 4:
        return None
    x0, x1 = sorted((float(coord[0]), float(coord[2])))
    y0, y1 = sorted((float(coord[1]), float(coord[3])))
    return [x0, y0, x1, y1]


def first_coord(citation):
    """(box, page_id) of a citation's first bounding box, or (None, None)."""
    for bbox in citation.get("bbox") or []:
        box = normalize_box(bbox.get("coord"))
        if box:
            return box, bbox.get("page_id")
    return None, None


def box_area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) if box else 0.0


def overlap_score(a, b):
    """Max of IoU and coverage of the smaller box: 1.0 when one box contains the other."""
    if not a or not b:
        return 0.0
    inter = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    if inter == 0:
        return 0.0
    union = box_area(a) + box_area(b) - inter
    return max(inter / union if union else 0.0, inter / min(box_area(a), box_area(b)))


def union_box(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def to_page_coords(local_box, origin):
    return [local_box[0] + origin[0], local_box[1] + origin[1], local_box[2] + origin[0], local_box[3] + origin[1]]


# --- text ----------------------------------------------------------------------------------------------

def text_from_element(element):
    raw = (element or {}).get("content") or (element or {}).get("description") or ""
    text = re.sub(r"<[^>]+>", " ", unescape(str(raw)))  # table content is HTML
    return re.sub(r"\s+", " ", text).strip()


def element_for_citation(citation, elements):
    """The parsed element whose box best overlaps the citation's box on the same page, with its text."""
    box, page_id = first_coord(citation)
    if box is None or page_id is None:
        return None, "", 0.0
    best = (None, "", 0.0)
    for element in elements or []:
        for bbox in element.get("bbox") or []:
            if bbox.get("page_id") is None or int(bbox["page_id"]) != int(page_id):
                continue
            score = overlap_score(box, normalize_box(bbox.get("coord")))
            if score > best[2]:
                best = (element, text_from_element(element), score)
    return best


def value_kind(value: str) -> str:
    return "numeric" if NUMBER_RE.match(str(value).strip()) else "text"


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def norm_num(value):
    raw = str(value).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    digits = re.sub(r"[^0-9.]", "", raw)
    try:
        number = float(digits)
    except ValueError:
        return ""
    return f"{-number if negative else number:.2f}"


def dedupe_keep_order(values):
    seen, out = set(), []
    for value in values:
        key = str(value).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def match_targets(value, kind, element_text):
    """Strings to look for in the OCR words: how the value is printed in the element, then the value itself.

    ai_extract normalizes values (1884.0 for "$1,884.00"), so the printed form is found in the element
    text first: numbers by numeric equality, text by fuzzy windows of the element's words.
    """
    if kind == "numeric":
        target = norm_num(value)
        if not target:
            return []
        printed = [m.group(0) for m in NUMERIC_TOKEN_RE.finditer(element_text or "") if norm_num(m.group(0)) == target]
        return dedupe_keep_order([*printed, str(value)])

    target = norm(value)
    if not target:
        return []
    candidates = []
    direct = re.search(re.escape(str(value)), element_text or "", flags=re.IGNORECASE)
    if direct:
        candidates.append((100.0, element_text[direct.start() : direct.end()]))
    words = re.findall(r"\S+", element_text or "")
    value_words = re.findall(r"\S+", str(value))
    if words:
        min_window = max(1, len(value_words) - 2)
        max_window = min(len(words), max(3, min(20, len(value_words) + 4)))
        for i in range(len(words)):
            for width in range(min_window, max_window + 1):
                run = words[i : i + width]
                if len(run) == width and (score := fuzz.ratio(norm(" ".join(run)), target)) >= 75:
                    candidates.append((score, " ".join(run)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return dedupe_keep_order([text for _, text in candidates] + [str(value)])


def match_tokens(tokens, target, kind):
    """Best run of consecutive OCR tokens for target: (box, score 0-100, matched text)."""
    target_norm = norm_num(target) if kind == "numeric" else norm(target)
    if not tokens or not target_norm:
        return None, 0.0, ""
    best = (None, 0.0, "")
    max_window = min(len(tokens), max(6, min(20, len(re.findall(r"\S+", str(target))) + 4)))
    for i in range(len(tokens)):
        for width in range(1, max_window + 1):
            run = tokens[i : i + width]
            joined = "".join(token["text"] for token in run)
            if kind == "numeric" and re.search(r"[A-Za-z]", joined):
                continue  # norm_num drops letters, so "Gross Pay $1,884.00" would otherwise match 1884
            candidate = norm_num(joined) if kind == "numeric" else norm(joined)
            if not candidate:
                continue
            score = 100.0 if kind == "numeric" and candidate == target_norm else fuzz.ratio(candidate, target_norm)
            if score > best[1]:
                best = (union_box([token["box"] for token in run]), score, " ".join(token["text"] for token in run))
    return best


def match_best_target(tokens, targets, kind):
    best = (None, 0.0, "", "")
    for target in targets:
        box, score, matched = match_tokens(tokens, target, kind)
        if score > best[1]:
            best = (box, score, matched, target)
    return best


def locate(value, kind, element_text, tokens, origin):
    """(page box or None, score, matched text, localized) for one value in one OCR'd crop."""
    box, score, matched, _ = match_best_target(tokens, match_targets(value, kind, element_text), kind)
    page_box = to_page_coords(box, origin) if box else None
    return page_box, round(score, 1), matched, bool(page_box) and score >= MATCH_THRESHOLD[kind]


# --- OCR (runs on the job cluster) -----------------------------------------------------------------------

def crop_origin(box, size, pad=CROP_PAD):
    width, height = size
    x0, y0 = max(0, int(box[0]) - pad), max(0, int(box[1]) - pad)
    return (x0, y0, min(width, int(round(box[2])) + pad), min(height, int(round(box[3])) + pad))


def ocr_crop(image_uri, box):
    """OCR words in one box of a page image: (tokens with crop-local boxes, crop origin)."""
    import pytesseract
    from PIL import Image

    with Image.open(image_uri.removeprefix("dbfs:")) as page:
        page = page.convert("RGB")
        x0, y0, x1, y1 = crop_origin(box, page.size)
        crop = page.crop((x0, y0, x1, y1))
    scaled = crop.resize((crop.width * OCR_SCALE, crop.height * OCR_SCALE), Image.LANCZOS)
    data = pytesseract.image_to_data(scaled, config=f"--psm {OCR_PSM}", output_type=pytesseract.Output.DICT)
    tokens = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text or float(data["conf"][i]) < 0:
            continue
        x, y = data["left"][i] / OCR_SCALE, data["top"][i] / OCR_SCALE
        tokens.append({"text": text, "box": [x, y, x + data["width"][i] / OCR_SCALE, y + data["height"][i] / OCR_SCALE]})
    return tokens, (x0, y0)


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def main() -> None:
    args = parse_args()
    import pandas as pd
    import pytesseract

    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        raise SystemExit(
            "Tesseract isn't installed on this compute. The localize task must run on the job cluster from "
            "databricks.yml, whose init script installs it; check the init script logs (apt-get needs egress)."
        ) from None

    params = build_params(args)
    sql_dir = Path(args.sql_dir) if args.sql_dir else Path(__file__).resolve().parent
    sections = load_sections(sql_dir / "localize.sql")
    spark = get_spark()

    def run(name: str):
        return spark.sql(render(sections[name], params))

    def ocr_partitions(frames):
        for frame in frames:
            rows = []
            for row in frame.itertuples(index=False):
                try:
                    tokens, origin = ocr_crop(row.image_uri, json.loads(row.box_json))
                    rows.append((row.crop_key, json.dumps(tokens), float(origin[0]), float(origin[1]), None))
                except Exception as e:  # noqa: BLE001 - one unreadable image must not fail the batch
                    rows.append((row.crop_key, "[]", 0.0, 0.0, f"{type(e).__name__}: {e}"))
            yield pd.DataFrame(rows, columns=["crop_key", "tokens_json", "origin_x", "origin_y", "error"])

    run("create_localized_table")
    run("pending_documents")
    pending = [r.path for r in spark.table("pending_documents").collect()]
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        spark.createDataFrame([(p,) for p in batch], "path STRING").createOrReplaceTempView("batch_paths")
        documents = {r.path: r for r in run("batch_documents").collect()}
        entities, crops = [], {}
        for row in run("batch_values").collect():
            document = documents.get(row.path)
            if document is None:
                continue
            elements = json.loads(document.elements_json or "[]")
            images = {p.get("id"): p.get("image_uri") for p in json.loads(document.pages_json or "[]")}
            citations = {c.get("id"): c for c in json.loads(row.citations_json or "[]")}
            for citation_id in row.citation_ids:
                citation = citations.get(citation_id)
                box, page_id = first_coord(citation) if citation else (None, None)
                image_uri = images.get(page_id)
                _, element_text, _ = element_for_citation(citation, elements) if citation else (None, "", 0.0)
                crop_key = f"{image_uri}|{box}"
                if box and image_uri:
                    crops[crop_key] = (image_uri, json.dumps(box))
                entities.append({
                    "row": row, "citation_id": citation_id, "page_id": page_id, "box": box, "crop_key": crop_key,
                    "element_text": element_text, "has_image": bool(box and image_uri),
                })

        ocr = {}
        if crops:
            frame = pd.DataFrame([(k, uri, b) for k, (uri, b) in crops.items()], columns=["crop_key", "image_uri", "box_json"])
            schema = "crop_key STRING, tokens_json STRING, origin_x DOUBLE, origin_y DOUBLE, error STRING"
            results = spark.createDataFrame(frame).repartition(max(1, len(crops) // 20)).mapInPandas(ocr_partitions, schema)
            ocr = {r.crop_key: r for r in results.collect()}

        out = []
        for entity in entities:
            row = entity["row"]
            kind = value_kind(row.value)
            result = ocr.get(entity["crop_key"])
            word_box, score, matched, localized, error = None, 0.0, "", False, None
            if not entity["has_image"]:
                error = "citation has no bounding box or page image"
            elif result is None or result.error:
                error = f"OCR failed: {result.error if result else 'no result'}"
            elif not entity["element_text"]:
                error = "no parsed element under the citation"
            else:
                word_box, score, matched, localized = locate(
                    row.value, kind, entity["element_text"], json.loads(result.tokens_json), (result.origin_x, result.origin_y)
                )
            out.append((
                row.path, row.segment_id, row.doc_type, row.field, row.value, kind, int(entity["citation_id"]),
                int(entity["page_id"]) if isinstance(entity["page_id"], int) else None,
                entity["box"], word_box, matched, float(score), localized, error, row.parsed_at,
            ))

        run("delete_batch_localized")
        if out:
            spark.createDataFrame(out, LOCALIZED_SCHEMA).createOrReplaceTempView("batch_localized")
            run("insert_batch_localized")
        run("record_batch_progress")
        print(f"localized {start + len(batch)}/{len(pending)} documents ({len(out)} cited values)")

    run("delete_orphan_localized")
    run("delete_stale_progress")
    summary = spark.sql(
        f"SELECT count(*) AS cited_values, count_if(localized) AS localized FROM {params['localized_table']}"
    ).first()
    hit_rate = summary.localized / summary.cited_values if summary.cited_values else None
    print(json.dumps({
        "documents_localized": len(pending),
        "cited_values": summary.cited_values,
        "localized": summary.localized,
        "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
        "meets_gate": hit_rate is not None and hit_rate >= MIN_HIT_RATE,
    }))


if __name__ == "__main__":
    main()
