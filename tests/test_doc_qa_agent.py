"""Checks for use-case-skills/doc-qa-agent that need no workspace.

    uv run --no-project --with pytest --with pyyaml pytest -q -p no:cacheprovider databricks-skill-forge/tests
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.dont_write_bytecode = True  # importing ingest must not leave __pycache__ in the published skill folder
SKILL = Path(__file__).resolve().parents[1] / "use-case-skills" / "doc-qa-agent"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ingest  # noqa: E402

SCHEMA = {  # module 02's example, apostrophe included
    "company": {"type": "string", "description": "Issuer's common name, e.g. 'JOHNSON & JOHNSON' -> 'JOHNSONJOHNSON'"},
    "document_type": {"type": "enum", "labels": ["10K", "10Q", "8K", "EARNINGS"]},
    "fiscal_year": {"type": "integer"},
}
STATE = {
    "profile": "DEFAULT",
    "volume_path": "/Volumes/main/docs/filings",
    "catalog": "main",
    "schema": "docs",
    "prefix": "filings",
    "name": "filings",
    "metadata_schema": SCHEMA,
    "vs_endpoint": "filings-vs",
    "embedding_model": "databricks-qwen3-embedding-0-6b",
    "filter_values": {"company": ["APPLE", "NETFLIX"], "fiscal_year": None},
    "corpus_description": "SEC filings for two US public companies",
}


def run(script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *args], capture_output=True, text=True, check=False
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    path = tmp_path / "doc-qa.json"
    path.write_text(json.dumps(STATE))
    return path


def test_every_sql_placeholder_has_a_value():
    args = argparse.Namespace(
        catalog="main", schema="docs", prefix="filings", description_element_types="*",
        page_images_path="/Volumes/main/docs/images", metadata_schema=json.dumps(SCHEMA),
    )
    params = ingest.build_params(args, ingest.metadata_columns(args.metadata_schema))
    for name in ("parse_documents.sql", "prep_chunks.sql"):
        for sql in ingest.load_sections(SCRIPTS / name).values():
            ingest.render(sql, params)  # KeyError on a placeholder build_params doesn't fill


def test_metadata_schema_rules():
    assert ingest.metadata_columns('["company"]') == {"company": "STRING"}
    for bad in ('{"Company": {}}', '{"chunk_id": {}}', '{"tags": {"type": "array"}}'):
        with pytest.raises(ValueError):
            ingest.metadata_columns(bad)


def test_render_ingest_round_trips_values(state_file: Path):
    run("render.py", "ingest", "--state", str(state_file))
    bundle = state_file.parent / "filings-ingest"
    variables = yaml.safe_load((bundle / "databricks.yml").read_text())["variables"]
    assert json.loads(variables["metadata_schema"]["default"]) == SCHEMA
    assert variables["metadata_columns"]["default"] == "company,document_type,fiscal_year"
    assert sorted(p.name for p in (bundle / "src").iterdir()) == sorted(
        ["ingest.py", "sync_index.py", "parse_documents.sql", "prep_chunks.sql"]
    )


def test_render_protects_hand_edits(state_file: Path):
    run("render.py", "ingest", "--state", str(state_file))
    target = state_file.parent / "filings-ingest" / "databricks.yml"
    target.write_text(target.read_text().replace("triggers_seconds: 300", "triggers_seconds: 600"))
    assert run("render.py", "ingest", "--state", str(state_file), check=False).returncode == 1
    run("render.py", "ingest", "--state", str(state_file), "--force")
    assert "triggers_seconds: 600" not in target.read_text()


def test_render_agent_fills_constants(state_file: Path):
    (state_file.parent / "filings" / "agent").mkdir(parents=True)
    run("render.py", "agent", "--state", str(state_file))
    tree = ast.parse((state_file.parent / "filings" / "agent" / "retrieval.py").read_text())
    constants = {
        (n.targets[0] if isinstance(n, ast.Assign) else n.target).id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
    }
    assert constants["INDEX_NAME"] == "main.docs.filings_chunks_index"
    assert constants["FILTER_VALUES"] == STATE["filter_values"]


def test_render_rejects_filter_on_unknown_column(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "filter_values": {"ticker": ["AAPL"]}}))
    (state_file.parent / "filings" / "agent").mkdir(parents=True)
    result = run("render.py", "agent", "--state", str(state_file), check=False)
    assert result.returncode == 1 and "ticker" in result.stderr


def test_check_base_skills(tmp_path: Path):
    ucs = yaml.safe_load((SKILL / "ucs.yaml").read_text())
    for module in ucs["modules"]:
        for dep in module.get("base_skills", []):
            meta = "" if dep["version"] == "unversioned" else f'metadata:\n  version: "{dep["version"]}"\n'
            (tmp_path / dep["name"]).mkdir(exist_ok=True)
            (tmp_path / dep["name"] / "SKILL.md").write_text(f"---\nname: {dep['name']}\ndescription: x\n{meta}---\n")
    run("check_base_skills.py", "--skills-dir", str(tmp_path))
    (tmp_path / "databricks-core" / "SKILL.md").unlink()
    result = run("check_base_skills.py", "--skills-dir", str(tmp_path), check=False)
    assert result.returncode == 1 and "databricks-core" in result.stderr


def test_manifest_matches_skill_text():
    text = (SKILL / "SKILL.md").read_text()
    meta = yaml.safe_load(text.split("---")[1])
    ucs = yaml.safe_load((SKILL / "ucs.yaml").read_text())
    assert meta["name"] == SKILL.name == ucs["name"]
    assert meta["metadata"]["version"] == ucs["version"]
    assert len(meta["description"]) <= 1024 and len(meta["compatibility"]) <= 500
    for module in ucs["modules"]:
        body = (SKILL / module["file"]).read_text()
        assert all((SKILL / script).is_file() for script in module.get("scripts", []))
        load_lines = [line for line in body.splitlines() if re.search(r"\*\*[Ll]oad:?\*\*", line)]
        loaded = set(re.findall(r"`(databricks-[a-z-]+)`", "\n".join(load_lines)))
        declared = {dep["name"] for dep in module.get("base_skills", [])}
        assert loaded == declared, f"{module['id']}: text loads {sorted(loaded)}, ucs.yaml declares {sorted(declared)}"


def test_thresholds_agree():
    tree = ast.parse((SCRIPTS / "evaluate.py").read_text())
    thresholds = next(
        ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "THRESHOLDS"
    )
    scenarios = yaml.safe_load((SKILL / "evals" / "scenarios.yaml").read_text())
    assert thresholds == scenarios["defaults"]["thresholds"]


def test_skill_folder_limits():
    entries = list(SKILL.rglob("*"))
    assert len(entries) <= 50
    assert max(len(p.relative_to(SKILL).parts) for p in entries) <= 3
    assert not [p for p in entries if p.name in ("__pycache__", ".ruff_cache", ".pytest_cache")]
    for path in entries:
        if path.is_file():
            path.read_text(encoding="utf-8")  # text files only
