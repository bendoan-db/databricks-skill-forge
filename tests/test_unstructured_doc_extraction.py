"""Checks for skills/forge-unstructured-doc-extraction that need no workspace (cross-skill checks are in test_skills.py)."""

import argparse
import ast
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
        chart_model="", chart_prompt="It's a chart", chart_source="vision",
    )
    params = prep_chunks.build_params(args, prep_chunks.metadata_columns(args.metadata_schema))
    for sql in prep_chunks.load_sections(SCRIPTS / "prep_chunks.sql").values():
        prep_chunks.render(sql, params)  # KeyError on a placeholder build_params doesn't fill
    assert params["chart_model"] == "'system.ai.claude-sonnet-5-5'"
    assert params["chart_prompt"] == "'It\\'s a chart'"
    assert params["builtin_only"] == "false"


def test_metadata_schema_rules():
    assert prep_chunks.metadata_columns('["company"]') == {"company": "STRING"}
    for bad in ('{"Company": {}}', '{"chunk_id": {}}', '{"tags": {"type": "array"}}'):
        with pytest.raises(ValueError):
            prep_chunks.metadata_columns(bad)


def test_crop_box_pads_clamps_and_drops_tiny_figures():
    assert prep_chunks.crop_box([100, 120, 300, 260], (1000, 1000)) == (80, 100, 320, 280)
    assert prep_chunks.crop_box([990, 0, 1200, 200], (1000, 1000)) is None  # 30 px wide after clamping
    assert prep_chunks.crop_box([300, 260, 100, 120], (1000, 1000)) == (80, 100, 320, 280)  # unordered coords
    assert prep_chunks.crop_box(None, (1000, 1000)) is None


def test_crop_figure_renders_a_pdf_page_at_the_parsers_200_dpi(tmp_path: Path):
    pytest.importorskip("PIL")
    pytest.importorskip("pypdfium2")
    from pypdf import PdfWriter
    from PIL import Image
    import io

    pdf = tmp_path / "deck.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=72)  # 2 x 1 inches: 400 x 200 px at 200 DPI
    writer.write(pdf)
    image = prep_chunks.crop_figure(None, [100, 50, 300, 150], f"dbfs:{pdf}", 0)
    assert Image.open(io.BytesIO(image)).size == (240, 140)  # the box padded by 20 px on each side
    assert prep_chunks.pdf_page(str(pdf), 0).size == (400, 200)
    assert prep_chunks.crop_figure(None, [100, 50, 300, 150], str(pdf), 3) is None    # no such page
    assert prep_chunks.crop_figure(None, [100, 50, 300, 150], "/Volumes/x/deck.pptx", 0) is None  # not a PDF


# From ai_parse_document v2.0 on a quarterly earnings deck (2026-11-09): the chart's title and units are separate
# elements, and on another page its legend is a footnote beside it, while its chart data names "Segment 1".
PAGE_ONE = [
    {"id": 3, "type": "title", "content": "Advertising Revenue by User Geography", "coord": [281, 123, 3191, 276]},
    {"id": 4, "type": "figure", "content": "Meta", "coord": [4553, 144, 5030, 249]},
    {"id": 5, "type": "text", "content": "In Millions", "coord": [281, 303, 657, 378]},
    {"id": 6, "type": "figure", "content": '{"title": "None", "values": {"US & Canada": {"Q3\'23": 14956}}}',
     "coord": [254, 711, 4876, 2400]},
    {"id": 7, "type": "text", "content": "Our revenue by user geography is geographically apportioned based on our "
     "estimation of the geographic location of our users when they perform a revenue-generating activity.",
     "coord": [329, 2691, 4966, 2796]},
    {"id": 8, "type": "page_number", "content": "2", "coord": [5114, 2853, 5168, 2913]},
]
PAGE_FOUR = [
    {"id": 21, "type": "title", "content": "Expenses as a Percentage of Revenue", "coord": [286, 123, 3000, 276]},
    {"id": 24, "type": "figure", "content": "{}", "coord": [281, 774, 4240, 2361]},
    {"id": 25, "type": "footnote", "content": "General &\nAdministrative\n\nMarketing\n& Sales",
     "coord": [4335, 1608, 4924, 2334]},
]


def test_figure_context_finds_the_title_units_and_adjacent_legend():
    assert prep_chunks.figure_context(PAGE_ONE, 6) == "Advertising Revenue by User Geography\nIn Millions"
    context = prep_chunks.figure_context(PAGE_FOUR, 24)
    assert context.startswith("Expenses as a Percentage of Revenue\n") and "Marketing\n& Sales" in context
    far = [{**PAGE_FOUR[2], "coord": [4600, 1608, 4924, 2334]}]  # 360 px from the chart: not its legend
    assert prep_chunks.figure_context([PAGE_FOUR[1], *far], 24) == ""
    assert prep_chunks.figure_context(PAGE_ONE, 99) == ""


def test_chart_table_renders_the_parsers_chart_data():
    multi = json.dumps({"title": "None", "values": {"US & Canada": {"Q3'23": 14956, "Q4'23": 17784},
                                                    "Europe": {"Q3'23": 7721, "Q4'23": 9159}}, "additional_info": "None"})
    assert prep_chunks.chart_table(multi) == (
        "| Series | Q3'23 | Q4'23 |\n|---|---|---|\n| US & Canada | 14956 | 17784 |\n| Europe | 7721 | 9159 |")
    single = json.dumps({"title": "Revenue", "values": {"Q3'23": 11583, "Q3'25*": None}})
    assert prep_chunks.chart_table(single) == (
        "title: Revenue\n| Category | Value |\n|---|---|\n| Q3'23 | 11583 |\n| Q3'25* |  |")
    for not_chart in ("Meta", "", None, "{}", '{"values": []}', "[1, 2]"):
        assert prep_chunks.chart_table(not_chart) is None


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
    assert "environments" not in job and not any("environment_key" in t for t in job["tasks"])  # notebooks %pip
    sync = job["tasks"][2]["notebook_task"]
    assert sync["notebook_path"] == "src/sync_index.py"
    assert sync["base_parameters"]["index_name"] == "${var.catalog}.${var.schema}.${var.index_name}"
    assert variables["index_name"]["default"] == "sec_filings_index"
    src = state_file.parent / "filings-index" / "src"
    assert sorted(p.name for p in src.iterdir()) == sorted(
        ["parse.py", "parse.sql", "prep_chunks.py", "prep_chunks.sql", "sync_index.py"]
    )
    notebook = (src / "prep_chunks.py").read_text().splitlines()
    assert notebook[0] == "# Databricks notebook source" and notebook[1].startswith("# Copied from " + SKILL)


def parser_of(script: str) -> argparse.ArgumentParser:
    """A notebook's argument parser. sync_index.py imports databricks-sdk, which these checks don't install, so
    its build_parser is compiled on its own."""
    if script != "sync_index":
        return load_script(SKILL, script).build_parser()
    tree = ast.parse((SCRIPTS / "sync_index.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser")
    namespace = {"argparse": argparse}
    exec(compile(ast.Module([function], []), "sync_index.py", "exec"), namespace)  # noqa: S102
    return namespace["build_parser"]()


@pytest.mark.parametrize("task, script", [("parse", "parse"), ("chunk", "prep_chunks"), ("sync_index", "sync_index")])
def test_task_parameters_match_the_notebooks_flags(state_file: Path, task: str, script: str):
    """Each base_parameters key is a widget the notebook reads, and every required one is passed."""
    render("--state", str(state_file))
    job = bundle_yaml(state_file)["resources"]["jobs"]["doc_index"]
    params = next(t for t in job["tasks"] if t["task_key"] == task)["notebook_task"]["base_parameters"]
    jobs_params = {p["name"] for p in job["parameters"]}  # pushed down to every notebook task as widgets
    actions = [a for a in parser_of(script)._actions if a.option_strings and a.dest != "help"]
    assert set(params) <= {a.dest for a in actions}
    assert {a.dest for a in actions if a.required} <= set(params) | jobs_params


def test_render_chart_insights_needs_descriptions_and_page_images_only_for_split_pages(state_file: Path):
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "description_element_types": ""}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "description_element_types" in result.stderr
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "split_pages": True}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "page_images_path" in result.stderr
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "chart_source": "ocr"}))
    result = render("--state", str(state_file), check=False)
    assert result.returncode == 1 and "chart_source" in result.stderr
    state_file.write_text(json.dumps({**STATE, "chart_insights": True}))  # PDF pages are rendered for crops
    render("--state", str(state_file))
    variables = bundle_yaml(state_file)["variables"]
    assert variables["chart_insights"]["default"] == "true" and variables["chart_source"]["default"] == "vision"
    assert variables["chart_model"]["default"] == ""  # prep_chunks.py falls back to Claude Sonnet 5.5
    state_file.write_text(json.dumps({**STATE, "chart_insights": True, "split_pages": True, "chart_source": "builtin"}))
    render("--state", str(state_file))  # the parser's chart data needs no page images
    assert bundle_yaml(state_file)["variables"]["chart_source"]["default"] == "builtin"


def test_render_protects_hand_edits(state_file: Path):
    render("--state", str(state_file))
    target = state_file.parent / "filings-index" / "databricks.yml"
    target.write_text(target.read_text().replace("triggers_seconds: 300", "triggers_seconds: 600"))
    assert render("--state", str(state_file), check=False).returncode == 1
    render("--state", str(state_file), "--force")
    assert "triggers_seconds: 600" not in target.read_text()


def test_render_keeps_an_edited_notebook_copy(state_file: Path):
    render("--state", str(state_file))
    copy = state_file.parent / "filings-index" / "src" / "prep_chunks.py"
    copy.write_text(copy.read_text().replace("MIN_CROP_PIXELS = 32", "MIN_CROP_PIXELS = 64"))  # edited in the workspace
    summary = json.loads(render("--state", str(state_file)).stdout)
    assert summary["kept_edited"] == [str(copy)] and "MIN_CROP_PIXELS = 64" in copy.read_text()
    render("--state", str(state_file), "--force")
    assert "MIN_CROP_PIXELS = 32" in copy.read_text()


def test_render_replaces_an_unmarked_copy_from_an_older_render(state_file: Path):
    copy = state_file.parent / "filings-index" / "src" / "sync_index.py"
    copy.parent.mkdir(parents=True)
    copy.write_text('"""An older copy, without the checksum line."""\n')
    summary = json.loads(render("--state", str(state_file)).stdout)
    assert str(copy) in summary["written"] and summary["kept_edited"] == []
    assert copy.read_text().startswith("# Databricks notebook source\n# Copied from")


def test_prep_chunks_reads_its_settings_from_widgets():
    """As a job's notebook task, prep_chunks.py reads its settings from widgets named like its flags."""
    values = {"catalog": "main", "schema": "docs", "prefix": "filings", "full_refresh": "true"}
    widgets = type("FakeWidgets", (), {"text": lambda self, name, default: values.setdefault(name, default),
                                       "get": lambda self, name: values[name]})()
    prep_chunks.dbutils = type("FakeDbutils", (), {"widgets": widgets})()
    try:
        args = prep_chunks.build_parser().parse_args(prep_chunks.notebook_argv(prep_chunks.build_parser()))
    finally:
        del prep_chunks.dbutils
    assert (args.prefix, args.full_refresh, args.chart_insights, args.metadata_schema) == ("filings", "true", "false", "")


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
