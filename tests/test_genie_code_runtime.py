"""Checks for the skills that run in Genie Code in a notebook (runtime workspace) as well as locally: the forge_run
helper and %pip line their SKILL.md gives, and the scripts it runs. In a notebook only the notebook's own Python
process is signed in as its user, so every script must run in that process and sign in through workspace_client. No
workspace."""

import ast
import contextlib
import io
import json
import re
import sys
from pathlib import Path

import pytest
from conftest import SKILLS, SKILLS_DIR

NOTEBOOK_SECTION = "\n### In a notebook\n"
NOTEBOOK_SKILLS = sorted(s for s in SKILLS if NOTEBOOK_SECTION in (SKILLS_DIR / s / "SKILL.md").read_text())
NOTEBOOK_HEADER = "# Databricks notebook source"
SHARED_FUNCTIONS = ("runtime_errors", "workspace_client")


def test_the_two_gateway_skills_run_in_a_notebook():
    assert NOTEBOOK_SKILLS == ["forge-aig-create-model-service", "forge-aig-evaluate-guardrails"]


def section(skill: str) -> str:
    text = (SKILLS_DIR / skill / "SKILL.md").read_text()
    return text.split(NOTEBOOK_SECTION, 1)[1].split("\n## ", 1)[0]


def python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.DOTALL)


def forge_run_source(skill: str) -> str:
    blocks = [b for b in python_blocks(section(skill)) if "def forge_run" in b]
    assert len(blocks) == 1, f"{skill}: SKILL.md, In a notebook, needs one forge_run block"
    return blocks[0]


def in_place_scripts(skill: str) -> list[Path]:
    """The scripts the agent runs: every scripts/*.py but the job's notebooks."""
    return [p for p in sorted((SKILLS_DIR / skill / "scripts").glob("*.py"))
            if not p.read_text().startswith(NOTEBOOK_HEADER)]


def dependencies(script: Path) -> set[str]:
    match = re.search(r"^# dependencies = (\[.*\])$", script.read_text(), re.MULTILINE)
    return set(json.loads(match.group(1))) if match else set()


def test_the_notebook_section_is_the_same_in_each_skill():
    sources = {skill: forge_run_source(skill) for skill in NOTEBOOK_SKILLS}
    assert len(set(sources.values())) == 1, "forge_run differs between the skills; copy it over"
    pip = {skill: re.findall(r"^%pip install .*$", section(skill), re.MULTILINE) for skill in NOTEBOOK_SKILLS}
    assert all(len(lines) == 1 for lines in pip.values()) and len({lines[0] for lines in pip.values()}) == 1


@pytest.mark.parametrize("skill", NOTEBOOK_SKILLS)
def test_the_pip_line_installs_what_the_scripts_declare(skill: str):
    line = re.search(r"^%pip install .*$", section(skill), re.MULTILINE).group(0)
    installed = set(re.findall(r'"([^"]+)"', line))
    declared = set().union(*(dependencies(p) for p in in_place_scripts(skill)))
    assert installed == declared, f"{skill}: %pip installs {sorted(installed)}, the scripts declare {sorted(declared)}"


def functions(script: Path) -> dict[str, str]:
    tree = ast.parse(script.read_text())
    return {node.name: ast.get_source_segment(script.read_text(), node) for node in tree.body
            if isinstance(node, ast.FunctionDef)}


def test_every_script_signs_in_the_same_way():
    """workspace_client and runtime_errors are copies: scripts are published separately and can't import each other."""
    copies: dict[str, set[str]] = {name: set() for name in SHARED_FUNCTIONS}
    for skill in NOTEBOOK_SKILLS:
        for script in in_place_scripts(skill):
            text, found = script.read_text(), functions(script)
            if "databricks-sdk" in str(dependencies(script)):
                assert "workspace_client" in found, f"{skill}/{script.name} signs in without workspace_client"
                assert text.count("WorkspaceClient(") == 2, (
                    f"{skill}/{script.name}: only workspace_client builds a client")
            if '"profile"' in text or "state.get(\"runtime\"" in text:
                assert "runtime_errors" in found, f"{skill}/{script.name} reads the state without runtime_errors"
            for name in SHARED_FUNCTIONS:
                if name in found:
                    copies[name].add(found[name])
    assert all(len(sources) == 1 for sources in copies.values()), (
        "runtime_errors or workspace_client differ between scripts; copy them over")


@pytest.mark.parametrize("skill", NOTEBOOK_SKILLS)
def test_no_script_starts_a_subprocess(skill: str):
    for script in in_place_scripts(skill):
        assert not re.search(r"^\s*(import subprocess|from subprocess)", script.read_text(), re.MULTILINE), (
            f"{skill}/{script.name}: a subprocess isn't signed in as the notebook's user; run it in this process")


@pytest.fixture
def forge_run():
    namespace: dict = {}
    exec(forge_run_source(NOTEBOOK_SKILLS[0]), namespace)  # noqa: S102 - the helper SKILL.md has Genie Code define
    return namespace["forge_run"]


def test_forge_run_passes_arguments_and_returns_the_exit_status(forge_run, tmp_path: Path):
    script = tmp_path / "probe.py"
    script.write_text("import sys\nprint(sys.argv[1:])\nif sys.argv[1:2] == ['fail']:\n    sys.exit('it failed')\n"
                      "if sys.argv[1:2] == ['two']:\n    sys.exit(2)\n")
    argv = list(sys.argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        assert forge_run(str(script), "ok", 3) == 0
        assert forge_run(str(script), "two") == 2
        assert forge_run(str(script), "fail") == 1
    assert out.getvalue().splitlines() == ["['ok', '3']", "['two']", "['fail']"] and err.getvalue() == "it failed\n"
    assert sys.argv == argv


def test_forge_run_runs_the_skill_scripts(forge_run, tmp_path: Path):
    skill = SKILLS_DIR / "forge-aig-create-model-service"
    state = tmp_path / "aig-model-services.json"
    state.write_text(json.dumps({"runtime": "workspace", "target_schema": "main.ai_gateway", "catalog": "main",
                                 "schema": "ai_gateway", "parts": ["model_services"], "config_dir": "ai-gateway",
                                 "completed": []}))
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert forge_run(str(skill / "scripts" / "render.py"), "--state", str(state)) == 0
    assert (tmp_path / "ai-gateway" / "model-services.yaml").is_file() and "to_fill" in out.getvalue()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
        status = forge_run(str(skill / "scripts" / "check_base_skills.py"), "--skills-dir", str(tmp_path))
    assert status == 1 and "Base skills missing" in err.getvalue()
