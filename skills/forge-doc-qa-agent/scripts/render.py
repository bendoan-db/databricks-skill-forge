# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Render the doc-qa agent's search tool from doc-qa.json and doc-index.json (forge-doc-qa-agent module 01).

    uv run <skill>/scripts/render.py --state <workdir>/doc-qa.json

Writes <workdir>/<name>/agent/retrieval.py, and needs `agentbricks init` to have run. The index, its
metadata columns, filter_values, and corpus_description come from the doc-index.json that
forge-unstructured-doc-extraction wrote (`index_state` in doc-qa.json); a corpus_description in doc-qa.json
takes precedence. The rendered file starts with a checksum header: if it was edited by hand since the
last render, nothing is written unless you pass --force. Prints a JSON summary.
"""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
APP_NAME_RE = re.compile(r"^[a-z0-9-]{1,17}$")  # agent-bricks-<name> must fit the 30-character app name limit
HEADER = "# Rendered by forge-doc-qa-agent render.py from doc-qa.json (sha256:{digest}). Change doc-qa.json and re-render."
HEADER_RE = re.compile(r"^# Rendered by forge-doc-qa-agent render\.py .*\(sha256:([0-9a-f]{12})\)")
FILTER_BLOCK_RE = re.compile(r"(?ms)^FILTER_VALUES: dict\[str, list\[str\] \| None\] = \{.*?^\}$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/doc-qa.json")
    p.add_argument("--force", action="store_true", help="Overwrite rendered files even if they were edited by hand")
    return p.parse_args()


def load_state(path: Path) -> dict:
    """doc-qa.json, with the index keys from its doc-index.json under "index_state"."""
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("name",) if not state.get(key)]
    if state.get("name") and not APP_NAME_RE.match(str(state["name"])):
        errors.append(f"name={state['name']!r}: use at most 17 characters from [a-z0-9-]")
    index_path = path.resolve().parent / state.get("index_state", "doc-index.json")
    if not index_path.is_file():
        raise SystemExit(f"{index_path} not found. Run forge-unstructured-doc-extraction first (module 00, step 1).")
    index = json.loads(index_path.read_text())
    for key in ("catalog", "schema", "index_name"):
        if not NAME_RE.match(str(index.get(key, ""))):
            errors.append(f"{index_path.name}: {key} is missing or invalid")
    if "02-chunk-index" not in index.get("completed", []):
        errors.append(f"{index_path.name}: the index isn't built yet (02-chunk-index isn't in completed)")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    state["index"] = index
    return state


def metadata_keys(spec) -> list[str]:
    """Column names of the index's metadata_schema (forge-unstructured-doc-extraction validated the types)."""
    if isinstance(spec, str):
        spec = json.loads(spec) if spec.strip() else None
    return list(spec or [])


def with_header(body: str) -> str:
    return HEADER.format(digest=hashlib.sha256(body.encode()).hexdigest()[:12]) + "\n" + body


def edited_by_hand(path: Path) -> bool:
    first, _, rest = path.read_text().partition("\n")
    match = HEADER_RE.match(first)
    return not match or hashlib.sha256(rest.encode()).hexdigest()[:12] != match.group(1)


def render_retrieval(state: dict) -> str:
    index = state["index"]
    try:
        columns = metadata_keys(index.get("metadata_schema"))
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        raise SystemExit(f"doc-index.json metadata_schema: {e}") from None
    filters = index.get("filter_values") or {}
    errors = []
    if unknown := sorted(set(filters) - set(columns)):
        errors.append(f"filter_values has columns {unknown} that aren't metadata_schema keys {sorted(columns)}")
    for column, values in filters.items():
        ok = values is None or (
            isinstance(values, list) and all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values)
        )
        if not ok:
            errors.append(f"filter_values[{column!r}] must be null or a list of strings or numbers")
    description = str(state.get("corpus_description") or index.get("corpus_description") or "").strip()
    if not description:
        errors.append("corpus_description is missing: forge-unstructured-doc-extraction module 02, step 5 records it")
    elif "<" in description:
        errors.append("corpus_description still contains a <placeholder>")
    num_results = state.get("num_results", 8)
    if not isinstance(num_results, int) or isinstance(num_results, bool) or not 1 <= num_results <= 50:
        errors.append(f"num_results={num_results!r}: use an integer from 1 to 50")
    if errors:
        raise SystemExit("; ".join(errors))

    expected = {
        "INDEX_NAME": f"{index['catalog']}.{index['schema']}.{index['index_name']}",
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
        raise SystemExit(f"Rendered retrieval.py constants don't match doc-qa.json and doc-index.json: {found}")
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    agent_dir = args.state.resolve().parent / state["name"] / "agent"
    if not agent_dir.is_dir():
        raise SystemExit(f"{agent_dir} not found. Run `agentbricks init` first (module 01, step 1).")
    rendered = {agent_dir / "retrieval.py": with_header(render_retrieval(state))}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into doc-qa.json and re-render, "
            "or pass --force to overwrite (the hand edit is lost)."
        )
    summary = {"written": [], "unchanged": []}
    for path, text in rendered.items():
        if path.exists() and path.read_text() == text:
            summary["unchanged"].append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        summary["written"].append(str(path))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
