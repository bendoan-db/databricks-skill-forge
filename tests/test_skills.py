"""Checks every use-case skill must pass: manifest vs. module text, versions, thresholds, shared files, and folder limits.

The forge router has no ucs.yaml, so only the folder limits apply to it here; tests/test_forge.py checks the rest.

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
    assert {"scripts/parse.py", "scripts/parse.sql", "scripts/check_base_skills.py"} <= set(carriers)
    for name, skills in carriers.items():
        copies = {(SKILLS_DIR / skill / name).read_bytes() for skill in skills}
        assert len(copies) == 1, f"{name} differs between {skills}: copy one over the others"


@pytest.mark.parametrize("skill", [*SKILLS, "forge"])
def test_skill_folder_limits(skill: str):
    folder = SKILLS_DIR / skill
    entries = list(folder.rglob("*"))
    assert len(entries) <= 50
    assert max(len(p.relative_to(folder).parts) for p in entries) <= 3
    assert not [p for p in entries if p.name in ("__pycache__", ".ruff_cache", ".pytest_cache")]
    for path in entries:
        if path.is_file():
            path.read_text(encoding="utf-8")  # text files only
