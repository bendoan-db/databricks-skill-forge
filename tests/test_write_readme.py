"""Checks for the shared write_readme.py, which writes the README of each project a Forge use case builds. No workspace.

Every use-case skill carries an identical copy (tests/test_skills.py), so these run the copies in skills/, where the
other use cases are their siblings.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR, load_script, run_script

INDEX, QA = "forge-unstructured-doc-extraction", "forge-doc-qa-agent"


def manifest(skill: str) -> dict:
    return yaml.safe_load((SKILLS_DIR / skill / "ucs.yaml").read_text())


def write(skill: str, state: Path, *args: str, check: bool = True):
    result = run_script(skill, "write_readme.py", "--state", str(state), *args, check=check)
    return json.loads(result.stdout) if result.returncode == 0 else result


def index_state(folder: Path, **extra) -> Path:
    path = folder / "doc-index.json"
    path.write_text(json.dumps({
        "profile": "DEFAULT", "volume_path": "/Volumes/main/sec/filings", "catalog": "main", "schema": "sec",
        "prefix": "filings", "name": "sec-filings", "vs_endpoint": "forge-vs", "index_name": "filings_chunks_index",
        "continuous_updates": True, "metadata_schema": {"type": "object", "properties": {"company": {}, "year": {}}},
        "completed": ["00-preflight", "01-ingest-parse"], **extra}))
    return path


@pytest.mark.parametrize("skill", SKILLS)
def test_every_skill_writes_its_project_readme(skill: str, tmp_path: Path):
    ucs = manifest(skill)
    state = tmp_path / ucs["catalog"]["state_file"]
    state.write_text(json.dumps({"name": "demo", "profile": "DEFAULT", "completed": [ucs["modules"][0]["id"]]}))
    summary = write(skill, state)
    readme = tmp_path / "README.md"
    assert summary["written"] == [str(readme.resolve())] and summary["covers"] == [skill]
    text = readme.read_text()
    assert re.match(r"<!-- Written by Forge's write_readme\.py from .*\(sha256:[0-9a-f]{12}\)", text)
    assert f"\n# {ucs['catalog']['title']}: `demo`\n" in text and f"`{skill}` {ucs['version']}" in text
    numbers = [m["id"][:2] for m in ucs["modules"]]
    for number, status in zip(numbers, ["done", "next", *["to do"] * len(numbers)]):
        assert re.search(rf"(?m)^#### {number} · .+ \({status}\)$", text), f"module {number} isn't {status}"
    assert "\n## Notes\n\n<!-- forge:notes -->\n" in text and text.endswith("<!-- /forge:notes -->\n")
    assert "<workdir>" not in text  # the folder is the README's own
    assert write(skill, state)["unchanged"] == [str(readme.resolve())]  # same state, same README


@pytest.mark.parametrize("skill", SKILLS)
def test_readme_reads_every_module(skill: str):
    """The README is built from each module's heading, purpose, and steps, and the SKILL.md Workflow checklist and
    table: a skill whose text loses one of those shapes would write a README with gaps."""
    script = load_script(skill, "write_readme")
    loaded = script.load_skill(SKILLS_DIR / skill)
    assert loaded["tree"].startswith("<workdir>/") and "README.md" in loaded["tree"]
    for entry in manifest(skill)["modules"]:
        module = script.module_info(loaded, entry)
        assert module["title"] != entry["id"], f"{entry['file']}: no `# NN · Title` heading"
        assert module["purpose"] and module["steps"], f"{entry['file']}: no purpose line or steps"
        assert script.CRITERION.match(module["criterion"]), f"SKILL.md checklist has no line for {entry['id']}"
        assert module["produces"], f"SKILL.md Workflow table has no row for {entry['id']}"


def test_readme_fills_in_the_project(tmp_path: Path):
    write(INDEX, index_state(tmp_path))
    text = (tmp_path / "README.md").read_text()
    assert "\n# Document search index: `sec-filings`\n" in text
    assert f"| Document search index | `{INDEX}` {manifest(INDEX)['version']} | `doc-index.json` | 2 of 4 modules done | " \
           "02 · Chunk and index |" in text
    assert "table `main.sec.filings_parsed` (one row per file)" in text  # module 01's outputs, filled in
    assert "└── sec-filings-index/  # Declarative Automation Bundle" in text  # the tree, with names filled in
    assert "──►  parse.py ───────► <prefix>_parsed  (ai_parse_document" in text  # the diagram keeps its alignment
    assert "In this project, `<catalog>` is `main`, `<schema>` is `sec`" in text
    assert "| `vs_endpoint` | `forge-vs` | An existing endpoint" in text
    assert "| `metadata_schema` | fields `company`, `year` |" in text
    assert "#### 02 · Chunk and index (next)" in text and "##### Editing the job's notebooks" in text
    assert "each in its own folder, with its own volume folder" in text  # `<workdir>` in prose
    assert "- **Document Q&A agent** (`forge-doc-qa-agent`)" in text  # catalog.next, not built yet
    assert "You told the user" not in text  # Done when keeps the conditions, not the report to the user


def test_readme_covers_every_use_case_in_the_folder(tmp_path: Path):
    index_state(tmp_path)
    qa = tmp_path / "doc-qa.json"
    qa.write_text(json.dumps({"index_state": "doc-index.json", "name": "sec-qa", "chat_model": "system.ai.claude-sonnet-5-5",
                              "completed": ["00-preflight"]}))
    summary = write(QA, qa)
    assert summary["covers"] == [INDEX, QA]  # the index first: the agent requires it
    text = (tmp_path / "README.md").read_text()
    assert "\n# Document Q&A agent: `sec-qa`\n" in text and "## Next steps" not in text
    assert text.index("\n## Document search index\n") < text.index("\n## Document Q&A agent\n")
    assert "├── sec-filings-index/" in text and "├── doc-qa.json" in text and "└── sec-qa/" in text
    assert "sec-qa-index" not in text  # the index's bundle is named for the index, whichever skill lists it
    assert "In this project, `<index_name>` is `filings_chunks_index`, `<name>` is `sec-qa`" in text
    assert write(INDEX, tmp_path / "doc-index.json")["unchanged"]  # the index's copy writes the same README


def test_readme_finds_an_index_in_another_folder(tmp_path: Path):
    (tmp_path / "index").mkdir()
    index_state(tmp_path / "index")
    qa = tmp_path / "agent" / "doc-qa.json"
    qa.parent.mkdir()
    qa.write_text(json.dumps({"index_state": "../index/doc-index.json", "name": "sec-qa", "completed": []}))
    assert write(QA, qa)["covers"] == [INDEX, QA]
    text = (qa.parent / "README.md").read_text()
    tree = text.split("## Files in this folder", 1)[1].split("\n## ", 1)[0]
    assert "`../index/doc-index.json`" in text and "sec-filings-index/" not in tree  # its files aren't here


def test_notes_are_kept_and_hand_edits_win(tmp_path: Path):
    state = index_state(tmp_path)
    readme = tmp_path / "README.md"
    write(INDEX, state)
    readme.write_text(readme.read_text().replace("Add your own notes here.", "Owner: the claims team."))
    assert write(INDEX, state)["unchanged"]  # a Notes edit isn't a conflict
    state.write_text(state.read_text().replace('"01-ingest-parse"]', '"01-ingest-parse", "02-chunk-index"]'))
    assert write(INDEX, state)["written"]
    assert "Owner: the claims team." in readme.read_text() and "#### 03 · Evaluate retrieval (next)" in readme.read_text()

    readme.write_text(readme.read_text().replace("## Status", "## Where we are"))
    assert write(INDEX, state)["kept_edited"] == [str(readme.resolve())]
    assert "## Where we are" in readme.read_text()
    assert write(INDEX, state, "--force")["written"]
    assert "## Where we are" not in readme.read_text() and "Owner: the claims team." in readme.read_text()


def test_a_readme_forge_didnt_write_is_left_alone(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Our repo\n")
    state = index_state(tmp_path)
    summary = write(INDEX, state)
    assert summary["readme"] == str((tmp_path / "FORGE-README.md").resolve()) and "left as is" in summary["note"]
    assert (tmp_path / "README.md").read_text() == "# Our repo\n"
    assert write(INDEX, state)["unchanged"] == [summary["readme"]]
    (tmp_path / "FORGE-README.md").write_text("# Also ours\n")
    result = write(INDEX, state, check=False)
    assert result.returncode == 1 and "Move one aside" in result.stderr


def test_refuses_another_skills_state_file(tmp_path: Path):
    other = tmp_path / "extraction.json"
    other.write_text("{}")
    result = write(INDEX, other, check=False)
    assert result.returncode == 1 and "keeps its settings in doc-index.json" in result.stderr
    result = write(INDEX, tmp_path / "doc-index.json", check=False)
    assert result.returncode == 1 and "Module 00 writes it" in result.stderr
