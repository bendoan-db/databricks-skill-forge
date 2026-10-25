"""Checks for skills/forge-unstructured-doc-extraction that need no workspace (cross-skill checks are in test_skills.py)."""

import argparse
import json
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS_DIR, load_script, run_script

SKILL = "forge-unstructured-doc-extraction"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
prep_chunks = load_script(SKILL, "prep_chunks")
evaluate = load_script(SKILL, "evaluate")

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
    "index_name": "sec_filings_index",
    "continuous_updates": True,
    "embedding_model": "databricks-qwen3-embedding-0-6b",
}


def render(*args: str, check: bool = True):
    return run_script(SKILL, "render.py", *args, check=check)


def bundle_yaml(state_file: Path) -> dict:
    return yaml.safe_load((state_file.parent / "filings-index" / "databricks.yml").read_text())


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    path = tmp_path / "doc-index.json"
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


def test_render_round_trips_values(state_file: Path):
    render("--state", str(state_file))
    parsed = bundle_yaml(state_file)
    variables = parsed["variables"]
    assert json.loads(variables["metadata_schema"]["default"]) == SCHEMA
    assert variables["metadata_columns"]["default"] == "company,document_type,fiscal_year"
    assert variables["split_pages"]["default"] == "none" and variables["chart_insights"]["default"] == "false"
    job = parsed["resources"]["jobs"]["doc_index"]
    assert [t["task_key"] for t in job["tasks"]] == ["parse", "chunk", "sync_index"]
    assert job["trigger"]["file_arrival"]["url"] == "${var.volume_path}/"
    sync = job["tasks"][2]["spark_python_task"]["parameters"]
    assert sync[sync.index("--index-name") + 1] == "${var.catalog}.${var.schema}.${var.index_name}"
    assert variables["index_name"]["default"] == "sec_filings_index"
    assert sorted(p.name for p in (state_file.parent / "filings-index" / "src").iterdir()) == sorted(
        ["parse.py", "parse.sql", "prep_chunks.py", "prep_chunks.sql", "sync_index.py"]
    )


def test_render_chart_insights_needs_page_images(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "chart_insights": True}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "page_images_path" in result.stderr
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "page_images_path": "/Volumes/main/docs/img"}))
    render("--state", str(state_file))
    variables = bundle_yaml(state_file)["variables"]
    assert variables["chart_insights"]["default"] == "true"
    assert variables["chart_model"]["default"] == ""  # prep_chunks.py falls back to Claude Sonnet 5.5


def test_render_protects_hand_edits(state_file: Path):
    render("--state", str(state_file))
    target = state_file.parent / "filings-index" / "databricks.yml"
    target.write_text(target.read_text().replace("triggers_seconds: 300", "triggers_seconds: 600"))
    assert render("--state", str(state_file), check=False).returncode == 1
    render("--state", str(state_file), "--force")
    assert "triggers_seconds: 600" not in target.read_text()


def test_render_schedule_replaces_file_arrival(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "schedule_cron": "0 0 6 * * ?"}))
    render("--state", str(state_file))
    job = bundle_yaml(state_file)["resources"]["jobs"]["doc_index"]
    assert "trigger" not in job and job["schedule"]["quartz_cron_expression"] == "0 0 6 * * ?"


def test_render_without_continuous_updates_has_no_trigger(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "continuous_updates": False}))
    render("--state", str(state_file))
    job = bundle_yaml(state_file)["resources"]["jobs"]["doc_index"]
    assert "trigger" not in job and "schedule" not in job
    state_file.write_text(json.dumps({**STATE, "continuous_updates": False, "schedule_cron": "0 0 6 * * ?"}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "schedule_cron needs continuous_updates" in result.stderr


@pytest.mark.parametrize("missing", ["vs_endpoint", "index_name", "continuous_updates"])
def test_render_needs_the_requirements_asked_first(state_file: Path, missing: str):
    state_file.write_text(json.dumps({key: value for key, value in STATE.items() if key != missing}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and missing in result.stderr


def test_render_rejects_qualified_index_names(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "index_name": "main.docs.sec_filings_index"}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "index_name" in result.stderr


def test_render_rejects_long_names(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "name": "x" * 41}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "40 characters" in result.stderr


def test_retrieval_hits_by_file_and_page():
    rows = [
        {"question": "q1", "file_name": "a.pdf", "pages": [3]},
        {"question": "q2", "file_name": "b.pdf", "page": 7},
        {"question": "q3", "file_name": "c.pdf"},
    ]
    results = [
        [{"file_name": "x.pdf", "page_numbers": [3]}, {"file_name": "a.pdf", "page_numbers": "[2, 3]"}],  # rank 2, page hit
        [{"file_name": "b.pdf", "page_numbers": [1]}],                                                    # rank 1, wrong page
        [{"file_name": "x.pdf", "page_numbers": [1]}],                                                    # miss
    ]
    result = evaluate.score(rows, results)
    assert result["metrics"] == {"doc_hit_rate": 2 / 3, "doc_mrr": (0.5 + 1.0) / 3, "page_hit_rate": 0.5}
    assert [q["rank"] for q in result["questions"]] == [2, 1, None]
    assert evaluate.threshold_failures(result["metrics"]) == ["doc_hit_rate=0.667 < 0.9", "page_hit_rate=0.500 < 0.8"]


def test_page_hit_rate_needs_labeled_pages():
    result = evaluate.score([{"question": "q", "file_name": "a.pdf"}], [[{"file_name": "a.pdf", "page_numbers": [1]}]])
    assert result["metrics"] == {"doc_hit_rate": 1.0, "doc_mrr": 1.0}


def test_synthetic_questions_are_labeled_from_their_chunk():
    chunks = {"c1": {"chunk_id": "c1", "file_name": "a.pdf", "pages": [4, 5]}}
    generated = [
        {"inputs": {"messages": [{"role": "user", "content": "What was revenue?"}]},
         "expectations": {"expected_facts": ("revenue was $5M",),
                          "expected_retrieved_context": [{"doc_uri": "c1", "content": "..."}]}},
        {"inputs": {"messages": [{"role": "user", "content": "Orphan?"}]},
         "expectations": {"expected_facts": [], "expected_retrieved_context": {"doc_uri": "unknown"}}},
    ]
    rows = evaluate.label_synthetic(generated, chunks)
    assert rows[0] == {"question": "What was revenue?", "expected_facts": ["revenue was $5M"],
                       "file_name": "a.pdf", "pages": [4, 5], "chunk_id": "c1"}
    assert rows[1] == {"question": "Orphan?", "expected_facts": []}  # kept for forge-doc-qa-agent, skipped by retrieval scoring
