"""Checks every use-case skill must pass: manifest vs. module text, versions, thresholds, shared files, and folder limits.

The forge router and the forge-smith builder have no ucs.yaml, so only the folder limits apply to them here;
tests/test_forge.py and tests/test_forge_smith.py check the rest.

    uv run --no-project --with pytest --with pyyaml --with pypdf --with rapidfuzz --with scipy \
        pytest -q -p no:cacheprovider tests
"""

import ast
import re
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR, load_script, run_script


def manifest(skill: str) -> dict:
    return yaml.safe_load((SKILLS_DIR / skill / "ucs.yaml").read_text())


def test_skills_found():
    assert {"forge-doc-qa-agent", "forge-structured-doc-extraction", "forge-unstructured-doc-extraction"} <= set(SKILLS)


@pytest.mark.parametrize("skill", SKILLS)
def test_manifest_matches_skill_text(skill: str):
    folder = SKILLS_DIR / skill
    text = (folder / "SKILL.md").read_text()
    meta = yaml.safe_load(text.split("---")[1])
    ucs = manifest(skill)
    assert meta["name"] == skill == ucs["name"]
    assert meta["metadata"]["version"] == ucs["version"]
    assert meta["metadata"]["forge_kind"] == "use-case"
    assert len(meta["description"]) <= 1024 and len(meta["compatibility"]) <= 500
    for module in ucs["modules"]:
        body = (folder / module["file"]).read_text()
        assert all((folder / script).is_file() for script in module.get("scripts", []))
        load_lines = [line for line in body.splitlines() if re.search(r"\*\*[Ll]oad:?\*\*", line)]
        loaded = set(re.findall(r"`(databricks-[a-z-]+)`", "\n".join(load_lines)))
        declared = {dep["name"] for dep in module.get("base_skills", [])}
        assert loaded == declared, f"{module['id']}: text loads {sorted(loaded)}, ucs.yaml declares {sorted(declared)}"
        row = next((line for line in text.splitlines() if line.startswith(f"| {module['id'][:2]} |")), "")
        assert set(re.findall(r"databricks-[a-z-]+", row)) == declared, f"SKILL.md workflow row for {module['id']}"
    for fact in ucs.get("facts", []):
        assert all((folder / f).is_file() for f in fact["used_in"]), fact["fact"]


@pytest.mark.parametrize("skill", SKILLS)
def test_thresholds_agree(skill: str):
    tree = ast.parse((SKILLS_DIR / skill / "scripts" / "evaluate.py").read_text())
    thresholds = next(
        ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "THRESHOLDS"
    )
    scenarios = yaml.safe_load((SKILLS_DIR / skill / "evals" / "scenarios.yaml").read_text())
    assert thresholds == scenarios["defaults"]["thresholds"]


@pytest.mark.parametrize("skill", SKILLS)
def test_check_base_skills(skill: str, tmp_path: Path):
    ucs = manifest(skill)
    deps = [dep for module in ucs["modules"] for dep in module.get("base_skills", [])] + ucs.get("requires", [])
    for dep in deps:
        meta = "" if dep["version"] == "unversioned" else f'metadata:\n  version: "{dep["version"]}"\n'
        (tmp_path / dep["name"]).mkdir(exist_ok=True)
        (tmp_path / dep["name"] / "SKILL.md").write_text(f"---\nname: {dep['name']}\ndescription: x\n{meta}---\n")
    check = load_script(skill, "check_base_skills")
    cited = sorted({pair for module in ucs["modules"] for pair in check.cited_files((SKILLS_DIR / skill / module["file"]).read_text())})
    for base, ref in cited:
        (tmp_path / base / ref).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / base / ref).write_text("reference\n")
    run_script(skill, "check_base_skills.py", "--skills-dir", str(tmp_path))
    if cited:  # a cited reference file that disappears blocks, like a missing skill
        (tmp_path / cited[0][0] / cited[0][1]).unlink()
        result = run_script(skill, "check_base_skills.py", "--skills-dir", str(tmp_path), check=False)
        assert result.returncode == 1 and f"{cited[0][0]}/{cited[0][1]}" in result.stderr
        (tmp_path / cited[0][0] / cited[0][1]).write_text("reference\n")
    (tmp_path / "databricks-core" / "SKILL.md").unlink()
    result = run_script(skill, "check_base_skills.py", "--skills-dir", str(tmp_path), check=False)
    assert result.returncode == 1 and "databricks-core" in result.stderr
    for dep in ucs.get("requires", []):  # use-case skills this one runs first are checked the same way
        (tmp_path / dep["name"] / "SKILL.md").unlink()
        result = run_script(skill, "check_base_skills.py", "--skills-dir", str(tmp_path), check=False)
        assert f"Use-case skills missing or too old: ['{dep['name']}']" in result.stderr


def test_required_use_case_skills_exist():
    for skill in SKILLS:
        for dep in manifest(skill).get("requires", []):
            assert dep["name"] in SKILLS, f"{skill} requires {dep['name']}, which isn't in skills/"
            pinned, current = (tuple(map(int, str(v).split("."))) for v in (dep["version"], manifest(dep["name"])["version"]))
            assert pinned <= current, f"{skill} requires {dep['name']} {dep['version']}, newer than the repo's copy"


def test_shared_files_are_identical():
    carriers: dict[str, list[str]] = {}
    for skill in SKILLS:
        for name in manifest(skill).get("shared", []):
            carriers.setdefault(name, []).append(skill)
    assert {"scripts/parse.py", "scripts/parse.sql", "scripts/check_base_skills.py", "scripts/write_readme.py"} <= set(carriers)
    assert set(carriers["scripts/write_readme.py"]) == set(SKILLS), "every use-case skill writes its project's README"
    for name, skills in carriers.items():
        copies = {(SKILLS_DIR / skill / name).read_bytes() for skill in skills}
        assert len(copies) == 1, f"{name} differs between {skills}: copy one over the others"


@pytest.mark.parametrize("skill", SKILLS)
def test_workflow_writes_the_project_readme(skill: str):
    """After each module, the workflow updates <workdir>/README.md (tests/test_write_readme.py checks what it writes)."""
    text = (SKILLS_DIR / skill / "SKILL.md").read_text()
    workflow = text.split("\n## Workflow\n", 1)[1].split("\n## ", 1)[0]
    state = manifest(skill)["catalog"]["state_file"]
    assert f"uv run <skill>/scripts/write_readme.py --state <workdir>/{state}" in workflow
    assert re.search(r"(?m)^├── README\.md +# ", workflow), "the working-directory tree lists README.md"
    done = text.split("\n## Done when\n", 1)[1].split("\n## ", 1)[0]
    assert "`<workdir>/README.md`" in done


NOTEBOOK_HEADER = "# Databricks notebook source"
RUN_CELL = ("# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.\n"
            'if __name__ == "__main__":\n'
            '    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)')
JOB_SKILLS = [skill for skill in SKILLS if (SKILLS_DIR / skill / "scripts" / "databricks.yml").is_file()]


def parser_flags(script: Path) -> dict[str, bool]:
    """{dest: required} for each add_argument in the script's build_parser(), read from its source: importing a
    notebook would need its runtime packages (the Databricks SDK, pyspark)."""
    tree = ast.parse(script.read_text())
    build = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser"), None)
    assert build, f"{script.name} has no build_parser()"
    flags = {}
    for call in ast.walk(build):
        if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "add_argument":
            keywords = {k.arg: k.value for k in call.keywords}
            dest = ast.literal_eval(keywords["dest"]) if "dest" in keywords else (
                ast.literal_eval(call.args[0]).lstrip("-").replace("-", "_"))
            flags[dest] = "required" in keywords and ast.literal_eval(keywords["required"]) is True
    return flags


@pytest.mark.parametrize("skill", JOB_SKILLS)
def test_job_tasks_are_notebooks(skill: str):
    """Job and training code runs as notebook tasks, so users can open and edit it in their workspace. App code
    and the agent's local tools stay plain Python."""
    scripts = SKILLS_DIR / skill / "scripts"
    bundle = yaml.safe_load((scripts / "databricks.yml").read_text())  # the optional `# >>>` blocks are comments
    notebooks = set()
    for job_key, job in bundle["resources"]["jobs"].items():
        job_parameters = {p["name"] for p in job.get("parameters") or []}
        for task in job["tasks"]:
            where = f"{job_key}.{task['task_key']}"
            assert not {"spark_python_task", "python_wheel_task"} & set(task), f"{where}: use a notebook_task"
            if "notebook_task" not in task:
                continue
            path = task["notebook_task"]["notebook_path"]
            assert path.startswith("src/"), f"{where}: notebook_path {path} isn't the bundle's copy of scripts/"
            script = scripts / path.removeprefix("src/")
            text = script.read_text()
            assert text.startswith(NOTEBOOK_HEADER), f"{script.name} must start with {NOTEBOOK_HEADER!r}"
            assert text.rstrip().endswith(RUN_CELL), f"{script.name} must end with the guarded run cell"
            notebooks.add(script.name)
            flags = parser_flags(script)
            given = set(task["notebook_task"].get("base_parameters") or {})
            extra = sorted(given - set(flags))
            assert not extra, f"{where}: base_parameters {extra} aren't flags of {script.name}"
            missing = {dest for dest, required in flags.items() if required} - given - job_parameters
            assert not missing, f"{where}: required settings {sorted(missing)} aren't passed"
    for script in scripts.glob("*.py"):
        if script.name not in notebooks and not script.read_text().startswith(NOTEBOOK_HEADER):
            assert "dbutils.widgets" not in script.read_text(), f"{script.name} reads widgets but isn't a notebook"


@pytest.mark.parametrize("skill", [*SKILLS, "forge", "forge-smith"])
def test_skill_folder_limits(skill: str):
    folder = SKILLS_DIR / skill
    entries = list(folder.rglob("*"))
    assert len(entries) <= 50
    assert max(len(p.relative_to(folder).parts) for p in entries) <= 3
    assert not [p for p in entries if p.name in ("__pycache__", ".ruff_cache", ".pytest_cache")]
    for path in entries:
        if path.is_file():
            path.read_text(encoding="utf-8")  # text files only


@pytest.mark.parametrize("skill", JOB_SKILLS)
def test_notebook_helpers_match_the_shared_parse_stage(skill: str):
    """Each job notebook carries its own copy of the parse stage's widget helpers (skills can't share imports);
    a copy that drifts would read a task's settings differently from the others."""
    def functions(path: Path) -> dict[str, str]:
        text = path.read_text()
        return {n.name: ast.get_source_segment(text, n) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}

    reference = functions(SKILLS_DIR / "forge-structured-doc-extraction" / "scripts" / "parse.py")
    for script in sorted((SKILLS_DIR / skill / "scripts").glob("*.py")):
        if script.read_text().startswith(NOTEBOOK_HEADER):
            for name, source in functions(script).items():
                if name in ("notebook_argv", "script_dir"):
                    assert source == reference[name], f"{skill}/{script.name}: {name}() differs from parse.py's; copy it over"
