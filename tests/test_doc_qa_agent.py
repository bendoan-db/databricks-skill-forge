"""Checks for use-case-skills/doc-qa-agent that need no workspace (cross-skill checks are in test_skills.py)."""

import argparse
import ast
import json
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS_DIR, load_script, run_script

SKILL = "doc-qa-agent"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
prep_chunks = load_script(SKILL, "prep_chunks")

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
    "chat_model": "system.ai.claude-sonnet-5-5",
    "vs_endpoint": "filings-vs",
    "embedding_model": "databricks-qwen3-embedding-0-6b",
    "filter_values": {"company": ["APPLE", "NETFLIX"], "fiscal_year": None},
    "corpus_description": "SEC filings for two US public companies",
}


def render(*args: str, check: bool = True):
    return run_script(SKILL, "render.py", *args, check=check)


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    path = tmp_path / "doc-qa.json"
    path.write_text(json.dumps(STATE))
    return path


def test_every_sql_placeholder_has_a_value():
    args = argparse.Namespace(
        catalog="main", schema="docs", prefix="filings", metadata_schema=json.dumps(SCHEMA),
        chart_model="", chart_prompt="It's a chart",
    )
    params = prep_chunks.build_params(args, prep_chunks.metadata_columns(args.metadata_schema))
    for sql in prep_chunks.load_sections(SCRIPTS / "prep_chunks.sql").values():
        prep_chunks.render(sql, params)  # KeyError on a placeholder build_params doesn't fill
    assert params["chart_model"] == "'system.ai.claude-sonnet-5-5'"
    assert params["chart_prompt"] == "'It\\'s a chart'"


def test_metadata_schema_rules():
    assert prep_chunks.metadata_columns('["company"]') == {"company": "STRING"}
    for bad in ('{"Company": {}}', '{"chunk_id": {}}', '{"tags": {"type": "array"}}'):
        with pytest.raises(ValueError):
            prep_chunks.metadata_columns(bad)


def test_crop_box_pads_clamps_and_drops_tiny_figures():
    assert prep_chunks.crop_box([100, 120, 300, 260], (1000, 1000)) == (92, 112, 308, 268)
    assert prep_chunks.crop_box([990, 0, 1200, 200], (1000, 1000)) is None  # 10 px wide after clamping
    assert prep_chunks.crop_box([300, 260, 100, 120], (1000, 1000)) == (92, 112, 308, 268)  # unordered coords
    assert prep_chunks.crop_box(None, (1000, 1000)) is None


def test_chart_insights_append_to_figure_descriptions_only():
    parsed = {"document": {"elements": [
        {"id": 3, "type": "figure", "description": "Bar chart of revenue"},
        {"id": 4, "type": "text", "content": "Revenue grew"},
    ]}}
    insights = [{"element_id": 3, "insight": "| Year | Revenue |"}, {"element_id": 4, "insight": "ignored"}]
    elements = prep_chunks.add_chart_insights(parsed, insights)["document"]["elements"]
    assert elements[0]["description"] == "Bar chart of revenue\n\nChart reading:\n| Year | Revenue |"
    assert "description" not in elements[1]


def test_render_ingest_round_trips_values(state_file: Path):
    render("ingest", "--state", str(state_file))
    bundle = state_file.parent / "filings-ingest"
    parsed = yaml.safe_load((bundle / "databricks.yml").read_text())
    variables = parsed["variables"]
    assert json.loads(variables["metadata_schema"]["default"]) == SCHEMA
    assert variables["metadata_columns"]["default"] == "company,document_type,fiscal_year"
    assert variables["split_pages"]["default"] == "none" and variables["chart_insights"]["default"] == "false"
    tasks = parsed["resources"]["jobs"]["doc_qa_ingest"]["tasks"]
    assert [t["task_key"] for t in tasks] == ["parse", "chunk", "sync_index"]
    assert sorted(p.name for p in (bundle / "src").iterdir()) == sorted(
        ["parse.py", "parse.sql", "prep_chunks.py", "prep_chunks.sql", "sync_index.py"]
    )


def test_render_chart_insights_needs_page_images(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "chart_insights": True}))
    result = render("ingest", "--state", str(state_file), check=False)
    assert result.returncode == 1 and "page_images_path" in result.stderr
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "page_images_path": "/Volumes/main/docs/img"}))
    render("ingest", "--state", str(state_file))
    variables = yaml.safe_load((state_file.parent / "filings-ingest" / "databricks.yml").read_text())["variables"]
    assert variables["chart_insights"]["default"] == "true"
    assert variables["chart_model"]["default"] == STATE["chat_model"]


def test_render_protects_hand_edits(state_file: Path):
    render("ingest", "--state", str(state_file))
    target = state_file.parent / "filings-ingest" / "databricks.yml"
    target.write_text(target.read_text().replace("triggers_seconds: 300", "triggers_seconds: 600"))
    assert render("ingest", "--state", str(state_file), check=False).returncode == 1
    render("ingest", "--state", str(state_file), "--force")
    assert "triggers_seconds: 600" not in target.read_text()


def test_render_schedule_replaces_file_arrival(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "schedule_cron": "0 0 6 * * ?"}))
    render("ingest", "--state", str(state_file))
    job = yaml.safe_load((state_file.parent / "filings-ingest" / "databricks.yml").read_text())["resources"]["jobs"]["doc_qa_ingest"]
    assert "trigger" not in job and job["schedule"]["quartz_cron_expression"] == "0 0 6 * * ?"


def test_render_agent_fills_constants(state_file: Path):
    (state_file.parent / "filings" / "agent").mkdir(parents=True)
    render("agent", "--state", str(state_file))
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
    result = render("agent", "--state", str(state_file), check=False)
    assert result.returncode == 1 and "ticker" in result.stderr
