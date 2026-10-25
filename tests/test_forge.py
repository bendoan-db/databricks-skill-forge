"""Checks for the forge router skill (skills/forge) that need no workspace."""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR

FORGE = SKILLS_DIR / "forge"
TEXT = (FORGE / "SKILL.md").read_text()
META = yaml.safe_load(TEXT.split("---")[1])
MANIFEST = yaml.safe_load((FORGE / "forge.yaml").read_text())
ROUTING = yaml.safe_load((FORGE / MANIFEST["routing"]).read_text())
EXPECT_KINDS = {"handoff", "ask", "point_to"}


def catalog_tool():
    path = SKILLS_DIR.parent / "tools" / "build_forge_catalog.py"
    spec = importlib.util.spec_from_file_location("build_forge_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(version).split("."))


def section(title: str) -> str:
    return TEXT.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def test_frontmatter():
    assert META["name"] == "forge" == MANIFEST["name"]
    assert META["metadata"]["forge_kind"] == "router"
    assert META["metadata"]["version"] == MANIFEST["version"]
    assert len(META["description"]) <= 1024 and len(META["compatibility"]) <= 500
    assert not (FORGE / "ucs.yaml").exists()  # keeps the router out of the use-case checks


def test_catalog_is_up_to_date():
    tool = catalog_tool()
    assert tool.update(TEXT, tool.render()) == TEXT, "run `uv run tools/build_forge_catalog.py`"


def test_every_use_case_skill_is_routed():
    names = [entry["name"] for entry in MANIFEST["use_cases"]]
    assert sorted(names) == SKILLS, "forge.yaml use_cases must list every skill folder with a ucs.yaml"
    for entry in MANIFEST["use_cases"]:
        version = yaml.safe_load((SKILLS_DIR / entry["name"] / "ucs.yaml").read_text())["version"]
        assert version_key(entry["min_version"]) <= version_key(version), f"{entry['name']} min_version is newer than {version}"


@pytest.mark.parametrize("skill", SKILLS)
def test_catalog_entries(skill: str):
    folder = SKILLS_DIR / skill
    catalog = yaml.safe_load((folder / "ucs.yaml").read_text())["catalog"]
    assert catalog["state_file"] in (folder / "SKILL.md").read_text()
    named = set(re.findall(r"forge-[a-z-]+[a-z]", f"{catalog['not_for']} {catalog['choose_when']}")) | set(catalog["next"])
    assert named <= set(SKILLS), f"{skill} catalog names skills that don't exist: {sorted(named - set(SKILLS))}"
    assert "forge" in (folder / "SKILL.md").read_text().split("## Conventions", 1)[1].split("\n## ", 1)[0]


def test_routing_cases():
    fallbacks = set(re.findall(r"`(databricks-[a-z-]+)`", section("5. No matching use case")))
    handed_off = set()
    for case in ROUTING["cases"]:
        expect = case["expect"]
        assert len(EXPECT_KINDS & set(expect)) == 1, case["prompt"]
        if "handoff" in expect:
            assert expect["handoff"] in SKILLS, case["prompt"]
            handed_off.add(expect["handoff"])
        if "point_to" in expect:
            assert expect["point_to"] in fallbacks, f"{expect['point_to']} isn't in the No matching use case table"
        if "offers_next" in expect:
            assert expect["offers_next"] in SKILLS
        if "ask" in expect:
            assert expect["ask"] in {"use_case_menu", "end_result"}
    assert handed_off == set(SKILLS), "every use-case skill needs at least one routing case"


def test_resume_cases_use_real_state_files():
    state_files = {yaml.safe_load((SKILLS_DIR / s / "ucs.yaml").read_text())["catalog"]["state_file"] for s in SKILLS}
    for case in ROUTING["cases"]:
        assert set(case.get("workdir_files", [])) <= state_files, case["prompt"]


def test_deprecated_products_are_redirected():
    guardrails = section("Guardrails")
    assert "Knowledge Assistant" in guardrails and "`forge-doc-qa-agent`" in guardrails
    assert "Information Extraction" in guardrails and "`forge-structured-doc-extraction`" in guardrails


def test_use_case_skills_no_longer_route_themselves():
    for skill in SKILLS:
        scenarios = yaml.safe_load((SKILLS_DIR / skill / "evals" / "scenarios.yaml").read_text())
        assert "routing" not in scenarios, f"{skill}: routing evals live in skills/forge/evals/routing.yaml"
        assert scenarios["defaults"]["harness"]["entry"] == "forge"


def test_tool_is_not_published():
    assert not any(Path(p).suffix == ".py" for p in FORGE.rglob("*")), "forge needs no scripts; tools/ holds the generator"
