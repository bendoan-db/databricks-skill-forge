"""Shared helpers for the checks under tests/. Nothing here needs a Databricks workspace."""

import importlib.util
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # importing skill scripts must not leave __pycache__ in the published skill folders
SKILLS_DIR = Path(__file__).resolve().parents[1] / "use-case-skills"
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
