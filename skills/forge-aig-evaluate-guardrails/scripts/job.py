# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Deploy the evaluation's bundle as a job, and run it, in Genie Code in a notebook (modules 01, 03).

    forge_run("<skill>/scripts/job.py", "--state", "<workdir>/guardrail-eval.json", "deploy")
    forge_run("<skill>/scripts/job.py", "--state", "<workdir>/guardrail-eval.json", "run", "--only", "load_prompts",
              "--wait")
    forge_run("<skill>/scripts/job.py", "--state", "<workdir>/guardrail-eval.json", "run", "--only", "run_eval",
              "--params", "mode=full")
    forge_run("<skill>/scripts/job.py", "--state", "<workdir>/guardrail-eval.json", "status", "--job-run-id", "<id>")

With runtime `workspace` there's no Databricks CLI, so this does for the bundle render.py writes in <workdir>/<name>/
what `databricks bundle deploy` and `bundle run` do locally, through the REST API as the notebook's user. The bundle
stays the source: the user can edit it, and each deploy reads it again. Locally (runtime `local`) it refuses, so the
bundle's own deploy is the only one and there's one job.

deploy imports the bundle's src/ into the folder `bundle deploy` would use, <file_path>/src/, where file_path is
/Workspace/Users/<user>/.bundle/<bundle>/default/files: each source-format notebook through the import API, without its
.py extension (a file written through /Workspace stays a file, and a notebook task needs a notebook), and every other
file, such as eval_config.json, as a file. Then it creates the job from databricks.yml, resolving ${var.*},
${bundle.*}, and ${workspace.*} as the CLI does for the default target, or replaces the settings of the job it created
before, which it finds by name and its forge_bundle tag. It stops on what it can't deploy the way the CLI would: a task
that isn't a notebook task, a notebook outside src/, a permissions block, or another ${...} reference.

run starts the job, or only some of its tasks (--only, as `bundle run --only`), with job parameters (--params
mode=full,run_id=<run_id>), after checking that the bundle hasn't changed since the last deploy (the job's forge_digest
tag), so an edit isn't silently left out of a run. With --wait it polls until the run ends, or --timeout minutes pass.
status reads a run: its state and link, each task's, and the JSON summary each task's notebook ends with. Prints JSON,
and exits 1 on a problem or a failed run.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import yaml

STATE_FILE = "guardrail-eval.json"
TARGET = "default"  # the bundle's one target, which names the folder `bundle deploy` uses
BUNDLE_TAG = "forge_bundle"  # marks the job this script deployed for a bundle, so the next deploy updates it
DIGEST_TAG = "forge_digest"  # a checksum of what was deployed, so run can tell the bundle changed since
NOTEBOOK_HEADER = "# Databricks notebook source"
JOBS = "/api/2.2/jobs"
WORKSPACE = "/api/2.0/workspace"
SCIM_ME = "/api/2.0/preview/scim/v2/Me"
REFERENCE_RE = re.compile(r"\$\{([^}]*)\}")
NOTEBOOK_PATH_RE = re.compile(r"^(?:\./)?(src/[\w./-]+)\.py$")
BUNDLE_ONLY = {"permissions"}  # job fields the CLI applies apart from the job's settings
TERMINAL = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
POLL_SECONDS = 30
RUNTIMES = ("local", "workspace")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help=f"<workdir>/{STATE_FILE}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("deploy", help="Import the bundle's src/, and create or update the job")
    run = sub.add_parser("run", help="Start the job, after checking it has the bundle's latest deploy")
    run.add_argument("--only", default="", help="Comma-separated task keys to run, such as load_prompts")
    run.add_argument("--params", default="", help="Job parameters, such as mode=full,run_id=<run_id>")
    run.add_argument("--wait", action="store_true", help="Wait until the run ends")
    run.add_argument("--timeout", type=float, default=30, help="Minutes to wait with --wait (default 30)")
    status = sub.add_parser("status", help="Read a run, and each task's summary")
    status.add_argument("--job-run-id", required=True, type=int, help="The job run's id, which run prints")
    return p.parse_args(argv)


def runtime_errors(state: dict) -> list[str]:
    """Problems with where the skill runs: `local`, with a CLI profile, or `workspace`, Genie Code in a notebook."""
    runtime = state.get("runtime", "local")
    if runtime not in RUNTIMES:
        return [f"runtime={runtime!r}: local or workspace (module 00)"]
    if runtime == "local" and not state.get("profile"):
        return ["profile is missing or empty: locally, every command uses the CLI profile"]
    return []


def workspace_client(state: dict):
    """The workspace: as the CLI profile locally, or with runtime workspace as the notebook's user, which only the
    notebook's own Python process can sign in as (so the modules run scripts with forge_run, not a subprocess)."""
    from databricks.sdk import WorkspaceClient

    if state.get("runtime") == "workspace":
        return WorkspaceClient()
    return WorkspaceClient(profile=state["profile"])


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = runtime_errors(state) + [f"{key} is missing or empty" for key in ("name",) if not state.get(key)]
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    if state.get("runtime", "local") != "workspace":
        raise SystemExit(f"{path}: runtime is local, so deploy and run with the bundle (`databricks bundle deploy`, "
                         "module 01, step 2), which keeps one job")
    return state


def read_bundle(folder: Path) -> dict:
    path = folder / "databricks.yml"
    if not path.is_file():
        raise SystemExit(f"{path} not found: render.py writes it (module 01, step 1)")
    try:
        bundle = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{path} isn't valid YAML: {e}") from None
    if not isinstance(bundle, dict) or not (bundle.get("bundle") or {}).get("name"):
        raise SystemExit(f"{path} has no bundle.name")
    return bundle


def references(bundle: dict, file_path: str) -> dict:
    """What each ${...} stands for, as `bundle deploy` resolves it for the default target."""
    variables = {name: (spec or {}).get("default") for name, spec in (bundle.get("variables") or {}).items()}
    overrides = ((bundle.get("targets") or {}).get(TARGET) or {}).get("variables") or {}
    variables.update({k: v.get("default") if isinstance(v, dict) else v for k, v in overrides.items()})
    refs = {f"var.{name}": value for name, value in variables.items() if value is not None}
    refs.update({"bundle.name": bundle["bundle"]["name"], "bundle.target": TARGET,
                 "workspace.root_path": file_path.rsplit("/", 1)[0], "workspace.file_path": file_path})
    return refs


def resolve(value, refs: dict, errors: list[str]):
    """value with each ${...} replaced; one this script can't resolve is added to errors."""
    if isinstance(value, dict):
        return {k: resolve(v, refs, errors) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, refs, errors) for v in value]
    if not isinstance(value, str):
        return value

    def one(match: re.Match) -> str:
        if match.group(1) not in refs:
            errors.append(f"${{{match.group(1)}}} can't be resolved here (only {sorted(refs)}): deploy with the CLI")
            return match.group(0)
        return str(refs[match.group(1)])

    return REFERENCE_RE.sub(one, value)


def job_settings(bundle: dict, file_path: str) -> tuple[str, dict]:
    """The bundle's one job, as Jobs API settings, with its notebook paths in file_path; and the job's key."""
    resources = bundle.get("resources") or {}
    jobs = resources.get("jobs") or {}
    if set(resources) != {"jobs"} or len(jobs) != 1:
        raise SystemExit(f"databricks.yml must define one job and nothing else, not {sorted(resources)} with jobs "
                         f"{sorted(jobs)}: deploy it with the CLI instead")
    key, job = next(iter(jobs.items()))
    errors = [f"job {key}: {field} is applied by the CLI apart from the job, so set it in the workspace instead"
              for field in sorted(set(job) & BUNDLE_ONLY)]
    tasks = []
    for task in job.get("tasks") or []:
        kinds = sorted(k for k in task if k.endswith("_task"))
        notebook = dict(task.get("notebook_task") or {})
        match = NOTEBOOK_PATH_RE.match(str(notebook.get("notebook_path", "")))
        if kinds != ["notebook_task"]:
            errors.append(f"task {task.get('task_key')}: only notebook tasks deploy this way, not {kinds}")
        elif not match or ".." in match.group(1).split("/"):
            errors.append(f"task {task.get('task_key')}: notebook_path {notebook.get('notebook_path')!r} must be "
                          "src/<file>.py in the bundle")
        else:
            notebook.update(notebook_path=f"{file_path}/{match.group(1)}", source="WORKSPACE")
        tasks.append({**task, "notebook_task": notebook})
    if not tasks:
        errors.append(f"job {key} has no tasks")
    settings = resolve({**job, "tasks": tasks}, references(bundle, file_path), errors)
    if errors:
        raise SystemExit("databricks.yml: " + "; ".join(dict.fromkeys(errors)))
    settings["tags"] = {**(settings.get("tags") or {}), BUNDLE_TAG: bundle["bundle"]["name"]}
    return key, settings


def bundle_files(folder: Path) -> dict[str, bytes]:
    """The bundle's src/ files, by their path in the bundle, which deploy imports."""
    files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted((folder / "src").rglob("*"))
             if p.is_file() and not any(part.startswith((".", "__")) for part in p.relative_to(folder).parts)}
    if not files:
        raise SystemExit(f"{folder / 'src'} has no files: render.py writes them (module 01, step 1)")
    return files


def digest(settings: dict, files: dict[str, bytes]) -> str:
    """A checksum of what a deploy sends, the job's settings and the files, so run can tell the bundle changed."""
    h = hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
    for path, data in sorted(files.items()):
        h.update(f"\n{path}\n".encode() + hashlib.sha256(data).digest())
    return h.hexdigest()[:12]


def bundle_file_path(client, bundle_name: str) -> str:
    """The folder `bundle deploy` syncs the bundle's files to, for the notebook's user."""
    me = (client.do("GET", SCIM_ME) or {}).get("userName")
    if not me:
        raise SystemExit("Couldn't read the notebook user's name, which names the bundle's folder")
    return f"/Workspace/Users/{me}/.bundle/{bundle_name}/{TARGET}/files"


def import_files(client, file_path: str, files: dict[str, bytes]) -> list[str]:
    """Import each file into file_path: a source-format .py notebook as a notebook, without the extension, as `bundle
    deploy` leaves it; anything else as a workspace file. Returns the workspace paths."""
    for folder in sorted({f"{file_path}/{path}".rsplit("/", 1)[0] for path in files}):
        client.do("POST", f"{WORKSPACE}/mkdirs", body={"path": folder})
    imported = []
    for path, data in sorted(files.items()):
        body = {"content": base64.b64encode(data).decode(), "overwrite": True}
        if path.endswith(".py") and data.startswith(NOTEBOOK_HEADER.encode()):
            body.update(path=f"{file_path}/{path.removesuffix('.py')}", format="SOURCE", language="PYTHON")
        else:
            body.update(path=f"{file_path}/{path}", format="AUTO")
        client.do("POST", f"{WORKSPACE}/import", body=body)
        imported.append(body["path"])
    return imported


def find_jobs(client, name: str, bundle_name: str) -> tuple[list[dict], int]:
    """The jobs named `name` that this script deployed for the bundle, each {job_id, settings}, and how many other
    jobs have that name."""
    ours, others, token = [], 0, None
    while True:
        query = {"name": name, "limit": 100, **({"page_token": token} if token else {})}
        page = client.do("GET", f"{JOBS}/list", query=query) or {}
        for job in page.get("jobs") or []:
            settings = (client.do("GET", f"{JOBS}/get", query={"job_id": job["job_id"]}) or {}).get("settings") or {}
            if (settings.get("tags") or {}).get(BUNDLE_TAG) == bundle_name:
                ours.append({"job_id": job["job_id"], "settings": settings})
            else:
                others += 1
        token = page.get("next_page_token")
        if not token:
            return ours, others


def planned(client, state: dict, workdir: Path) -> dict:
    """What a deploy sends: the job's key and settings, the files, their checksum, and the bundle's folder."""
    folder = workdir / state["name"]
    bundle = read_bundle(folder)
    file_path = bundle_file_path(client, bundle["bundle"]["name"])
    key, settings = job_settings(bundle, file_path)
    files = bundle_files(folder)
    return {"key": key, "settings": settings, "files": files, "digest": digest(settings, files),
            "file_path": file_path, "bundle_name": bundle["bundle"]["name"]}


def deployed_job(client, plan: dict) -> tuple[dict | None, int]:
    ours, others = find_jobs(client, plan["settings"]["name"], plan["bundle_name"])
    if len(ours) > 1:
        raise SystemExit(f"{len(ours)} jobs named {plan['settings']['name']!r} carry {BUNDLE_TAG}="
                         f"{plan['bundle_name']}: {[j['job_id'] for j in ours]}. Keep one; deleting the others needs "
                         "a confirmation from the user that names each")
    return (ours[0] if ours else None), others


def deploy(w, state: dict, workdir: Path) -> dict:
    client = w.api_client
    plan = planned(client, state, workdir)
    job, others = deployed_job(client, plan)
    settings = {**plan["settings"], "tags": {**plan["settings"]["tags"], DIGEST_TAG: plan["digest"]}}
    imported = import_files(client, plan["file_path"], plan["files"])
    if job:
        client.do("POST", f"{JOBS}/reset", body={"job_id": job["job_id"], "new_settings": settings})
        job_id, action = job["job_id"], "updated"
    else:
        job_id, action = client.do("POST", f"{JOBS}/create", body=settings)["job_id"], "created"
    warnings = ([f"{others} other job(s) are also named {settings['name']!r}: this deploy left them alone"]
                if others else [])
    return {"job": plan["key"], "job_id": job_id, "action": action, "url": f"{w.config.host.rstrip('/')}/jobs/{job_id}",
            "file_path": plan["file_path"], "imported": imported, "warnings": warnings}


def parse_params(text: str, allowed: list[str]) -> dict[str, str]:
    params = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"--params {text!r}: name=value pairs, such as mode=full,run_id=<run_id>")
        params[name.strip()] = value.strip()
    if unknown := sorted(set(params) - set(allowed)):
        raise SystemExit(f"--params: {unknown} aren't parameters of the job, which has {allowed}")
    return params


def start(w, state: dict, workdir: Path, only: str, params: str) -> int:
    """Start the deployed job, after checking it has the bundle's latest deploy; returns the job run's id."""
    client = w.api_client
    plan = planned(client, state, workdir)
    job, _ = deployed_job(client, plan)
    if job is None:
        raise SystemExit(f"No job {plan['settings']['name']!r} yet: deploy first (module 01, step 2)")
    if (job["settings"].get("tags") or {}).get(DIGEST_TAG) != plan["digest"]:
        raise SystemExit("The bundle changed since the last deploy, so the job would run the old version: deploy "
                         "again first (module 01, step 2)")
    keys = [t["task_key"] for t in plan["settings"]["tasks"]]
    tasks = [k.strip() for k in only.split(",") if k.strip()]
    if unknown := sorted(set(tasks) - set(keys)):
        raise SystemExit(f"--only: {unknown} aren't tasks of the job, which has {keys}")
    values = parse_params(params, [p["name"] for p in plan["settings"].get("parameters") or []])
    body = {"job_id": job["job_id"], **({"job_parameters": values} if values else {}),
            **({"only": tasks} if tasks else {})}
    return client.do("POST", f"{JOBS}/run-now", body=body)["run_id"]


def run_status(client, job_run_id: int) -> dict:
    """A job run's state and link, and each task's, with the JSON summary its notebook exited with."""
    run = client.do("GET", f"{JOBS}/runs/get", query={"run_id": job_run_id}) or {}
    tasks = []
    for task in run.get("tasks") or []:
        state = task.get("state") or {}
        entry = {"task_key": task.get("task_key"), "state": state.get("life_cycle_state"),
                 "result": state.get("result_state"), "message": state.get("state_message") or None}
        if state.get("life_cycle_state") in TERMINAL and task.get("run_id"):
            try:
                output = client.do("GET", f"{JOBS}/runs/get-output", query={"run_id": task["run_id"]}) or {}
            except Exception as e:  # noqa: BLE001 - a skipped task has no output; the state says enough
                output = {"error": f"its output couldn't be read ({type(e).__name__})"}
            result = (output.get("notebook_output") or {}).get("result")
            if result:
                try:
                    entry["output"] = json.loads(result)
                except ValueError:
                    entry["output"] = result
            if output.get("error"):
                entry["error"] = str(output["error"])[:2000]
        tasks.append(entry)
    state = run.get("state") or {}
    done = state.get("life_cycle_state") in TERMINAL
    return {"job_run_id": job_run_id, "url": run.get("run_page_url"), "state": state.get("life_cycle_state"),
            "result": state.get("result_state"), "done": done,
            "succeeded": done and state.get("result_state") == "SUCCESS", "tasks": tasks}


def wait(client, job_run_id: int, minutes: float, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The run's status once it ends, or when `minutes` have passed (then `done` is false)."""
    deadline = clock() + minutes * 60
    while True:
        status = run_status(client, job_run_id)
        if status["done"] or clock() >= deadline:
            return status
        sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    state = load_state(args.state)
    workdir = args.state.resolve().parent
    w = workspace_client(state)
    if args.command == "deploy":
        print(json.dumps(deploy(w, state, workdir), indent=2))
        return
    if args.command == "run":
        job_run_id = start(w, state, workdir, args.only, args.params)
        out = wait(w.api_client, job_run_id, args.timeout) if args.wait else run_status(w.api_client, job_run_id)
    else:
        out = run_status(w.api_client, args.job_run_id)
    print(json.dumps(out, indent=2))
    sys.exit(1 if out["done"] and not out["succeeded"] else 0)


if __name__ == "__main__":
    main()
