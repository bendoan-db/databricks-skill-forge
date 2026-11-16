# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68", "pyyaml>=6"]
# ///
"""Deploy a use case's rendered bundle as jobs, and run them, in Genie Code in a notebook (runtime workspace).

    forge_run("<skill>/scripts/job.py", "--state", "<state file>", "--bundle", "<bundle folder>", "deploy")
    forge_run("<skill>/scripts/job.py", "--state", "<state file>", "--bundle", "<bundle folder>", "run",
              "--only", "<task>", "--params", "<name>=<value>,...", "--wait")
    forge_run("<skill>/scripts/job.py", "--state", "<state file>", "--bundle", "<bundle folder>", "status",
              "--job-run-id", "<id>")

With runtime `workspace` there's no Databricks CLI, so this does for the bundle render.py writes in <workdir> what
`databricks bundle deploy` and `bundle run` do locally, through the REST API as the notebook's user. The bundle stays
the source: the user can edit it, and each deploy reads it again. Locally (runtime `local`) it refuses, so the
bundle's own deploy is the only one and each job exists once. Every Forge skill that carries it has an identical copy
(`shared` in ucs.yaml); tests/test_skills.py fails if the copies drift.

deploy imports the bundle's src/ into the folder `bundle deploy` would use, <file_path>/src/, where file_path is
/Workspace/Users/<user>/.bundle/<bundle name>/default/files: each source-format notebook through the import API,
without its .py extension (a file written through /Workspace stays a file, and a notebook task needs a notebook), and
every other file, such as a config, SQL, or an init script, as a file. Then, for each job in databricks.yml, it
resolves ${var.*}, ${bundle.*}, ${workspace.file_path}, ${workspace.root_path}, and
${workspace.current_user.userName} as the CLI does for the default target, and creates the job, or replaces the
settings of the one it created before, which it finds by name and its forge_bundle and forge_job tags. It stops on
what it can't deploy the way the CLI would: a task that isn't a notebook or pipeline task, a notebook outside src/,
a permissions block, or another ${...} reference.

run starts a job (--job, when the bundle has several), or only some of its tasks (--only, as `bundle run --only`),
with job parameters (--params), after checking that the bundle hasn't changed since the last deploy (the job's
forge_digest tag), so an edit isn't silently left out of a run. With --wait it polls until the run ends, or --timeout
minutes pass. status reads a run: its state and link, each task's, and the JSON summary a task's notebook exited with.
Prints JSON, and exits 1 on a problem or a failed run.
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

TARGET = "default"  # the bundle's one target, which names the folder `bundle deploy` uses
BUNDLE_TAG = "forge_bundle"  # with JOB_TAG, marks a job this script deployed, so the next deploy updates it
JOB_TAG = "forge_job"
DIGEST_TAG = "forge_digest"  # a checksum of what was deployed, so run can tell the bundle changed since
NOTEBOOK_HEADER = "# Databricks notebook source"
JOBS = "/api/2.2/jobs"
WORKSPACE = "/api/2.0/workspace"
SCIM_ME = "/api/2.0/preview/scim/v2/Me"
REFERENCE_RE = re.compile(r"\$\{([^}]*)\}")
NOTEBOOK_PATH_RE = re.compile(r"^(?:\./)?(src/[\w./-]+)\.py$")
TASK_KINDS = {"notebook_task", "pipeline_task"}  # a pipeline task names its pipeline by id: no path to deploy
BUNDLE_ONLY = {"permissions"}  # job fields the CLI applies apart from the job's settings
TERMINAL = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
POLL_SECONDS = 30
RUNTIMES = ("local", "workspace")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="The use case's state file in <workdir>")
    p.add_argument("--bundle", required=True, type=Path, help="The folder holding the rendered databricks.yml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("deploy", help="Import the bundle's src/, and create or update each job")
    run = sub.add_parser("run", help="Start a job, after checking it has the bundle's latest deploy")
    run.add_argument("--job", default="", help="The job's key in databricks.yml, when the bundle has several")
    run.add_argument("--only", default="", help="Comma-separated task keys to run, such as parse")
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
    if errors := runtime_errors(state):
        raise SystemExit(f"{path}: " + "; ".join(errors))
    if state.get("runtime", "local") != "workspace":
        raise SystemExit(f"{path}: runtime is local, so deploy and run with the bundle (`databricks bundle deploy`), "
                         "which keeps one copy of each job")
    return state


def read_bundle(folder: Path) -> dict:
    path = folder / "databricks.yml"
    if not path.is_file():
        raise SystemExit(f"{path} not found: render.py writes it")
    try:
        bundle = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{path} isn't valid YAML: {e}") from None
    if not isinstance(bundle, dict) or not (bundle.get("bundle") or {}).get("name"):
        raise SystemExit(f"{path} has no bundle.name")
    return bundle


def references(bundle: dict, file_path: str, me: str) -> dict:
    """What each ${...} stands for, as `bundle deploy` resolves it for the default target."""
    variables = {name: (spec or {}).get("default") for name, spec in (bundle.get("variables") or {}).items()}
    overrides = ((bundle.get("targets") or {}).get(TARGET) or {}).get("variables") or {}
    variables.update({k: v.get("default") if isinstance(v, dict) else v for k, v in overrides.items()})
    refs = {f"var.{name}": value for name, value in variables.items() if value is not None}
    refs.update({"bundle.name": bundle["bundle"]["name"], "bundle.target": TARGET,
                 "workspace.root_path": file_path.rsplit("/", 1)[0], "workspace.file_path": file_path,
                 "workspace.current_user.userName": me})
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


def job_settings(bundle: dict, file_path: str, me: str) -> dict[str, dict]:
    """Each job in the bundle, by its key, as Jobs API settings with its notebook paths in file_path."""
    resources = bundle.get("resources") or {}
    jobs = resources.get("jobs") or {}
    if set(resources) != {"jobs"} or not jobs:
        raise SystemExit(f"databricks.yml must define jobs and nothing else, not {sorted(resources)}: deploy it with "
                         "the CLI instead")
    refs, errors, out = references(bundle, file_path, me), [], {}
    for key, job in jobs.items():
        errors += [f"job {key}: {field} is applied by the CLI apart from the job, so set it in the workspace instead"
                   for field in sorted(set(job) & BUNDLE_ONLY)]
        tasks = []
        for task in job.get("tasks") or []:
            kinds = sorted(k for k in task if k.endswith("_task"))
            if len(kinds) != 1 or kinds[0] not in TASK_KINDS:
                errors.append(f"job {key}, task {task.get('task_key')}: only notebook and pipeline tasks deploy this "
                              f"way, not {kinds}")
            elif kinds == ["notebook_task"]:
                notebook = dict(task["notebook_task"] or {})
                match = NOTEBOOK_PATH_RE.match(str(notebook.get("notebook_path", "")))
                if not match or ".." in match.group(1).split("/"):
                    errors.append(f"job {key}, task {task.get('task_key')}: notebook_path "
                                  f"{notebook.get('notebook_path')!r} must be src/<file>.py in the bundle")
                else:
                    notebook.update(notebook_path=f"{file_path}/{match.group(1)}", source="WORKSPACE")
                task = {**task, "notebook_task": notebook}
            tasks.append(task)
        if not tasks:
            errors.append(f"job {key} has no tasks")
        settings = resolve({**job, "tasks": tasks}, refs, errors)
        settings["tags"] = {**(settings.get("tags") or {}), BUNDLE_TAG: bundle["bundle"]["name"], JOB_TAG: key}
        out[key] = settings
    if errors:
        raise SystemExit("databricks.yml: " + "; ".join(dict.fromkeys(errors)))
    return out


def bundle_files(folder: Path) -> dict[str, bytes]:
    """The bundle's src/ files, by their path in the bundle, which deploy imports."""
    files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted((folder / "src").rglob("*"))
             if p.is_file() and not any(part.startswith((".", "__")) for part in p.relative_to(folder).parts)}
    if not files:
        raise SystemExit(f"{folder / 'src'} has no files: render.py writes them")
    return files


def digest(settings: dict, files: dict[str, bytes]) -> str:
    """A checksum of what a deploy sends for a job, its settings and the files, so run can tell the bundle changed."""
    h = hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
    for path, data in sorted(files.items()):
        h.update(f"\n{path}\n".encode() + hashlib.sha256(data).digest())
    return h.hexdigest()[:12]


def current_user(client) -> str:
    me = (client.do("GET", SCIM_ME) or {}).get("userName")
    if not me:
        raise SystemExit("Couldn't read the notebook user's name, which names the bundle's folder")
    return me


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


def find_jobs(client, name: str, bundle_name: str, key: str) -> tuple[list[dict], int]:
    """The jobs named `name` that this script deployed as the bundle's job `key`, each {job_id, settings}, and how
    many other jobs have that name."""
    ours, others, token = [], 0, None
    while True:
        query = {"name": name, "limit": 100, **({"page_token": token} if token else {})}
        page = client.do("GET", f"{JOBS}/list", query=query) or {}
        for job in page.get("jobs") or []:
            settings = (client.do("GET", f"{JOBS}/get", query={"job_id": job["job_id"]}) or {}).get("settings") or {}
            tags = settings.get("tags") or {}
            if tags.get(BUNDLE_TAG) == bundle_name and tags.get(JOB_TAG) == key:
                ours.append({"job_id": job["job_id"], "settings": settings})
            else:
                others += 1
        token = page.get("next_page_token")
        if not token:
            return ours, others


def planned(client, folder: Path) -> dict:
    """What a deploy sends: each job's settings and checksum, the files, and the bundle's folder in the workspace."""
    bundle = read_bundle(folder)
    name = bundle["bundle"]["name"]
    me = current_user(client)
    file_path = f"/Workspace/Users/{me}/.bundle/{name}/{TARGET}/files"
    jobs = job_settings(bundle, file_path, me)
    files = bundle_files(folder)
    return {"jobs": jobs, "files": files, "digests": {key: digest(s, files) for key, s in jobs.items()},
            "file_path": file_path, "bundle_name": name}


def deployed_job(client, plan: dict, key: str) -> tuple[dict | None, int]:
    name = plan["jobs"][key]["name"]
    ours, others = find_jobs(client, name, plan["bundle_name"], key)
    if len(ours) > 1:
        raise SystemExit(f"{len(ours)} jobs named {name!r} carry {BUNDLE_TAG}={plan['bundle_name']} and {JOB_TAG}="
                         f"{key}: {[j['job_id'] for j in ours]}. Keep one; deleting the others needs a confirmation "
                         "from the user that names each")
    return (ours[0] if ours else None), others


def deploy(w, folder: Path) -> dict:
    client = w.api_client
    plan = planned(client, folder)
    found = {key: deployed_job(client, plan, key) for key in plan["jobs"]}
    imported = import_files(client, plan["file_path"], plan["files"])
    jobs, warnings = {}, []
    for key, settings in plan["jobs"].items():
        job, others = found[key]
        settings = {**settings, "tags": {**settings["tags"], DIGEST_TAG: plan["digests"][key]}}
        if job:
            client.do("POST", f"{JOBS}/reset", body={"job_id": job["job_id"], "new_settings": settings})
            job_id, action = job["job_id"], "updated"
        else:
            job_id, action = client.do("POST", f"{JOBS}/create", body=settings)["job_id"], "created"
        jobs[key] = {"job_id": job_id, "action": action, "url": f"{w.config.host.rstrip('/')}/jobs/{job_id}"}
        if others:
            warnings.append(f"{others} other job(s) are also named {settings['name']!r}: this deploy left them alone")
    return {"jobs": jobs, "file_path": plan["file_path"], "imported": imported, "warnings": warnings}


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


def start(w, folder: Path, job_key: str, only: str, params: str) -> int:
    """Start a deployed job, after checking it has the bundle's latest deploy; returns the job run's id."""
    client = w.api_client
    plan = planned(client, folder)
    keys = list(plan["jobs"])
    if not job_key and len(keys) > 1:
        raise SystemExit(f"--job: the bundle has the jobs {keys}; name one")
    key = job_key or keys[0]
    if key not in plan["jobs"]:
        raise SystemExit(f"--job {key!r} isn't in the bundle, which has the jobs {keys}")
    job, _ = deployed_job(client, plan, key)
    if job is None:
        raise SystemExit(f"No job {plan['jobs'][key]['name']!r} yet: deploy first")
    if (job["settings"].get("tags") or {}).get(DIGEST_TAG) != plan["digests"][key]:
        raise SystemExit("The bundle changed since the last deploy, so the job would run the old version: deploy "
                         "again first")
    tasks = [t["task_key"] for t in plan["jobs"][key]["tasks"]]
    chosen = [k.strip() for k in only.split(",") if k.strip()]
    if unknown := sorted(set(chosen) - set(tasks)):
        raise SystemExit(f"--only: {unknown} aren't tasks of the job, which has {tasks}")
    values = parse_params(params, [p["name"] for p in plan["jobs"][key].get("parameters") or []])
    body = {"job_id": job["job_id"], **({"job_parameters": values} if values else {}),
            **({"only": chosen} if chosen else {})}
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
    w = workspace_client(state)
    folder = args.bundle.resolve()
    if args.command == "deploy":
        print(json.dumps(deploy(w, folder), indent=2))
        return
    if args.command == "run":
        job_run_id = start(w, folder, args.job, args.only, args.params)
        out = wait(w.api_client, job_run_id, args.timeout) if args.wait else run_status(w.api_client, job_run_id)
    else:
        out = run_status(w.api_client, args.job_run_id)
    print(json.dumps(out, indent=2))
    sys.exit(1 if out["done"] and not out["succeeded"] else 0)


if __name__ == "__main__":
    main()
