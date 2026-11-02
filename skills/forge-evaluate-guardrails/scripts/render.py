# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render forge-evaluate-guardrails's job bundle from guardrail-eval.json (forge-evaluate-guardrails modules 01, 03).

    uv run <skill>/scripts/render.py --state <workdir>/guardrail-eval.json

Writes <workdir>/<name>/databricks.yml from scripts/databricks.yml, filling each whole-value <key> from the state
file, and <name>/src/eval_config.json, the settings the job's notebooks read (the dataset, the model service, the
policies it must have, and the run settings). It copies the job's notebooks to <name>/src/. Both rendered files carry
a checksum: if one was edited by hand since the last render, nothing is written unless you pass --force. Each copy
carries a checksum line too: a copy changed since it was written, such as a notebook edited in the workspace and
exported back (module 01), is kept and listed under kept_edited, and --force replaces it with the skill's version.
Validates the whole state file first and reports every problem at once. Prints a JSON summary.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

SKILL = "forge-evaluate-guardrails"
STATE_FILE = "guardrail-eval.json"
SCRIPTS = Path(__file__).resolve().parent
JOB_FILES = ["load_prompts.py", "run_eval.py"]  # the notebooks the job runs
WHOLE_SCALAR_RE = re.compile(r"(?m)^(\s*[\w-]+:[ \t]*)<([a-z_]+)>[ \t]*$")
HEADER = "# Rendered by " + SKILL + " render.py from " + STATE_FILE + " (sha256:{digest}). Change it and re-render."
HEADER_RE = re.compile(r"^# Rendered by [\w-]+ render\.py .*\(sha256:([0-9a-f]{12})\)")
COPY_MARK = ("{comment} Copied from " + SKILL + " by render.py (sha256:{digest}). Edits are kept when you render "
             "again; render.py --force replaces them.")
COPY_MARK_RE = re.compile(r"^(?:#|--) Copied from [\w-]+ by render\.py \(sha256:([0-9a-f]{12})\)")
CONFIG_MARK = "_rendered_by"
CONFIG_NOTE = SKILL + " render.py from " + STATE_FILE + " (sha256:{digest}). Change " + STATE_FILE + " and re-render."

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
PREFIX_RE = re.compile(r"^[a-z0-9_]+$")
DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
VOLUME_FILE_RE = re.compile(r"^/Volumes/[^/]+/[^/]+/[^/]+/.+")
# The built-in guardrails this skill evaluates, in rank order: state key -> service policy name.
POLICIES = {"sensitive_data": "sensitive_data_detection", "unsafe_content": "unsafe_content_detection",
            "jailbreak": "jailbreak_detection"}
LLM_GUARDRAILS = ("unsafe_content", "jailbreak")  # these run on an evaluator model service
DEFAULT_DATASETS = ("aegis2", "toxicchat")
FORMATS = ("csv", "json", "jsonl", "parquet")
TARGETS = {"recall", "precision", "f1", "accuracy", "fp_rate"}  # fp_rate is a ceiling, the others floors
NUMBERS = {  # setting: (default, minimum, maximum)
    "concurrency": (8, 1, 64),
    "max_tokens": (64, 1, 4096),
    "sample_size": (100, 1, 100_000),
    "settle_minutes": (5, 0, 120),
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help=f"<workdir>/{STATE_FILE}")
    p.add_argument("--force", action="store_true",
                   help="Overwrite hand-edited rendered files, and replace edited copies with the skill's version")
    return p.parse_args()


def three_part(value) -> bool:
    parts = str(value).split(".")
    return len(parts) == 3 and all(NAME_RE.match(part) for part in parts)


def dataset_errors(dataset) -> list[str]:
    """Problems with the dataset spec: the default datasets, or the user's table or volume file and its columns."""
    if not isinstance(dataset, dict):
        return ["dataset must be an object with a source: default, table, or file (module 00)"]
    source = dataset.get("source")
    if source == "default":
        chosen = dataset.get("defaults", list(DEFAULT_DATASETS))
        if not isinstance(chosen, list) or not chosen or set(chosen) - set(DEFAULT_DATASETS) or len(set(chosen)) != len(chosen):
            return [f"dataset.defaults={chosen!r}: a non-empty list from {list(DEFAULT_DATASETS)}"]
        return []
    if source not in ("table", "file"):
        return [f"dataset.source={source!r}: use default, table, or file"]
    errors = []
    if source == "table" and not three_part(dataset.get("table", "")):
        errors.append(f"dataset.table={dataset.get('table')!r}: use <catalog>.<schema>.<table>")
    if source == "file":
        if not VOLUME_FILE_RE.match(str(dataset.get("path", ""))):
            errors.append(f"dataset.path={dataset.get('path')!r}: a file in a volume, "
                          "/Volumes/<catalog>/<schema>/<volume>/<file>")
        if dataset.get("format") not in FORMATS:
            errors.append(f"dataset.format={dataset.get('format')!r}: use one of {list(FORMATS)}")
    for key in ("prompt_column", "label_column"):
        if not isinstance(dataset.get(key), str) or not dataset[key].strip():
            errors.append(f"dataset.{key} is missing: the column that holds the {key.split('_')[0]}")
    values = dataset.get("block_values")
    if not isinstance(values, list) or not values or not all(isinstance(v, (str, int, bool)) and str(v).strip() for v in values):
        errors.append("dataset.block_values must be a non-empty list: the label values that mean a prompt should be blocked")
    column = dataset.get("category_column")
    if "category_column" in dataset and (not isinstance(column, str) or not column.strip()):
        errors.append("dataset.category_column, when set, names a column")
    if "name" in dataset and not DATASET_NAME_RE.match(str(dataset["name"])):
        errors.append(f"dataset.name={dataset['name']!r}: up to 40 characters from [A-Za-z0-9_-]")
    return errors


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("profile", "catalog", "schema", "prefix", "name", "dataset",
                                                       "model_service", "guardrails", "experiment")
              if not state.get(key)]
    for key in ("catalog", "schema"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: use letters, digits, '_' or '-'")
    if state.get("prefix") and not PREFIX_RE.match(str(state["prefix"])):
        errors.append(f"prefix={state['prefix']!r}: use lowercase letters, digits, and '_'")
    if state.get("name") and not BUNDLE_NAME_RE.match(str(state["name"])):
        errors.append(f"name={state['name']!r}: use at most 40 characters from [a-z0-9-]")
    if state.get("model_service") and not three_part(state["model_service"]):
        errors.append(f"model_service={state['model_service']!r}: use <catalog>.<schema>.<service>")
    if state.get("base_model_service") and not three_part(state["base_model_service"]):
        errors.append(f"base_model_service={state['base_model_service']!r}: use <catalog>.<schema>.<service>, such as "
                      "system.ai.claude-sonnet-5-5")
    if state.get("experiment") and not str(state["experiment"]).startswith("/"):
        errors.append(f"experiment={state['experiment']!r}: a workspace path, such as /Users/<user>/guardrail-eval")
    if state.get("dataset"):
        errors += dataset_errors(state["dataset"])
    guardrails = state.get("guardrails")
    if guardrails:
        if not isinstance(guardrails, list) or set(guardrails) - set(POLICIES) or len(set(guardrails)) != len(guardrails):
            errors.append(f"guardrails={guardrails!r}: a list from {list(POLICIES)}, each once")
        else:
            evaluators = state.get("evaluators") or {}
            for g in guardrails:
                if g in LLM_GUARDRAILS and not three_part(evaluators.get(g, "")):
                    errors.append(f"evaluators.{g} is missing: the evaluator model service it runs on, such as "
                                  "system.ai.gpt-oss-20b")
    for key, (_, low, high) in NUMBERS.items():
        value = state.get(key, NUMBERS[key][0])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            errors.append(f"{key}={value!r}: a whole number from {low} to {high}")
    targets = state.get("targets") or {}
    if not isinstance(targets, dict):
        errors.append("targets must be an object, such as {\"recall\": 0.9, \"fp_rate\": 0.1}")
    else:
        for key, value in targets.items():
            if key not in TARGETS:
                errors.append(f"targets.{key}: use {sorted(TARGETS)}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"targets.{key}={value!r}: a number from 0 to 1")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def bundle_values(state: dict) -> dict[str, str]:
    """Each whole-value <key> in scripts/databricks.yml, and its value from the state file."""
    return {
        "bundle_name": state["name"],
        "profile": state["profile"],
        "catalog": state["catalog"],
        "schema": state["schema"],
        "prefix": state["prefix"],
    }


def job_config(state: dict) -> dict:
    """eval_config.json: what the job's notebooks read, with defaults filled in."""
    return {
        "dataset": state["dataset"],
        "model_service": state["model_service"],
        "policies": [name for key, name in POLICIES.items() if key in state["guardrails"]],
        **{key: state.get(key, default) for key, (default, _, _) in NUMBERS.items()},
        "experiment": state["experiment"],
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

    parsed = yaml.safe_load(body)
    got = {key: spec.get("default") for key, spec in parsed["variables"].items()}
    got.update(bundle_name=parsed["bundle"]["name"], profile=parsed["targets"]["default"]["workspace"]["profile"])
    if mismatched := sorted(key for key in values if got.get(key) != values[key]):
        raise SystemExit(f"Rendered databricks.yml doesn't round-trip {mismatched}")
    jobs = parsed["resources"]["jobs"]
    if list(jobs) != ["guardrail_eval"]:
        raise SystemExit(f"Rendered databricks.yml has jobs {list(jobs)}")
    job = jobs["guardrail_eval"]
    if [p["name"] for p in job.get("parameters") or []] != ["mode", "run_id"]:
        raise SystemExit("Rendered databricks.yml's job parameters aren't mode and run_id")
    tasks = {t["task_key"]: t for t in job["tasks"]}
    if list(tasks) != ["load_prompts", "run_eval"]:
        raise SystemExit(f"Rendered databricks.yml has tasks {list(tasks)}")
    params = {"catalog": "${var.catalog}", "schema": "${var.schema}", "prefix": "${var.prefix}",
              "config": "${workspace.file_path}/src/eval_config.json"}
    for key, task in tasks.items():
        notebook = task.get("notebook_task") or {}
        if notebook.get("notebook_path") != f"src/{key}.py" or notebook.get("base_parameters") != params:
            raise SystemExit(f"Rendered databricks.yml's {key} task isn't a notebook task on src/{key}.py with {params}")
    if tasks["run_eval"].get("depends_on") != [{"task_key": "load_prompts"}]:
        raise SystemExit("Rendered databricks.yml's run_eval task must depend on load_prompts")
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    values = bundle_values(state)
    bundle = args.state.resolve().parent / values["bundle_name"]
    rendered = {
        bundle / "databricks.yml": with_header(render_bundle(values)),
        bundle / "src" / "eval_config.json": config_json(job_config(state)),
    }
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
