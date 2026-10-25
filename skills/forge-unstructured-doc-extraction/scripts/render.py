# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the forge-unstructured-doc-extraction job from doc-index.json (forge-unstructured-doc-extraction modules 01-02).

    uv run <skill>/scripts/render.py --state <workdir>/doc-index.json

Writes <workdir>/<name>-index/databricks.yml with every value from doc-index.json, and copies the job's
scripts to its src/ (those copies always track the skill: parse.py and parse.sql are the shared Forge
parse stage). The rendered file starts with a checksum header: if it was edited by hand since the last
render, nothing is written unless you pass --force. Prints a JSON summary.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing prep_chunks must not leave __pycache__ in the published skill folder

import yaml  # noqa: E402

from prep_chunks import NAME_RE, metadata_columns  # noqa: E402  (same validation the chunk task applies)

SCRIPTS = Path(__file__).resolve().parent
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
INDEX_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,255}$")
VOLUME_RE = re.compile(r"^/Volumes/[^/]+/[^/]+/[^/]+")
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
HEADER = "# Rendered by forge-unstructured-doc-extraction render.py from doc-index.json (sha256:{digest}). Change doc-index.json and re-render."
HEADER_RE = re.compile(r"^# Rendered by forge-unstructured-doc-extraction render\.py .*\(sha256:([0-9a-f]{12})\)")
JOB_FILES = ["parse.py", "parse.sql", "prep_chunks.py", "prep_chunks.sql", "sync_index.py"]
CHART_ELEMENT_TYPES = ("*", "figure")  # chart insights read the figure descriptions ai_parse_document writes
TRIGGER_BLOCK_RE = re.compile(r"(?ms)^      trigger:.*?\n.*?wait_after_last_change_seconds: \d+\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/doc-index.json")
    p.add_argument("--force", action="store_true", help="Overwrite rendered files even if they were edited by hand")
    return p.parse_args()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    required = ["profile", "volume_path", "catalog", "schema", "prefix", "name", "vs_endpoint", "index_name",
                "embedding_model"]
    errors = [f"{key} is missing or empty" for key in required if not state.get(key)]
    if not isinstance(state.get("continuous_updates"), bool):
        errors.append("continuous_updates must be true or false: ask the user (module 00, step 1)")
    elif state.get("schedule_cron") and not state["continuous_updates"]:
        errors.append("schedule_cron needs continuous_updates: true (a schedule keeps the index updated)")
    for key in ("catalog", "schema", "prefix"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: use letters, digits, '_' or '-'")
    if state.get("name") and not BUNDLE_NAME_RE.match(str(state["name"])):
        errors.append(f"name={state['name']!r}: use at most 40 characters from [a-z0-9-]")
    if state.get("index_name") and not INDEX_NAME_RE.match(str(state["index_name"])):
        errors.append(f"index_name={state['index_name']!r}: use letters, digits, and '_' (the catalog and schema are separate)")
    if state.get("volume_path") and not VOLUME_RE.match(str(state["volume_path"])):
        errors.append(f"volume_path={state['volume_path']!r}: use /Volumes/<catalog>/<schema>/<volume>[/subdir]")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def metadata(state: dict) -> tuple[str, dict[str, str]]:
    """Return (schema JSON for ai_prep_search, {column: SQL type})."""
    spec = state.get("metadata_schema")
    raw = spec.strip() if isinstance(spec, str) else (json.dumps(spec) if spec else "")
    try:
        return raw, metadata_columns(raw)
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        raise SystemExit(f"metadata_schema: {e}") from None


def with_header(body: str) -> str:
    return HEADER.format(digest=hashlib.sha256(body.encode()).hexdigest()[:12]) + "\n" + body


def edited_by_hand(path: Path) -> bool:
    first, _, rest = path.read_text().partition("\n")
    match = HEADER_RE.match(first)
    return not match or hashlib.sha256(rest.encode()).hexdigest()[:12] != match.group(1)


def render_bundle(state: dict) -> str:
    raw_schema, columns = metadata(state)
    element_types = state.get("description_element_types", "*")
    page_images = state.get("page_images_path", "")
    charts = state.get("chart_insights") is True
    if charts and (not page_images or element_types not in CHART_ELEMENT_TYPES):
        raise SystemExit(
            "chart_insights needs page images (page_images_path) and figure descriptions "
            f"(description_element_types {' or '.join(map(repr, CHART_ELEMENT_TYPES))})"
        )
    values = {
        "bundle_name": f"{state['name']}-index",
        "catalog": state["catalog"],
        "schema": state["schema"],
        "prefix": state["prefix"],
        "index_name": state["index_name"],
        "volume_path": state["volume_path"].rstrip("/"),
        "metadata_schema": raw_schema,
        "metadata_columns": ",".join(columns),
        "description_element_types": element_types,
        "page_images_path": page_images,
        "split_pages": "folder" if state.get("split_pages") is True else "none",
        "chart_insights": "true" if charts else "false",
        "chart_model": state.get("chart_model") or "",  # empty: prep_chunks.py's default, Claude Sonnet 5.5
        "chart_prompt": state.get("chart_prompt") or "",
        "vs_endpoint": state["vs_endpoint"],
        "embedding_model": state["embedding_model"],
        "profile": state["profile"],
    }
    template = (SCRIPTS / "databricks.yml").read_text()
    body = template[template.index("bundle:") :]  # drop the template's instructions
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

    cron = state.get("schedule_cron")  # SKILL.md decision point: a schedule instead of file arrival
    if cron or not state["continuous_updates"]:
        schedule = ""  # no continuous updates: no trigger, so runs start only by hand
        if cron:
            schedule = (
                "      schedule:\n"
                f"        quartz_cron_expression: {json.dumps(cron)}\n"
                f"        timezone_id: {json.dumps(state.get('schedule_timezone', 'UTC'))}\n"
                "        pause_status: UNPAUSED\n"
            )
        body, count = TRIGGER_BLOCK_RE.subn(lambda _: schedule, body)
        if count != 1:
            raise SystemExit("databricks.yml template changed: can't find the file_arrival trigger block")

    parsed = yaml.safe_load(body)
    got = {key: spec.get("default") for key, spec in parsed["variables"].items()}
    got.update(bundle_name=parsed["bundle"]["name"], profile=parsed["targets"]["default"]["workspace"]["profile"])
    if mismatched := sorted(key for key in values if got.get(key) != values[key]):
        raise SystemExit(f"Rendered databricks.yml doesn't round-trip {mismatched}")
    job = parsed["resources"]["jobs"]["doc_index"]
    if cron and (job.get("schedule") or {}).get("quartz_cron_expression") != cron:
        raise SystemExit("Rendered databricks.yml doesn't carry schedule_cron")
    if not state["continuous_updates"] and ("trigger" in job or "schedule" in job):
        raise SystemExit("Rendered databricks.yml still has a trigger, but continuous_updates is false")
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    bundle = args.state.resolve().parent / f"{state['name']}-index"
    rendered = {bundle / "databricks.yml": with_header(render_bundle(state))}
    copies = {bundle / "src" / name: (SCRIPTS / name).read_text() for name in JOB_FILES}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into doc-index.json and re-render, "
            "or pass --force to overwrite (the hand edit is lost)."
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
