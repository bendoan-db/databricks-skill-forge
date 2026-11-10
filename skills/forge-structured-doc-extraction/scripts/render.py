# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the forge-structured-doc-extraction job from extraction.json (forge-structured-doc-extraction modules 01-04, 06, 07).

    uv run <skill>/scripts/render.py --state <workdir>/extraction.json

Writes <workdir>/<name>-extract/databricks.yml and <name>-extract/src/extraction_config.json, and copies
the job's notebooks, SQL, and init script to src/ (parse.py and parse.sql are the shared Forge parse
stage). Validates the document types and schemas with the same rules the job applies. Until
document_types is recorded (modules 02-03) the job's trigger is rendered PAUSED. Rendered files carry a
checksum: if one was edited by hand since the last render, nothing is written unless you pass --force.
Copied files carry a checksum line too, but an edited copy (for example a notebook changed in the
workspace and exported back into src/) is kept and listed under kept_edited; --force replaces it with
the skill's version. Prints a JSON summary.

With review_app on (module 07) and the app scaffolded in <workdir>/<review.app_name>/, it also copies the
review app's server and client files there and sets the app's table names in its app.yaml.
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
SKILL = "forge-structured-doc-extraction"
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
VOLUME_RE = re.compile(r"^/Volumes/[^/]+/[^/]+/[^/]+")
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
BLOCK_RE = re.compile(r"(?ms)^[ \t]*# >>> (\w+)\n(.*?)^[ \t]*# <<< \1\n")
HEADER = "# Rendered by forge-structured-doc-extraction render.py from extraction.json (sha256:{digest}). Change extraction.json and re-render."
HEADER_RE = re.compile(r"^# Rendered by forge-structured-doc-extraction render\.py .*\(sha256:([0-9a-f]{12})\)")
CONFIG_MARK = "_rendered_by"
CONFIG_NOTE = ("forge-structured-doc-extraction render.py from extraction.json (sha256:{digest}). "
               "Change extraction.json and re-render.")
JOB_FILES = ["parse.py", "parse.sql", "extract.py", "extract.sql"]
LOCALIZE_FILES = ["localize.py", "localize.sql", "install_tesseract.sh"]
ELEMENT_TYPES = ("*", "figure", "")  # ai_parse_document descriptionElementTypes values
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,25}$")  # Apps allow 30 characters; AppKit's dev workflow adds "dev-"
PG_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")  # Lakebase names allow [A-Za-z0-9_]; leave room for the suffixes
PIPELINE_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
APP_FILES = {  # review app file in scripts/review-app -> where it goes in the scaffolded AppKit project
    "server.ts": "server/server.ts",
    "review-routes.ts": "server/review-routes.ts",
    "App.tsx": "client/src/App.tsx",
}
APP_HEADER = ("// Copied by forge-structured-doc-extraction render.py (sha256:{digest}). Change the skill's "
              "scripts/review-app copy and re-render.")
APP_HEADER_RE = re.compile(r"^// Copied by forge-structured-doc-extraction render\.py \(sha256:([0-9a-f]{12})\)")
COPY_MARK = ("{comment} Copied from " + SKILL + " by render.py (sha256:{digest}). Edits are kept when you render "
             "again; render.py --force replaces them.")
COPY_MARK_RE = re.compile(r"^(?:#|--) Copied from [\w-]+ by render\.py \(sha256:([0-9a-f]{12})\)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/extraction.json")
    p.add_argument("--force", action="store_true",
                   help="Overwrite rendered files edited by hand, and replace edited copies of the job's files")
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
    if not isinstance(state.get("review_app"), bool):  # module 00 asks; there's no default
        errors.append("review_app must be true or false: ask module 00's review app question and record the answer")
    elif state["review_app"]:
        errors += review_errors(state)
    if state.get("document_types"):
        errors += validate_config(state)
    elif state.get("layout") not in LAYOUTS:  # types come in modules 02-03; the layout is known from preflight
        errors.append(f"layout={state.get('layout')!r}: use one of {list(LAYOUTS)}")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def review_errors(state: dict) -> list[str]:
    """Problems with the review app's settings (module 07)."""
    errors = [f"review_app needs {key} of lowercase letters, digits, and '_' (at most 50): the review queue's "
              f"Postgres names come from it, got {state.get(key)!r}"
              for key in ("schema", "prefix") if state.get(key) and not PG_NAME_RE.match(str(state[key]))]
    review = state.get("review") or {}
    if not isinstance(review, dict):
        return errors + ["review must be an object (module 07)"]
    if "app_name" in review and not APP_NAME_RE.match(str(review["app_name"])):
        errors.append(f"review.app_name={review['app_name']!r}: use 2 to 26 characters from [a-z0-9-], starting with a letter")
    if "sync_pipeline_id" in review and not PIPELINE_ID_RE.match(str(review["sync_pipeline_id"])):
        errors.append(f"review.sync_pipeline_id={review['sync_pipeline_id']!r} isn't a pipeline id")
    history_schema = str(review.get("history_schema", "x.x"))
    if len(history_schema.split(".")) != 2 or not all(NAME_RE.match(part) for part in history_schema.split(".")):
        errors.append(f"review.history_schema={history_schema!r}: use <catalog>.<schema>")
    if review.get("history_ready") is True and not review.get("sync_pipeline_id"):
        errors.append("review.history_ready needs review.sync_pipeline_id: the app reads the queue (module 07, step 3)")
    return errors


def review_names(state: dict) -> dict[str, str]:
    """The review app's resources, all named from catalog, schema, and prefix (module 07)."""
    review = state.get("review") or {}
    prefix = state["prefix"]
    history_schema = review.get("history_schema") or f"{state['catalog']}.{state['schema']}"
    return {
        "candidates_table": f"{state['catalog']}.{state['schema']}.{prefix}_review_candidates",
        "synced_table": f"{state['catalog']}.{state['schema']}.{prefix}_review_queue",
        "queue_postgres": f"{state['schema']}.{prefix}_review_queue",  # a synced table's Postgres schema is its UC schema
        "decisions_postgres": f"{prefix}_review.{prefix}_decisions",   # created and owned by the app
        "app_postgres_schema": f"{prefix}_review",
        "history_table": f"{history_schema}.lb_{prefix}_decisions_history",  # named by Lakehouse Sync
        "decisions_table": f"{state['catalog']}.{state['schema']}.{prefix}_decisions",
    }


def job_config(state: dict) -> dict:
    """The part of extraction.json the extract task reads, with defaults filled in."""
    return {
        "layout": state["layout"],
        "extract_input": state.get("extract_input", "variant"),
        "review_threshold": state.get("review_threshold", 0.8),
        "review_cutoffs": state.get("review_cutoffs") or {},
        "parse_cutoffs": state.get("parse_cutoffs") or {},
        "review_app": state.get("review_app") is True,
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
    if path.suffix in (".ts", ".tsx"):  # a review app file: the scaffold's own (no header) is replaced
        first, _, rest = text.partition("\n")
        match = APP_HEADER_RE.match(first)
        return bool(match) and digest(rest) != match.group(1)
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


def render_bundle(state: dict) -> str:
    localize = state.get("localize") is True
    review = (state.get("review") or {}) if state.get("review_app") is True else {}
    queue = bool(review.get("sync_pipeline_id"))
    review_sync = review.get("history_ready") is True
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
    if queue:
        values["sync_pipeline_id"] = review["sync_pipeline_id"]
    if review_sync:
        values["history_table"] = review_names(state)["history_table"]

    template = (SCRIPTS / "databricks.yml").read_text()
    body = template[template.index("bundle:") :]  # drop the template's instructions
    cron = state.get("schedule_cron")  # decision point: a schedule instead of file arrival
    found = set()

    def block(match: re.Match) -> str:
        name, content = match.group(1), match.group(2)
        found.add(name)
        if name == "localize":
            return content if localize else ""
        if name == "review_queue":
            return content if queue else ""
        if name == "review_sync":
            return content if review_sync else ""
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
    if found != {"localize", "trigger", "review_queue", "review_sync"}:
        raise SystemExit("databricks.yml template changed: expected localize, trigger, review_queue, and review_sync "
                         f"blocks, found {sorted(found)}")
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
    tasks = {t["task_key"]: t for t in job["tasks"]}
    if queue:
        got["sync_pipeline_id"] = tasks["review_queue"]["pipeline_task"]["pipeline_id"]
    if mismatched := sorted(key for key in values if got.get(key) != values[key]):
        raise SystemExit(f"Rendered databricks.yml doesn't round-trip {mismatched}")
    if list(tasks) != ["parse", "extract"] + (["localize"] if localize else []) + (["review_queue"] if queue else []):
        raise SystemExit(f"Rendered databricks.yml has tasks {list(tasks)}")
    notebook_tasks = [t for jobs in parsed["resources"]["jobs"].values() for t in jobs["tasks"] if "pipeline_task" not in t]
    for task in notebook_tasks:  # every job task runs one of the notebooks render.py copies
        path = (task.get("notebook_task") or {}).get("notebook_path", "")
        if not path.startswith("src/") or path[4:] not in JOB_FILES + LOCALIZE_FILES or not path.endswith(".py"):
            raise SystemExit(f"Rendered databricks.yml task {task['task_key']} isn't a notebook task on a copied notebook")
    if sorted(parsed["resources"]["jobs"]) != sorted(["doc_extraction"] + (["review_sync"] if review_sync else [])):
        raise SystemExit(f"Rendered databricks.yml has jobs {sorted(parsed['resources']['jobs'])}")
    if cron and (job.get("schedule") or {}).get("quartz_cron_expression") != cron:
        raise SystemExit("Rendered databricks.yml doesn't carry schedule_cron")
    if (job.get("schedule") or job.get("trigger") or {}).get("pause_status") != pause:
        raise SystemExit(f"Rendered databricks.yml trigger isn't {pause}")
    return body


def app_yaml(path: Path, env: dict[str, str]) -> str:
    """The scaffolded app.yaml with the review app's settings in its env list; other entries are kept."""
    data = yaml.safe_load(path.read_text()) or {}
    entries = [entry for entry in data.get("env") or [] if entry.get("name") not in env]
    data["env"] = entries + [{"name": name, "value": value} for name, value in env.items()]
    return yaml.safe_dump(data, sort_keys=False)


def render_app(state: dict, workdir: Path) -> tuple[dict[Path, str], str]:
    """The review app's files for the AppKit project scaffolded in module 07, and a note for the summary."""
    app_name = (state.get("review") or {}).get("app_name")
    folder = workdir / str(app_name)
    if not app_name or not (folder / "package.json").is_file() or not (folder / "app.yaml").is_file():
        return {}, f"not scaffolded yet: {folder} (module 07, step 4)"
    names = review_names(state)
    files = {folder / target: with_app_header((SCRIPTS / "review-app" / source).read_text())
             for source, target in APP_FILES.items()}
    files[folder / "app.yaml"] = app_yaml(folder / "app.yaml", {
        "REVIEW_QUEUE_TABLE": names["queue_postgres"],
        "REVIEW_DECISIONS_TABLE": names["decisions_postgres"],
    })
    return files, str(folder)


def with_app_header(body: str) -> str:
    return APP_HEADER.format(digest=digest(body)) + "\n" + body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    workdir = args.state.resolve().parent
    bundle = workdir / f"{state['name']}-extract"
    rendered = {
        bundle / "databricks.yml": with_header(render_bundle(state)),
        bundle / "src" / "extraction_config.json": config_json(job_config(state)),
    }
    names = JOB_FILES + (LOCALIZE_FILES if state.get("localize") is True else [])
    job_copies = {bundle / "src" / name: mark_copy(name, (SCRIPTS / name).read_text()) for name in names}
    copies = {}
    review = {}
    if state.get("review_app") is True:
        app_files, app = render_app(state, workdir)
        rendered.update({path: text for path, text in app_files.items() if path.suffix in (".ts", ".tsx")})
        copies.update({path: text for path, text in app_files.items() if path.suffix not in (".ts", ".tsx")})
        review = {"app": app, **review_names(state)}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into extraction.json and "
            "re-render, or pass --force to overwrite (the hand edit is lost)."
        )
    summary = {"written": [], "unchanged": [], "kept_edited": []} | ({"review": review} if review else {})
    for path, text in list(job_copies.items()):
        if path.exists() and path.read_text() != text and copy_edited(path) and not args.force:
            summary["kept_edited"].append(str(path))  # a change exported from the workspace, say; --force replaces it
            del job_copies[path]
    for path, text in {**rendered, **job_copies, **copies}.items():
        if path.exists() and path.read_text() == text:
            summary["unchanged"].append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        summary["written"].append(str(path))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
