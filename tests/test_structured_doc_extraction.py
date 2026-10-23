"""Checks for use-case-skills/structured-doc-extraction that need no workspace (cross-skill checks are in test_skills.py)."""

import argparse
import ast
import json
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS_DIR, load_script, run_script

SKILL = "structured-doc-extraction"
SCRIPTS = SKILLS_DIR / SKILL / "scripts"
extract = load_script(SKILL, "extract")

PAYSTUB = {
    "employee_name": {"type": "string"},
    "pay_period_end": {"type": "string", "description": "MM/DD/YYYY"},
    "gross": {"type": "object", "properties": {"current": {"type": "number"}, "year_to_date": {"type": "number"}}},
    "deductions": {"type": "array", "items": {"type": "object", "properties": {
        "description": {"type": "string"}, "amount": {"type": "number"}}}},
    "flags": {"type": "array", "items": {"type": "string"}},
}
STATE = {
    "profile": "DEFAULT",
    "volume_path": "/Volumes/main/loans/packets",
    "catalog": "main",
    "schema": "loans",
    "prefix": "loan_docs",
    "name": "loan-docs",
    "layout": "packet",
    "classify_instructions": "Pages come from mortgage loan files.",
    "document_types": {
        "paystub": {"description": "A pay stub for one pay period", "schema": PAYSTUB,
                    "instructions": "Don't reformat amounts."},
        "bank_statement": {"description": "A bank account statement", "pages": "multi",
                           "schema": {"ending_balance": {"type": "number"}}},
    },
}


def render(*args: str, check: bool = True):
    return run_script(SKILL, "render.py", *args, check=check)


def write_state(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "extraction.json"
    path.write_text(json.dumps({**STATE, **overrides}))
    return path


def table(suffix: str) -> str:
    return f"`main`.`loans`.`loan_docs_{suffix}`"


def test_every_sql_placeholder_has_a_value():
    args = argparse.Namespace(catalog="main", schema="loans", prefix="loan_docs")
    params = extract.build_params(args, STATE)
    gold, child = extract.type_tables(table, "paystub", PAYSTUB)[:2]
    extra = {
        "target_table": table("segments"), "doc_type_literal": "'paystub'",
        "extract_schema": extract.sql_string(json.dumps(PAYSTUB)),
        "extract_options": extract.extract_options(STATE["document_types"]["paystub"]), **gold, **child,
    }
    for sql in extract.load_sections(SCRIPTS / "extract.sql").values():
        extract.render(sql, {**params, **extra})
    localize = load_script(SKILL, "localize")
    localize_params = localize.build_params(args)
    for sql in localize.load_sections(SCRIPTS / "localize.sql").values():
        localize.render(sql, localize_params)
    assert params["multi_page_types"] == "'bank_statement'"
    assert "'version', '2.1'" in params["classify_options"]


def test_valid_config_passes():
    assert extract.validate_config(STATE) == []


@pytest.mark.parametrize("change, message", [
    ({"layout": "folder"}, "layout"),
    ({"document_types": {"Pay Stub": {"description": "x", "schema": PAYSTUB}}}, "snake_case"),
    ({"document_types": {"other": {"description": "x", "schema": PAYSTUB}}}, "not ["),
    ({"document_types": {"paystub": {"schema": PAYSTUB}}}, "description is required"),
    ({"document_types": {"paystub": {"description": "x", "schema": {"value": {"type": "string"}}}}}, "'value'"),
    ({"document_types": {"paystub": {"description": "x", "schema": {"a": {"type": "enum"}}}}}, "labels"),
    ({"document_types": {"paystub": {"description": "x", "schema": {"rows": {"type": "array", "items": {
        "type": "object", "properties": {"cells": {"type": "array", "items": {"type": "string"}}}}}}}}}, "inside an array"),
    ({"document_types": {"paystub": {"description": "x", "schema": {"path": {"type": "string"}}}}}, "clashes"),
    ({"layout": "single_type"}, "exactly one"),
    ({"review_threshold": 1.5}, "review_threshold"),
])
def test_invalid_configs_are_reported(change, message):
    errors = extract.validate_config({**STATE, **change})
    assert any(message in e for e in errors), errors


def test_flatten_plans_columns_arrays_and_child_tables():
    scalars, arrays, children = extract.flatten(PAYSTUB)
    assert [c for c, _, _ in scalars] == ["employee_name", "pay_period_end", "gross__current", "gross__year_to_date"]
    assert arrays == [("flags", ("flags",), "STRING")]
    assert children == [("deductions", ("deductions",), [
        ("description", ("description",), "STRING"), ("amount", ("amount",), "DOUBLE")])]
    gold, child = extract.type_tables(table, "paystub", PAYSTUB)
    assert gold["gold_table"] == table("paystub") and child["child_table"] == table("paystub__deductions")
    assert extract.expected_columns(gold)[:3] == ["employee_name", "employee_name__confidence", "pay_period_end"]
    assert "'$.response.gross.current.value', 'DOUBLE') AS `gross__current`" in gold["gold_column_select"]


def test_inputs_scope_to_segment_pages_only_for_packets():
    assert extract.input_expr("variant", scoped=False) == "parsed"
    assert "array_contains(page_ids, pg:id::INT)" in extract.input_expr("variant", scoped=True)
    assert extract.input_expr("text", scoped=False).startswith("array_join(")


def test_leaf_fields_walks_wrappers_objects_and_arrays():
    response = {
        "employee_name": {"value": "Jordan Rivera", "confidence_score": 0.97, "citation_ids": [0]},
        "gross": {"current": {"value": 1884.0, "confidence_score": 0.6, "citation_ids": [1, 2]}},
        "deductions": [{"amount": {"value": 27.32, "confidence_score": 0.9, "citation_ids": [3]}}],
        "flags": {"value": ["overtime"], "citation_ids": []},
        "pay_period_end": {"value": None, "confidence_score": 0.2},
    }
    assert extract.leaf_fields(response) == [
        ("employee_name", "Jordan Rivera", 0.97, [0]),
        ("gross.current", "1884.0", 0.6, [1, 2]),
        ("deductions[0].amount", "27.32", 0.9, [3]),
        ("flags[0]", "overtime", None, None),
        ("pay_period_end", None, 0.2, None),
    ]


def test_render_packet_job_with_localization(tmp_path: Path):
    state = write_state(tmp_path, localize=True, page_images_path="/Volumes/main/loans/img",
                        ocr_cluster={"spark_version": "17.3.x-scala2.13", "node_type_id": "i3.xlarge"})
    render("--state", str(state))
    bundle = tmp_path / "loan-docs-extract"
    job = yaml.safe_load((bundle / "databricks.yml").read_text())["resources"]["jobs"]["doc_extraction"]
    assert [t["task_key"] for t in job["tasks"]] == ["parse", "extract", "localize"]
    assert job["job_clusters"][0]["new_cluster"]["spark_version"] == "17.3.x-scala2.13"
    assert job["trigger"]["pause_status"] == "UNPAUSED"
    config = json.loads((bundle / "src" / "extraction_config.json").read_text())
    assert config["document_types"] == STATE["document_types"] and config["review_threshold"] == 0.8
    assert {"localize.py", "localize.sql", "install_tesseract.sh"} <= {p.name for p in (bundle / "src").iterdir()}


def test_render_without_types_pauses_the_trigger_and_drops_localize(tmp_path: Path):
    state = write_state(tmp_path, document_types={})
    render("--state", str(state))
    job = yaml.safe_load((tmp_path / "loan-docs-extract" / "databricks.yml").read_text())["resources"]["jobs"]["doc_extraction"]
    assert job["trigger"]["pause_status"] == "PAUSED"
    assert [t["task_key"] for t in job["tasks"]] == ["parse", "extract"] and "job_clusters" not in job


def test_render_localize_requires_page_images_and_cluster(tmp_path: Path):
    result = render("--state", str(write_state(tmp_path, localize=True)), check=False)
    assert result.returncode == 1 and "page_images_path" in result.stderr and "ocr_cluster" in result.stderr


def test_render_protects_hand_edited_config(tmp_path: Path):
    state = write_state(tmp_path)
    render("--state", str(state))
    config = tmp_path / "loan-docs-extract" / "src" / "extraction_config.json"
    config.write_text(config.read_text().replace('"review_threshold": 0.8', '"review_threshold": 0.5'))
    assert render("--state", str(state), check=False).returncode == 1
    render("--state", str(state), "--force")
    assert '"review_threshold": 0.8' in config.read_text()


# --- evaluate.py ---------------------------------------------------------------------------------------

evaluate = load_script(SKILL, "evaluate")
TYPES = {"paystub": {"schema": PAYSTUB}}


def wrapped(value, confidence=0.9):
    return {"value": value, "confidence_score": confidence, "citation_ids": [0]}


def segment(response, doc_type="paystub", segment_id=0, pages=(1,)):
    return {"segment_id": segment_id, "doc_type": doc_type, "page_numbers": list(pages), "response": response}


def test_scoring_skips_unlabeled_fields_and_scores_absence():
    prediction = {"employee_name": wrapped("JORDAN  rivera"), "pay_period_end": wrapped("01/31/2026"),
                  "gross": {"current": wrapped(1884.0), "year_to_date": wrapped(9000.0)}}
    truth = {"file_name": "a.pdf", "doc_type": "paystub",
             "fields": {"employee_name": "Jordan Rivera", "gross": {"current": "1,884.00"}, "flags": None}}
    result = evaluate.evaluate([truth], {"a.pdf": [segment(prediction)]}, TYPES)
    assert result["metrics"]["field_accuracy"] == 1.0  # pay_period_end and gross.year_to_date aren't labeled
    assert result["metrics"]["classification_accuracy"] == 1.0
    assert result["metrics"]["array_soft_f1"] == 1.0  # flags labeled absent, predicted absent


def test_scoring_arrays_with_optimal_matching():
    pytest.importorskip("scipy")
    prediction = {"deductions": [{"description": wrapped("Medicare"), "amount": wrapped(27.32)},
                                 {"description": wrapped("Federal"), "amount": wrapped(100.0)},
                                 {"description": wrapped("Extra"), "amount": wrapped(1.0)}]}
    truth = {"file_name": "a.pdf", "fields": {"deductions": [
        {"description": "Federal", "amount": 100}, {"description": "Medicare", "amount": 27.32}]}}
    result = evaluate.evaluate([truth], {"a.pdf": [segment(prediction)]}, TYPES)
    assert result["metrics"]["array_soft_f1"] == pytest.approx(0.8)  # precision 2/3, recall 1
    per_field = {(r["field"], r["metric"]): r["value"] for r in result["per_field"]}
    assert per_field[("deductions.amount", "soft_f1")] == 1.0


def test_scoring_uses_the_judge_only_for_string_misses():
    calls = []

    def judge(path, predicted, expected):
        calls.append(path)
        return predicted == "Jordan R."

    prediction = {"employee_name": wrapped("Jordan R."), "gross": {"current": wrapped(1.0)}}
    truth = {"file_name": "a.pdf", "fields": {"employee_name": "Jordan Rivera", "gross": {"current": 2.0}}}
    result = evaluate.evaluate([truth], {"a.pdf": [segment(prediction)]}, TYPES, judge)
    assert calls == ["employee_name"] and result["metrics"]["field_accuracy"] == 0.5


def test_packet_labels_match_segments_by_page_and_wrong_types_score_zero():
    truths = [{"file_name": "loan.pdf", "doc_type": "paystub", "page": 4, "fields": {"employee_name": "A"}}]
    predictions = {"loan.pdf": [segment({"employee_name": wrapped("A")}, "bank_statement", 0, (1, 2, 3)),
                                segment({"employee_name": wrapped("A")}, "paystub", 1, (4,))]}
    assert evaluate.evaluate(truths, predictions, TYPES)["metrics"]["field_accuracy"] == 1.0
    predictions["loan.pdf"][1]["doc_type"] = "bank_statement"
    result = evaluate.evaluate(truths, predictions, TYPES)
    assert result["metrics"]["classification_accuracy"] == 0.0 and result["metrics"]["field_accuracy"] == 0.0


def test_calibration_and_threshold_failures():
    calibration = evaluate.calibration([(0.3, 0.0), (0.95, 1.0), (0.85, 1.0), (0.82, 0.0)])
    assert [b["correct_rate"] for b in calibration["bins"]] == [0.0, 0.5, 1.0] and calibration["monotonic"]
    assert evaluate.threshold_failures({"field_accuracy": 0.85}) == ["field_accuracy=0.850 < 0.9"]


# --- localize.py ---------------------------------------------------------------------------------------

def test_localize_finds_the_printed_number_inside_the_cited_element():
    pytest.importorskip("rapidfuzz")
    localize = load_script(SKILL, "localize")
    tokens = [{"text": "Gross", "box": [0, 0, 40, 10]}, {"text": "Pay", "box": [45, 0, 70, 10]},
              {"text": "$1,884.00", "box": [100, 0, 160, 10]}]
    element_text = "Gross Pay $1,884.00 YTD $9,000.00"
    box, score, matched, ok = localize.locate("1884.0", "numeric", element_text, tokens, (500, 200))
    assert ok and score == 100.0 and matched == "$1,884.00" and box == [600, 200, 660, 210]
    box, score, _, ok = localize.locate("Gross Pay", "text", element_text, tokens, (0, 0))
    assert ok and box == [0, 0, 70, 10]


def test_localize_matches_citations_to_elements_and_classifies_values():
    pytest.importorskip("rapidfuzz")
    localize = load_script(SKILL, "localize")
    elements = [{"id": 1, "content": "<td>Net</td>", "bbox": [{"page_id": 0, "coord": [0, 0, 100, 100]}]},
                {"id": 2, "content": "Other", "bbox": [{"page_id": 1, "coord": [0, 0, 100, 100]}]}]
    element, text, overlap = localize.element_for_citation({"bbox": [{"page_id": 0, "coord": [10, 10, 50, 50]}]}, elements)
    assert element["id"] == 1 and text == "Net" and overlap == 1.0
    assert localize.value_kind("1884.0") == "numeric" and localize.value_kind("01/31/2026") == "text"


def test_localization_gate_matches_scenarios():
    tree = ast.parse((SCRIPTS / "localize.py").read_text())
    gate = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "MIN_HIT_RATE")
    scenarios = yaml.safe_load((SKILLS_DIR / SKILL / "evals" / "scenarios.yaml").read_text())
    assert gate == scenarios["defaults"]["localization_hit_rate"]
