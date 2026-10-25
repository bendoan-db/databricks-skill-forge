"""Checks for skills/forge-doc-qa-agent that need no workspace (cross-skill checks are in test_skills.py)."""

import ast
import json
from pathlib import Path

import pytest
from conftest import run_script

SKILL = "forge-doc-qa-agent"
INDEX_STATE = {  # the doc-index.json forge-unstructured-doc-extraction writes, after its module 02
    "profile": "DEFAULT",
    "catalog": "main",
    "schema": "docs",
    "prefix": "filings",
    "index_name": "sec_filings_index",
    "name": "filings",
    "metadata_schema": {"company": {"type": "string"}, "document_type": {"type": "enum"}, "fiscal_year": {"type": "integer"}},
    "filter_values": {"company": ["APPLE", "NETFLIX"], "fiscal_year": None},
    "corpus_description": "SEC filings for two US public companies",
    "completed": ["00-preflight", "01-ingest-parse", "02-chunk-index", "03-evaluate"],
}
STATE = {"index_state": "doc-index.json", "name": "filings", "chat_model": "system.ai.claude-sonnet-5-5"}


def render(state_file: Path, *args: str, check: bool = True):
    return run_script(SKILL, "render.py", "--state", str(state_file), *args, check=check)


def write(workdir: Path, state: dict | None = None, index: dict | None = None) -> Path:
    (workdir / "doc-index.json").write_text(json.dumps(INDEX_STATE if index is None else index))
    path = workdir / "doc-qa.json"
    path.write_text(json.dumps(STATE if state is None else state))
    return path


def constants(workdir: Path) -> dict:
    tree = ast.parse((workdir / "filings" / "agent" / "retrieval.py").read_text())
    return {
        (n.targets[0] if isinstance(n, ast.Assign) else n.target).id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
    }


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "filings" / "agent").mkdir(parents=True)
    return tmp_path


def test_render_fills_constants_from_the_index(workdir: Path):
    render(write(workdir))
    found = constants(workdir)
    assert found["INDEX_NAME"] == "main.docs.sec_filings_index"
    assert found["FILTER_VALUES"] == INDEX_STATE["filter_values"]
    assert found["CORPUS_DESCRIPTION"] == INDEX_STATE["corpus_description"]
    assert found["NUM_RESULTS"] == 8


def test_corpus_description_in_doc_qa_json_wins(workdir: Path):
    render(write(workdir, {**STATE, "corpus_description": "Annual and quarterly reports"}))
    assert constants(workdir)["CORPUS_DESCRIPTION"] == "Annual and quarterly reports"


def test_render_rejects_filter_on_unknown_column(workdir: Path):
    result = render(write(workdir, index={**INDEX_STATE, "filter_values": {"ticker": ["AAPL"]}}), check=False)
    assert result.returncode == 1 and "ticker" in result.stderr


def test_render_needs_a_built_index(workdir: Path):
    state_file = write(workdir, index={**INDEX_STATE, "completed": ["00-preflight", "01-ingest-parse"]})
    result = render(state_file, check=False)
    assert result.returncode == 1 and "02-chunk-index" in result.stderr
    (workdir / "doc-index.json").unlink()
    result = render(state_file, check=False)
    assert result.returncode == 1 and "forge-unstructured-doc-extraction" in result.stderr


def test_render_rejects_long_app_names(workdir: Path):
    result = render(write(workdir, {**STATE, "name": "a-name-over-17-chars"}), check=False)
    assert result.returncode == 1 and "17 characters" in result.stderr


def test_render_protects_hand_edits(workdir: Path):
    state_file = write(workdir)
    render(state_file)
    target = workdir / "filings" / "agent" / "retrieval.py"
    target.write_text(target.read_text().replace("NUM_RESULTS = 8", "NUM_RESULTS = 12"))
    assert render(state_file, check=False).returncode == 1
    render(state_file, "--force")
    assert "NUM_RESULTS = 8" in target.read_text()
