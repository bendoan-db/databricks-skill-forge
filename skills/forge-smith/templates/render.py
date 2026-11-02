# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render <skill-name>'s job bundle from <state-file> (<skill-name> module <module-id>).

    uv run <skill>/scripts/render.py --state <workdir>/<state-file>

Writes <workdir>/<bundle_name>/databricks.yml from scripts/databricks.yml, filling each whole-value <key> from the
state file, and copies the job's notebooks, and the files they read, to <bundle_name>/src/. The rendered
databricks.yml carries a checksum: if it was edited by hand since the last render, nothing is written unless you
pass --force. Each copy carries a checksum line too: a copy changed since it was written, such as a notebook edited
in the workspace and exported back, is kept and listed under kept_edited, and --force replaces it with the skill's
version. Prints a JSON summary.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

SKILL = "<skill-name>"
STATE_FILE = "<state-file>"
SCRIPTS = Path(__file__).resolve().parent
JOB_FILES = []  # smith: the notebooks the job runs, and the files they read (such as .sql), as named in scripts/
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
HEADER = "# Rendered by " + SKILL + " render.py from " + STATE_FILE + " (sha256:{digest}). Change it and re-render."
HEADER_RE = re.compile(r"^# Rendered by [\w-]+ render\.py .*\(sha256:([0-9a-f]{12})\)")
COPY_MARK = ("{comment} Copied from " + SKILL + " by render.py (sha256:{digest}). Edits are kept when you render "
             "again; render.py --force replaces them.")
COPY_MARK_RE = re.compile(r"^(?:#|--) Copied from [\w-]+ by render\.py \(sha256:([0-9a-f]{12})\)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help=f"<workdir>/{STATE_FILE}")
    p.add_argument("--force", action="store_true",
                   help="Overwrite a hand-edited databricks.yml, and replace edited copies with the skill's version")
    return p.parse_args()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("bundle_name", "profile") if not state.get(key)]
    # smith: check every other key the bundle needs, so every problem is reported at once
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def bundle_values(state: dict) -> dict[str, str]:
    """Each whole-value <key> in scripts/databricks.yml, and its value from the state file."""
    return {
        "bundle_name": state["bundle_name"],
        "profile": state["profile"],
        # smith: one entry per other <key> in scripts/databricks.yml
    }


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def with_header(body: str) -> str:
    return HEADER.format(digest=digest(body)) + "\n" + body


def edited_by_hand(path: Path) -> bool:
    first, _, rest = path.read_text().partition("\n")
    match = HEADER_RE.match(first)
    return not match or digest(rest) != match.group(1)


def mark_copy(name: str, text: str) -> str:
    """A job file as render.py copies it: with a line recording its checksum, after a notebook header or a shebang.
    In a notebook the line gets a cell of its own, so the first cell stays markdown."""
    lines = text.splitlines(keepends=True)
    notebook = bool(lines) and lines[0].startswith("# Databricks notebook source")
    at = 1 if lines and (notebook or lines[0].startswith("#!")) else 0
    if notebook:
        lines[1:1] = ["\n", "# COMMAND ----------\n", "\n"]
    mark = COPY_MARK.format(comment="--" if name.endswith(".sql") else "#", digest=digest("".join(lines))) + "\n"
    return "".join(lines[:at] + [mark] + lines[at:])


def copy_edited(path: Path) -> bool:
    """True when a copy changed since render.py wrote it. A copy without the line, from an older render.py, is replaced."""
    lines = path.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines[:2]):
        if match := COPY_MARK_RE.match(line):
            return digest("".join(lines[:i] + lines[i + 1:])) != match.group(1)
    return False


def render_bundle(values: dict[str, str]) -> str:
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
    yaml.safe_load(body)  # smith: also check that the rendered bundle round-trips the values that matter
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    values = bundle_values(state)
    bundle = args.state.resolve().parent / values["bundle_name"]
    rendered = {bundle / "databricks.yml": with_header(render_bundle(values))}
    copies = {bundle / "src" / name: mark_copy(name, (SCRIPTS / name).read_text()) for name in JOB_FILES}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into {STATE_FILE} and re-render, "
            "or pass --force to overwrite (the hand edit is lost)."
        )
    summary = {"written": [], "unchanged": [], "kept_edited": []}
    for path, text in {**rendered, **copies}.items():
        if path.exists() and path.read_text() == text:
            summary["unchanged"].append(str(path))
            continue
        if path in copies and path.exists() and copy_edited(path) and not args.force:
            summary["kept_edited"].append(str(path))  # an edit, maybe exported from the workspace: keep it
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        summary["written"].append(str(path))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
