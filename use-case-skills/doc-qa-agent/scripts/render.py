# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the doc-qa-agent templates from doc-qa.json (doc-qa-agent modules 01-03).

    uv run <skill>/scripts/render.py ingest --state <workdir>/doc-qa.json   # <name>-ingest/ bundle
    uv run <skill>/scripts/render.py agent --state <workdir>/doc-qa.json    # <name>/agent/retrieval.py

`ingest` writes <workdir>/<name>-ingest/databricks.yml and copies the job's scripts to its src/
(those copies always track the skill: parse.py and parse.sql are the shared Forge parse stage). `agent` writes <workdir>/<name>/agent/retrieval.py and needs
`agentbricks init` to have run. Rendered files start with a checksum header: if one was edited by hand
since the last render, nothing is written unless you pass --force. Prints a JSON summary.
"""

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing prep_chunks must not leave __pycache__ in the published skill folder

import yaml  # noqa: E402

from prep_chunks import NAME_RE, metadata_columns  # noqa: E402  (same validation the chunk task applies)

SCRIPTS = Path(__file__).resolve().parent
APP_NAME_RE = re.compile(r"^[a-z0-9-]{1,17}$")  # agent-bricks-<name> must fit the 30-character app name limit
VOLUME_RE = re.compile(r"^/Volumes/[^/]+/[^/]+/[^/]+")
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
HEADER = "# Rendered by doc-qa-agent render.py from doc-qa.json (sha256:{digest}). Change doc-qa.json and re-render."
HEADER_RE = re.compile(r"^# Rendered by doc-qa-agent render\.py .*\(sha256:([0-9a-f]{12})\)")
JOB_FILES = ["parse.py", "parse.sql", "prep_chunks.py", "prep_chunks.sql", "sync_index.py"]
CHART_ELEMENT_TYPES = ("*", "figure")  # chart insights read the figure descriptions ai_parse_document writes
FILTER_BLOCK_RE = re.compile(r"(?ms)^FILTER_VALUES: dict\[str, list\[str\] \| None\] = \{.*?^\}$")
TRIGGER_BLOCK_RE = re.compile(r"(?ms)^      trigger:\n.*?wait_after_last_change_seconds: \d+\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("target", choices=["ingest", "agent"])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/doc-qa.json")
    p.add_argument("--force", action="store_true", help="Overwrite rendered files even if they were edited by hand")
    return p.parse_args()


def load_state(path: Path, required: list[str]) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in required if not state.get(key)]
    for key in ("catalog", "schema", "prefix"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: use letters, digits, '_' or '-'")
    if state.get("name") and not APP_NAME_RE.match(str(state["name"])):
        errors.append(f"name={state['name']!r}: use at most 17 characters from [a-z0-9-]")
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
        "bundle_name": f"{state['name']}-ingest",
        "catalog": state["catalog"],
        "schema": state["schema"],
        "prefix": state["prefix"],
        "volume_path": state["volume_path"].rstrip("/"),
        "metadata_schema": raw_schema,
        "metadata_columns": ",".join(columns),
        "description_element_types": element_types,
        "page_images_path": page_images,
        "split_pages": "folder" if state.get("split_pages") is True else "none",
        "chart_insights": "true" if charts else "false",
        "chart_model": state.get("chart_model") or state.get("chat_model") or "",
        "chart_prompt": state.get("chart_prompt", ""),
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
    job = parsed["resources"]["jobs"]["doc_qa_ingest"]
    if cron and (job.get("schedule") or {}).get("quartz_cron_expression") != cron:
        raise SystemExit("Rendered databricks.yml doesn't carry schedule_cron")
    return body


def render_retrieval(state: dict) -> str:
    _, columns = metadata(state)
    filters = state.get("filter_values") or {}
    errors = []
    if unknown := sorted(set(filters) - set(columns)):
        errors.append(f"filter_values has columns {unknown} that aren't metadata_schema keys {sorted(columns)}")
    for column, values in filters.items():
        ok = values is None or (
            isinstance(values, list) and all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values)
        )
        if not ok:
            errors.append(f"filter_values[{column!r}] must be null or a list of strings or numbers")
    description = str(state.get("corpus_description", "")).strip()
    if "<" in description:
        errors.append("corpus_description still contains a <placeholder>")
    num_results = state.get("num_results", 8)
    if not isinstance(num_results, int) or isinstance(num_results, bool) or not 1 <= num_results <= 50:
        errors.append(f"num_results={num_results!r}: use an integer from 1 to 50")
    if errors:
        raise SystemExit("; ".join(errors))

    expected = {
        "INDEX_NAME": f"{state['catalog']}.{state['schema']}.{state['prefix']}_chunks_index",
        "NUM_RESULTS": num_results,
        "FILTER_VALUES": filters,
        "CORPUS_DESCRIPTION": description,
    }
    entries = "".join(f"    {json.dumps(k)}: {'None' if v is None else json.dumps(v)},\n" for k, v in filters.items())
    replacements = [
        (re.compile(r"(?m)^INDEX_NAME = .*$"), f"INDEX_NAME = {json.dumps(expected['INDEX_NAME'])}"),
        (re.compile(r"(?m)^NUM_RESULTS = .*$"), f"NUM_RESULTS = {num_results}"),
        (FILTER_BLOCK_RE, "FILTER_VALUES: dict[str, list[str] | None] = {" + (f"\n{entries}}}" if entries else "}")),
        (re.compile(r"(?m)^CORPUS_DESCRIPTION = .*$"), f"CORPUS_DESCRIPTION = {json.dumps(description)}"),
    ]
    body = (SCRIPTS / "retrieval.py").read_text()
    for pattern, replacement in replacements:
        body, count = pattern.subn(lambda _, text=replacement: text, body)  # a function, so backslashes stay literal
        if count != 1:
            raise SystemExit(f"retrieval.py template changed: expected one match for {pattern.pattern!r}")

    found = {}
    for node in ast.parse(body).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in expected:
                found[target.id] = ast.literal_eval(node.value)
    if found != expected:
        raise SystemExit(f"Rendered retrieval.py constants don't match doc-qa.json: {found}")
    return body


def main() -> None:
    args = parse_args()
    base = ["profile", "volume_path", "catalog", "schema", "prefix", "name"]
    workdir = args.state.resolve().parent
    rendered: dict[Path, str] = {}
    copies: dict[Path, str] = {}
    if args.target == "ingest":
        state = load_state(args.state, base + ["vs_endpoint", "embedding_model"])
        bundle = workdir / f"{state['name']}-ingest"
        rendered[bundle / "databricks.yml"] = with_header(render_bundle(state))
        copies = {bundle / "src" / name: (SCRIPTS / name).read_text() for name in JOB_FILES}
    else:
        state = load_state(args.state, base + ["corpus_description"])
        agent_dir = workdir / state["name"] / "agent"
        if not agent_dir.is_dir():
            raise SystemExit(f"{agent_dir} not found. Run `agentbricks init` first (module 03, step 1).")
        rendered[agent_dir / "retrieval.py"] = with_header(render_retrieval(state))

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into doc-qa.json and re-render, "
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
