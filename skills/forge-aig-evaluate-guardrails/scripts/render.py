# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the guardrail evaluation's job bundle from guardrail-eval.json (forge-aig-evaluate-guardrails modules 01, 03).

    uv run <skill>/scripts/render.py --state <workdir>/guardrail-eval.json

Writes <workdir>/<job_name>/databricks.yml from scripts/databricks.yml, filling each whole-value <key> from the state
file, and <job_name>/src/eval_config.json, the settings the job's notebooks read (the dataset, the model services and
the policies each must have, and the run settings). It copies the job's notebooks to <job_name>/src/. Both rendered
files carry a checksum: if one was edited by hand since the last render, nothing is written unless you pass --force.
Each copy carries a checksum line too: a copy changed since it was written, such as a notebook edited in the workspace
and exported back (module 01), is kept and listed under kept_edited, and --force replaces it with the skill's version.

The model services are forge-aig-create-model-service's: it creates them, and its aig-model-services.json and the
model-services.yaml and service-policies.yaml in its config_dir describe them. This reads those files at render time,
and never writes them, so the evaluation can't drift from what was applied. Each service in `model_services` must be in
model-services.yaml, named <table_prefix>_..., not granted in permissions.yaml, and carry only policies that block,
since a policy that only logs or redacts leaves no verdict in the reply. Validates everything first and reports every
problem at once. Prints a JSON summary, with warnings about services that keep an inference table or rate limits.

`runtime` is where both skills run, recorded once in module 00, and it must be the same in both state files. Locally,
the bundle's target uses the CLI profile; in Genie Code in a notebook (runtime workspace) it has none, since job.py
deploys the job as the notebook's user, and a deploy from the workspace uses the workspace it's in.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

SKILL = "forge-aig-evaluate-guardrails"
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
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PROFILE_BLOCK_RE = re.compile(r"(?ms)^[ \t]*# >>> profile\b[^\n]*\n(.*?)^[ \t]*# <<< profile[^\n]*\n")
RUNTIMES = ("local", "workspace")
SERVICES_STATE = "aig-model-services.json"  # forge-aig-create-model-service's state file, in <workdir>
APPLIED = "03-apply"  # its module that creates the services
MAX_SERVICES = 10  # each service gets every prompt, so the cost of a run grows with each one
DEFAULT_DATASETS = ("aegis2", "toxicchat")
FORMATS = ("csv", "json", "jsonl", "parquet")
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
        if (not isinstance(chosen, list) or not chosen or set(chosen) - set(DEFAULT_DATASETS)
                or len(set(chosen)) != len(chosen)):
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
    if (not isinstance(values, list) or not values
            or not all(isinstance(v, (str, int, bool)) and str(v).strip() for v in values)):
        errors.append("dataset.block_values must be a non-empty list: the label values that mean a prompt should be "
                      "blocked")
    column = dataset.get("category_column")
    if "category_column" in dataset and (not isinstance(column, str) or not column.strip()):
        errors.append("dataset.category_column, when set, names a column")
    if "name" in dataset and not DATASET_NAME_RE.match(str(dataset["name"])):
        errors.append(f"dataset.name={dataset['name']!r}: up to 40 characters from [A-Za-z0-9_-]")
    return errors


def runtime_errors(state: dict) -> list[str]:
    """Problems with where the skill runs: `local`, with a CLI profile, or `workspace`, Genie Code in a notebook."""
    runtime = state.get("runtime", "local")
    if runtime not in RUNTIMES:
        return [f"runtime={runtime!r}: local or workspace (module 00)"]
    if runtime == "local" and not state.get("profile"):
        return ["profile is missing or empty: locally, every command uses the CLI profile"]
    return []


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = runtime_errors(state) + [
        f"{key} is missing or empty" for key in ("catalog", "schema", "table_prefix", "job_name", "dataset",
                                                 "model_services", "experiment") if not state.get(key)]
    for key in ("catalog", "schema"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: use letters, digits, '_' or '-'")
    if state.get("table_prefix") and not PREFIX_RE.match(str(state["table_prefix"])):
        errors.append(f"table_prefix={state['table_prefix']!r}: use lowercase letters, digits, and '_'")
    if state.get("job_name") and not BUNDLE_NAME_RE.match(str(state["job_name"])):
        errors.append(f"job_name={state['job_name']!r}: use at most 40 characters from [a-z0-9-]")
    services = state.get("model_services")
    if services:
        if not isinstance(services, list) or len(set(map(str, services))) != len(services):
            errors.append(f"model_services={services!r}: a list of three-part names, each once")
        elif len(services) > MAX_SERVICES:
            errors.append(f"model_services lists {len(services)}: at most {MAX_SERVICES}, since each gets every prompt")
        else:
            for name in services:
                if not three_part(name):
                    errors.append(f"model_services: {name!r}: use <catalog>.<schema>.<service>")
                elif (state.get("table_prefix")
                      and not str(name).rsplit(".", 1)[1].startswith(f"{state['table_prefix']}_")):
                    errors.append(f"model_services: {name} isn't named {state['table_prefix']}_...: the evaluation "
                                  "measures only services of its own, so evaluate a copy named that way (module 00, "
                                  "step 1)")
    if "services_state" in state and not FILE_NAME_RE.match(str(state["services_state"])):
        errors.append(f"services_state={state['services_state']!r}: a file name in the working directory")
    if state.get("experiment") and not str(state["experiment"]).startswith("/"):
        errors.append(f"experiment={state['experiment']!r}: a workspace path, such as /Users/<user>/guardrail-eval")
    if state.get("dataset"):
        errors += dataset_errors(state["dataset"])
    for key, (_, low, high) in NUMBERS.items():
        value = state.get(key, NUMBERS[key][0])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            errors.append(f"{key}={value!r}: a whole number from {low} to {high}")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def bundle_values(state: dict) -> dict[str, str]:
    """Each whole-value <key> in scripts/databricks.yml, and its value from the state file. The profile only locally:
    in a notebook the target has none."""
    values = {
        "bundle_name": state["job_name"],
        "catalog": state["catalog"],
        "schema": state["schema"],
        "table_prefix": state["table_prefix"],
    }
    if state.get("runtime", "local") == "local":
        values["profile"] = state["profile"]
    return values


def load_yaml(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, f"{path} not found"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        return None, f"{path} isn't valid YAML: {e}"
    return (data, None) if isinstance(data, dict) else (None, f"{path}: the top level must be a mapping")


def granted(permissions: dict) -> set[str]:
    """Every model service permissions.yaml grants to someone."""
    return {str(name) for section in ("groups", "users") for entry in permissions.get(section) or []
            if isinstance(entry, dict) for name in entry.get("model_services") or []}


def evaluated_services(state: dict, workdir: Path) -> tuple[list[dict], list[str], list[str]]:
    """The services to evaluate, each {name, policies} with the policies in the order listed, from
    forge-aig-create-model-service's files in <workdir>; and the errors and warnings found. Reads only."""
    path = workdir / str(state.get("services_state", SERVICES_STATE))
    if not path.is_file():
        return [], [f"{path} not found: run forge-aig-create-model-service in this folder first "
                    "(module 00, step 7)"], []
    other = json.loads(path.read_text())
    errors, warnings = [], []
    if other.get("runtime", "local") != state.get("runtime", "local"):
        errors.append(f"{path.name} has runtime {other.get('runtime', 'local')!r} and {STATE_FILE} "
                      f"{state.get('runtime', 'local')!r}: both skills run in the same place, so record the same "
                      "runtime in both (module 00)")
    parts = other.get("parts") or []
    if not {"model_services", "service_policies"} <= set(parts):
        errors.append(f"{path.name}: parts={parts!r} needs model_services and service_policies, so the services get "
                      "their policies")
    if APPLIED not in (other.get("completed") or []):
        errors.append(f"{path.name}: forge-aig-create-model-service hasn't applied the services ({APPLIED} isn't in "
                      "its completed): resume it")
    folder = workdir / str(other.get("config_dir") or "")
    cfg, problem = load_yaml(folder / "model-services.yaml")
    policies_cfg, policy_problem = load_yaml(folder / "service-policies.yaml")
    errors += [p for p in (problem, policy_problem) if p]
    if errors:
        return [], errors, warnings
    defined = {p.get("name"): p for p in policies_cfg.get("service_policies") or [] if isinstance(p, dict)}
    listed = {f"{cfg.get('catalog')}.{cfg.get('schema')}.{s.get('name')}": s
              for s in cfg.get("model_services") or [] if isinstance(s, dict)}
    shared = granted(load_yaml(folder / "permissions.yaml")[0] or {}) if "grants" in parts else set()
    services = []
    for name in state["model_services"]:
        s = listed.get(name)
        if s is None:
            errors.append(f"{name} isn't in {folder.name}/model-services.yaml: add the evaluation's entry with "
                          "forge-aig-create-model-service (module 00, step 7)")
            continue
        policies = [str(p) for p in s.get("service_policies") or []]
        if not policies:
            errors.append(f"{name} lists no service_policies, so there's nothing to evaluate")
        for policy in policies:
            p = defined.get(policy)
            if p is None:
                errors.append(f"{name}: {policy} isn't defined in service-policies.yaml")
            elif p.get("mode", "block") != "block":
                errors.append(f"{name}: {policy} has mode: {p.get('mode')}, which doesn't block, so its verdicts can't "
                              "be measured. Leave it out of the evaluation's copy")
            elif p.get("type") == "detect_sensitive_data" and p.get("action", "block") != "block":
                errors.append(f"{name}: {policy} redacts instead of blocking, so its verdicts can't be measured. Leave "
                              "it out of the evaluation's copy")
        if name in shared:
            errors.append(f"{name} is granted to others in permissions.yaml, and the evaluation measures only a copy "
                          "of its own: take it out of permissions.yaml")
        if s.get("enable_inference_table", True):
            warnings.append(f"{name} logs to an inference table, a second copy of the harmful prompts: set "
                            "enable_inference_table: false on it")
        if s.get("enable_rate_limits", True):
            warnings.append(f"{name} has rate limits, which slow a run: set enable_rate_limits: false on it")
        services.append({"name": name, "policies": policies})
    return services, errors, warnings


def job_config(state: dict, services: list[dict]) -> dict:
    """eval_config.json: what the job's notebooks read, with defaults filled in."""
    return {
        "dataset": state["dataset"],
        "services": services,
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
    if not PROFILE_BLOCK_RE.search(body):
        raise SystemExit("databricks.yml template has no # >>> profile block")
    # Locally the target keeps its profile; in a notebook (no profile in values) the block goes.
    body = PROFILE_BLOCK_RE.sub(lambda m: m.group(1) if "profile" in values else "", body)
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
    target = parsed["targets"]["default"]
    got.update(bundle_name=parsed["bundle"]["name"], profile=(target.get("workspace") or {}).get("profile"))
    if mismatched := sorted(key for key in values if got.get(key) != values[key]):
        raise SystemExit(f"Rendered databricks.yml doesn't round-trip {mismatched}")
    if "profile" not in values and "workspace" in target:
        raise SystemExit("Rendered databricks.yml names a workspace for its target, which a notebook's deploy "
                         "doesn't use")
    jobs = parsed["resources"]["jobs"]
    if list(jobs) != ["guardrail_eval"]:
        raise SystemExit(f"Rendered databricks.yml has jobs {list(jobs)}")
    job = jobs["guardrail_eval"]
    if job.get("name") != "${bundle.name}":
        raise SystemExit("Rendered databricks.yml's job must be named ${bundle.name}, the job_name")
    if [p["name"] for p in job.get("parameters") or []] != ["mode", "run_id"]:
        raise SystemExit("Rendered databricks.yml's job parameters aren't mode and run_id")
    tasks = {t["task_key"]: t for t in job["tasks"]}
    if list(tasks) != ["load_prompts", "run_eval"]:
        raise SystemExit(f"Rendered databricks.yml has tasks {list(tasks)}")
    params = {"catalog": "${var.catalog}", "schema": "${var.schema}", "table_prefix": "${var.table_prefix}",
              "config": "${workspace.file_path}/src/eval_config.json"}
    for key, task in tasks.items():
        notebook = task.get("notebook_task") or {}
        if notebook.get("notebook_path") != f"src/{key}.py" or notebook.get("base_parameters") != params:
            raise SystemExit(f"Rendered databricks.yml's {key} task isn't a notebook task on src/{key}.py with "
                             f"{params}")
    if tasks["run_eval"].get("depends_on") != [{"task_key": "load_prompts"}]:
        raise SystemExit("Rendered databricks.yml's run_eval task must depend on load_prompts")
    return body


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    workdir = args.state.resolve().parent
    services, errors, warnings = evaluated_services(state, workdir)
    if errors:
        raise SystemExit(f"{args.state}: " + "; ".join(errors))
    values = bundle_values(state)
    bundle = workdir / values["bundle_name"]
    rendered = {
        bundle / "databricks.yml": with_header(render_bundle(values)),
        bundle / "src" / "eval_config.json": config_json(job_config(state, services)),
    }
    copies = {bundle / "src" / name: mark_copy(name, (SCRIPTS / name).read_text()) for name in JOB_FILES}

    conflicts = [str(p) for p, text in rendered.items() if p.exists() and p.read_text() != text and edited_by_hand(p)]
    if conflicts and not args.force:
        raise SystemExit(
            f"Edited by hand since the last render: {conflicts}. Move the change into {STATE_FILE} and re-render, "
            "or pass --force to overwrite (the hand edit is lost)."
        )
    summary = {"services": [s["name"] for s in services], "warnings": warnings, "written": [], "unchanged": [],
               "kept_edited": []}
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
