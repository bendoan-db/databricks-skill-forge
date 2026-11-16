"""Shared helpers for the checks under tests/. Nothing here needs a Databricks workspace."""

import importlib.util
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing skill scripts must not leave __pycache__ in the published skill folders
SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
SKILLS = sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "ucs.yaml").is_file())


def load_script(skill: str, name: str):
    """Import <skill>/scripts/<name>.py under a unique module name; skills reuse script names."""
    path = SKILLS_DIR / skill / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{skill.replace('-', '_')}__{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(skill: str, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-B", str(SKILLS_DIR / skill / "scripts" / script), *args],
        capture_output=True, text=True, check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


class FakeJobsWorkspace:
    """The Jobs, Workspace, and SCIM APIs, as far as the shared job.py uses them, keeping the jobs, files, and runs
    it's sent. Stands in for both a WorkspaceClient (config.host, api_client) and its api_client (do)."""

    def __init__(self, me: str):
        from types import SimpleNamespace

        self.me = me
        self.config = SimpleNamespace(host="https://example.cloud.databricks.com/")
        self.api_client = self
        self.jobs, self.files, self.started, self.runs, self.outputs = {}, {}, [], {}, {}
        self.next_id = 100

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def file_text(self, path: str) -> str:
        import base64

        return base64.b64decode(self.files[path]["content"]).decode()

    def do(self, method, path, query=None, body=None):
        import copy

        if path == "/api/2.0/preview/scim/v2/Me":
            return {"userName": self.me}
        if path == "/api/2.0/workspace/mkdirs":
            return {}
        if path == "/api/2.0/workspace/import":
            self.files[body["path"]] = body
            return {}
        if path == "/api/2.2/jobs/list":
            found = [{"job_id": i} for i, settings in self.jobs.items()
                     if settings["name"].lower() == query["name"].lower()]
            return {"jobs": found} if found else {}
        if path == "/api/2.2/jobs/get":
            return {"job_id": query["job_id"], "settings": copy.deepcopy(self.jobs[query["job_id"]])}
        if path == "/api/2.2/jobs/create":
            job_id = self.new_id()
            self.jobs[job_id] = copy.deepcopy(body)
            return {"job_id": job_id}
        if path == "/api/2.2/jobs/reset":
            self.jobs[body["job_id"]] = copy.deepcopy(body["new_settings"])
            return {}
        if path == "/api/2.2/jobs/run-now":
            self.started.append(body)
            return {"run_id": self.new_id()}
        if path == "/api/2.2/jobs/runs/get":
            return self.runs.get(query["run_id"], {"state": {"life_cycle_state": "PENDING"}, "tasks": []})
        if path == "/api/2.2/jobs/runs/get-output":
            return self.outputs.get(query["run_id"], {})
        raise AssertionError(f"unexpected {method} {path}")

