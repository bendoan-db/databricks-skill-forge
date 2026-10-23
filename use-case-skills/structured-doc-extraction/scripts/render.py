# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the structured-doc-extraction job from extraction.json (structured-doc-extraction modules 01-04, 06).

    uv run <skill>/scripts/render.py --state <workdir>/extraction.json

Writes <workdir>/<name>-extract/databricks.yml and <name>-extract/src/extraction_config.json, and copies
the job's scripts to src/ (those copies always track the skill: parse.py and parse.sql are the shared
Forge parse stage). Validates the document types and schemas with the same rules the job applies.
Until document_types is recorded (modules 02-03) the job's trigger is rendered PAUSED. Rendered files
carry a checksum: if one was edited by hand since the last render, nothing is written unless you pass
--force. Prints a JSON summary.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing extract must not leave __pycache__ in the published skill folder

import yaml  # noqa: E402

from extract import LAYOUTS, NAME_RE, validate_config  # noqa: E402  (same validation the extract task applies)

SCRIPTS = Path(__file__).resolve().parent
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
VOLUME_RE = re.compile(r"^/Volumes/[^/]+/[^/]+/[^/]+")
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
BLOCK_RE = re.compile(r"(?ms)^[ \t]*# >>> (\w+)\n(.*?)^[ \t]*# <<< \1\n")
HEADER = "# Rendered by structured-doc-extraction render.py from extraction.json (sha256:{digest}). Change extraction.json and re-render."
HEADER_RE = re.compile(r"^# Rendered by structured-doc-extraction render\.py .*\(sha256:([0-9a-f]{12})\)")
CONFIG_MARK = "_rendered_by"
CONFIG_NOTE = "structured-doc-extraction render.py from extraction.json (sha256:{digest}). Change extraction.json and re-render."
JOB_FILES = ["parse.py", "parse.sql", "extract.py", "extract.sql"]
LOCALIZE_FILES = ["localize.py", "localize.sql", "install_tesseract.sh"]
ELEMENT_TYPES = ("*", "figure", "")  # ai_parse_document descriptionElementTypes values


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/extraction.json")
    p.add_argument("--force", action="store_true", help="Overwrite rendered files even if they were edited by hand")
    return p.parse_args()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("profile", "volume_path", "catalog", "schema", "prefix", "name")
              if not state.get(key)]
    for key in ("catalog", "schema", "prefix"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: use letters, digits, '_' or '-'")
    if state.get("name") and not BUNDLE_NAME_RE.match(str(state["name"])):
        errors.append(f"name={state['name']!r}: use at most 40 characters from [a-z0-9-]")
    if state.get("volume_path") and not VOLUME_RE.match(str(state["volume_path"])):
        errors.append(f"volume_path={state['volume_path']!r}: use /Volumes/<catalog>/<schema>/<volume>[/subdir]")
    if state.get("description_element_types", "*") not in ELEMENT_TYPES:
        errors.append("description_element_types must be '*', 'figure', or ''")
    if state.get("localize") is True:
        if not state.get("page_images_path"):
            errors.append("localize needs page images: set page_images_path (module 01, step 1)")
        cluster = state.get("ocr_cluster") or {}
        if not cluster.get("spark_version") or not cluster.get("node_type_id"):
            errors.append("localize needs ocr_cluster.spark_version and ocr_cluster.node_type_id (module 06, step 1)")
    if state.get("document_types"):
        errors += validate_config(state)
    elif state.get("layout") not in LAYOUTS:  # types come in modules 02-03; the layout is known from preflight
        errors.append(f"layout={state.get('layout')!r}: use one of {list(LAYOUTS)}")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def job_config(state: dict) -> dict:
    """The part of extraction.json the extract task reads, with defaults filled in."""
    return {
        "layout": state["layout"],
        "extract_input": state.get("extract_input", "variant"),
        "review_threshold": state.get("review_threshold", 0.8),
        "classify_instructions": state.get("classify_instructions", ""),
        "document_types": state.get("document_types") or {},
    }


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def with_header(body: str) -> str:
    return HEADER.format(digest=digest(body)) + "\n" + body


def config_json(config: dict) -> str:
    body = json.dumps(config, indent=2)
    return json.dumps({CONFIG_MARK: CONFIG_NOTE.format(digest=digest(body)), **config}, indent=2) + "\n"


def edited_by_hand(path: Path) -> bool:
    text = path.read_text()
    if path.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return True
        note = data.pop(CONFIG_MARK, "") if isinstance(data, dict) else ""
        match = re.search(r"\(sha256:([0-9a-f]{12})\)", str(note))
        return not match or digest(json.dumps(data, indent=2)) != match.group(1)
    first, _, rest = text.partition("\n")
    match = HEADER_RE.match(first)
    return not match or digest(rest) != match.group(1)


def render_bundle(state: dict) -> str:
    localize = state.get("localize") is True
    # Until modules 02-03 record document_types there's nothing to extract, so new files mustn't start runs.
    pause = "UNPAUSED" if state.get("document_types") else "PAUSED"
    values = {
        "bundle_name": f"{state['name']}-extract",
        "catalog": state["catalog"],
        "schema": state["schema"],
        "prefix": state["prefix"],
        "volume_path": state["volume_path"].rstrip("/"),
        "description_element_types": state.get("description_element_types", "*"),
        "page_images_path": state.get("page_images_path", ""),
        "split_pages": "folder" if state.get("split_pages") is True else "none",
        "profile": state["profile"],
    }
    if localize:
        values["ocr_spark_version"] = state["ocr_cluster"]["spark_version"]
        values["ocr_node_type"] = state["ocr_cluster"]["node_type_id"]

    template = (SCRIPTS / "databricks.yml").read_text()
    body = template[template.index("bundle:") :]  # drop the template's instructions
    cron = state.get("schedule_cron")  # decision point: a schedule instead of file arrival
    found = set()

    def block(match: re.Match) -> str:
        name, content = match.group(1), match.group(2)
        found.add(name)
        if name == "localize":
            return content if localize else ""
        if name == "trigger" and cron:
            return (
                "      schedule:\n"
                f"        quartz_cron_expression: {json.dumps(cron)}\n"
                f"        timezone_id: {json.dumps(state.get('schedule_timezone', 'UTC'))}\n"
                f"        pause_status: {pause}\n"
            )
        if name == "trigger":
            return content.replace("pause_status: UNPAUSED", f"pause_status: {pause}")
        return content

    body = BLOCK_RE.sub(block, body)
    if found != {"localize", "trigger"}:
        raise SystemExit(f"databricks.yml template changed: expected localize and trigger blocks, found {sorted(found)}")
    used = set()

    def fill(match: re.Match) -> str:
        key = match.group(2)
        if key not in values:
            raise SystemExit(f"databricks.yml template has <{key}>, which render.py doesn't know")
        used.add(key)
        return match.group(1) + json.dumps(values[key])  # a JSON string is a valid YAML scalar, so no quoting bugs

    body = WHOLE_SCALAR_RE.sub(fill, body)
    if unused := sorted(set(values) - used):
        raise SystemExit(f"databricks.yml template no longer has placeholders for {unused}")

    parsed = yaml.safe_load(body)
    got = {key: spec.get("default") for key, spec in parsed["variables"].items()}
    got.update(bundle_name=parsed["bundle"]["name"], profile=parsed["targets"]["default"]["workspace"]["profile"])
    job = parsed["resources"]["jobs"]["doc_extraction"]
    clusters = {c["job_cluster_key"]: c["new_cluster"] for c in job.get("job_clusters", [])}
    if localize:
        got.update(ocr_spark_version=clusters["ocr"]["spark_version"], ocr_node_type=clusters["ocr"]["node_type_id"])
    if mismatched := sorted(key for key in values if got.get(key) != values[key]):
        raise SystemExit(f"Rendered databricks.yml doesn't round-trip {mismatched}")
    tasks = [t["task_key"] for t in job["tasks"]]
    if tasks != ["parse", "extract"] + (["localize"] if localize else []):
        raise SystemExit(f"Rendered databricks.yml has tasks {tasks}")
    if cron and (job.get("schedule") or {}).get("quartz_cron_expression") != cron:
        raise SystemExit("Rendered databricks.yml doesn't carry schedule_cron")
    if (job.get("schedule") or job.get("trigger") or {}).get("pause_status") != pause:
        raise SystemExit(f"Rendered databricks.yml trigger isn't {pause}")
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    bundle = args.state.resolve().parent / f"{state['name']}-extract"
    rendered = {
        bundle / "databricks.yml": with_header(render_bundle(state)),
        bundle / "src" / "extraction_config.json": config_json(job_config(state)),
    }
    names = JOB_FILES + (LOCALIZE_FILES if state.get("localize") is True else [])
    copies = {bundle / "src" / name: (SCRIPTS / name).read_text() for name in names}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into extraction.json and "
            "re-render, or pass --force to overwrite (the hand edit is lost)."
        )
    summary = {"written": [], "unchanged": []}
    for path, text in {**rendered, **copies}.items():
        if path.exists() and path.read_text() == text:
            summary["unchanged"].append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        summary["written"].append(str(path))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
