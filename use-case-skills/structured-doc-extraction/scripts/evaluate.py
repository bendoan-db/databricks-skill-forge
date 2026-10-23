# /// script
# requires-python = ">=3.11"
# dependencies = ["mlflow[databricks]>=3.1", "databricks-sdk>=0.40", "numpy>=1.26", "scipy>=1.11"]
# ///
"""Score extraction against labeled documents and log the results to MLflow (structured-doc-extraction module 05).

    uv run <skill>/scripts/evaluate.py --state <workdir>/extraction.json --ground-truth <workdir>/ground_truth.jsonl

Ground truth is JSON Lines, one labeled document (or packet sub-document) per line:

    {"file_name": "paystub_1.pdf", "doc_type": "paystub", "fields": {"employee_name": "Jordan Rivera",
     "gross": {"current_amount": 1884.0}, "deductions": [{"description": "Medicare", "amount": 27.32}]}}

For packets, add "page": the 1-based number of any page in the sub-document. Only labeled fields are
scored: leave a field out when it isn't labeled, and set it to null when the document truly lacks it.

Scoring is type-aware: strings match after lowercasing and collapsing whitespace, falling back to an LLM
judge (mlflow is_correct) on a miss unless --no-judge; numbers, integers, booleans, and enums match
exactly; objects average their fields; arrays use optimal one-to-one item matching and soft F1. Exits 1
when a threshold is missed, so CI can gate on it.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

THRESHOLDS = {"classification_accuracy": 0.95, "field_accuracy": 0.9, "array_soft_f1": 0.8}
CONFIDENCE_BINS = [0.0, 0.5, 0.8, 0.9, 1.0001]
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/extraction.json")
    p.add_argument("--ground-truth", required=True, type=Path, help="JSON Lines of labeled documents")
    p.add_argument("--no-judge", action="store_true", help="Exact string matching only (deterministic, no LLM calls)")
    return p.parse_args()


# --- scoring (pure) --------------------------------------------------------------------------------------

def strip_wrappers(node):
    """ai_extract leaf wrappers {value, citation_ids, confidence_score} collapsed to plain values."""
    if isinstance(node, dict) and "value" in node and set(node) <= {"value", "confidence_score", "citation_ids"}:
        return strip_wrappers(node["value"])
    if isinstance(node, dict):
        return {k: strip_wrappers(v) for k, v in node.items()}
    if isinstance(node, list):
        return [strip_wrappers(v) for v in node]
    return node


def confidences(node, path="", out=None) -> dict[str, float]:
    """confidence_score of every scalar leaf outside arrays, keyed by dotted path."""
    out = {} if out is None else out
    if isinstance(node, dict) and "value" in node and set(node) <= {"value", "confidence_score", "citation_ids"}:
        if isinstance(node.get("confidence_score"), (int, float)) and not isinstance(node["value"], (dict, list)):
            out[path] = float(node["confidence_score"])
    elif isinstance(node, dict):
        for key, child in node.items():
            confidences(child, f"{path}.{key}" if path else key, out)
    return out


def node_type(spec: dict) -> str:
    kind = spec.get("type")
    if kind == "array":
        return "array_object" if spec["items"].get("type") == "object" else "array"
    return kind


def is_absent(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or (isinstance(value, str) and not value.strip())


def norm_str(value) -> str:
    return "" if is_absent(value) else re.sub(r"\s+", " ", str(value).strip().lower())


def to_num(value):
    if is_absent(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    negative = str(value).strip().startswith("(") and str(value).strip().endswith(")")
    try:
        number = float(re.sub(r"[^0-9.\-]", "", str(value)))
    except ValueError:
        return None
    return -number if negative else number


def leaf_score(spec: dict, predicted, expected, judge=None, path="") -> float:
    if is_absent(predicted) and is_absent(expected):
        return 1.0
    if is_absent(predicted) or is_absent(expected):
        return 0.0
    kind = spec.get("type")
    if kind in ("number", "integer"):
        a, b = to_num(predicted), to_num(expected)
        return 1.0 if a is not None and b is not None and abs(a - b) < 1e-9 else 0.0
    if norm_str(predicted) == norm_str(expected):
        return 1.0
    if kind == "string" and judge is not None:
        return 1.0 if judge(path, predicted, expected) else 0.0
    return 0.0


def similarity(spec: dict, predicted, expected) -> float:
    """Judge-free similarity in [0, 1], used to pair array items. Only labeled object keys count."""
    kind = node_type(spec)
    if kind == "object":
        expected = expected if isinstance(expected, dict) else {}
        predicted = predicted if isinstance(predicted, dict) else {}
        keys = [k for k in spec["properties"] if k in expected]
        return sum(similarity(spec["properties"][k], predicted.get(k), expected[k]) for k in keys) / len(keys) if keys else 1.0
    if kind in ("array", "array_object"):
        return soft_f1(spec["items"], predicted or [], expected or [])[0]
    return leaf_score(spec, predicted, expected)


def optimal_pairs(item_spec: dict, predicted: list, expected: list) -> list[tuple[int, int, float]]:
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    matrix = np.array([[similarity(item_spec, p, e) for e in expected] for p in predicted])
    rows, cols = linear_sum_assignment(-matrix)
    return [(int(i), int(j), float(matrix[i, j])) for i, j in zip(rows, cols)]


def soft_f1(item_spec: dict, predicted: list, expected: list) -> tuple[float, list]:
    """Soft F1 over the optimal one-to-one item matching, and the pairs used."""
    if not predicted and not expected:
        return 1.0, []
    if not predicted or not expected:
        return 0.0, []
    pairs = optimal_pairs(item_spec, predicted, expected)
    total = sum(score for _, _, score in pairs)
    precision, recall = total / len(predicted), total / len(expected)
    return (2 * precision * recall / (precision + recall) if precision + recall else 0.0), pairs


def new_accumulator() -> dict:
    return {"leaf": defaultdict(list), "array": defaultdict(list), "object": defaultdict(list), "calibration": []}


def score(spec_fields: dict, predicted: dict, expected: dict, acc: dict, judge=None, conf=None, path="") -> float:
    """Score the labeled fields of one document; record per-field results in acc. Returns the mean."""
    conf = conf or {}
    predicted = predicted if isinstance(predicted, dict) else {}
    scores = []
    for name, spec in spec_fields.items():
        if name not in expected:
            continue  # not labeled: skip, don't count as absent
        here = f"{path}.{name}" if path else name
        kind = node_type(spec)
        if kind == "object":
            if isinstance(expected[name], dict):
                s = score(spec["properties"], predicted.get(name), expected[name], acc, judge, conf, here)
            else:  # labeled as wholly absent
                s = 1.0 if is_absent(predicted.get(name)) else 0.0
            acc["object"][here].append(s)
        elif kind in ("array", "array_object"):
            items = predicted.get(name) if isinstance(predicted.get(name), list) else []
            labels = expected[name] if isinstance(expected[name], list) else []
            s, pairs = soft_f1(spec["items"], items, labels)
            acc["array"][here].append(s)
            if kind == "array_object":
                for child, child_spec in spec["items"]["properties"].items():
                    matched = [
                        similarity(child_spec, items[i].get(child) if isinstance(items[i], dict) else None, labels[j][child])
                        for i, j, _ in pairs
                        if isinstance(labels[j], dict) and child in labels[j]
                    ]
                    if matched:
                        acc["array"][f"{here}.{child}"].append(sum(matched) / len(matched))
        else:
            s = leaf_score(spec, predicted.get(name), expected[name], judge, here)
            acc["leaf"][here].append(s)
            if here in conf:
                acc["calibration"].append((conf[here], s))
        scores.append(s)
    return sum(scores) / len(scores) if scores else 1.0


def calibration(pairs: list[tuple[float, float]]) -> dict:
    """Correctness rate per confidence bin, and whether it rises with confidence."""
    bins = []
    for low, high in zip(CONFIDENCE_BINS, CONFIDENCE_BINS[1:]):
        inside = [s for c, s in pairs if low <= c < high]
        if inside:
            bins.append({"confidence": f"[{low:.1f}, {min(high, 1.0):.1f}]", "values": len(inside), "correct_rate": sum(inside) / len(inside)})
    rates = [b["correct_rate"] for b in bins]
    return {"bins": bins, "monotonic": all(a <= b + 1e-9 for a, b in zip(rates, rates[1:]))}


def match_segment(truth: dict, segments: list[dict]) -> dict | None:
    """The predicted segment a ground-truth row labels: by page for packets, else segment 0."""
    if truth.get("page") is not None:
        page = int(truth["page"])
        return next((s for s in segments if page in s["page_numbers"]), None)
    return next((s for s in segments if s["segment_id"] == 0), None)


def evaluate(truths: list[dict], predictions: dict[str, list[dict]], types: dict, judge=None) -> dict:
    """Metrics for all ground-truth rows; predictions maps file_name to its predicted segments."""
    acc = new_accumulator()
    classified, rows = [], []
    for truth in truths:
        segment = match_segment(truth, predictions.get(truth["file_name"], []))
        expected_type = truth.get("doc_type")
        predicted_type = segment["doc_type"] if segment else None
        if expected_type:
            classified.append(1.0 if predicted_type == expected_type else 0.0)
        doc_type = expected_type or predicted_type
        if doc_type not in types:
            rows.append({"file_name": truth["file_name"], "page": truth.get("page"), "doc_type": doc_type,
                         "score": 0.0, "note": "no predicted segment" if segment is None else "unknown document type"})
            continue
        response = segment["response"] if segment and predicted_type == doc_type else {}
        document_score = score(types[doc_type]["schema"], strip_wrappers(response), truth.get("fields") or {},
                               acc, judge, confidences(response))
        rows.append({"file_name": truth["file_name"], "page": truth.get("page"), "doc_type": doc_type,
                     "score": document_score, "note": "" if segment else "no predicted segment"})

    leaf_scores = [s for values in acc["leaf"].values() for s in values]
    array_scores = [s for path, values in acc["array"].items() if "." not in path for s in values]
    metrics = {}
    if classified:
        metrics["classification_accuracy"] = sum(classified) / len(classified)
    if leaf_scores:
        metrics["field_accuracy"] = sum(leaf_scores) / len(leaf_scores)
    if array_scores:
        metrics["array_soft_f1"] = sum(array_scores) / len(array_scores)
    per_field = (
        [{"field": p, "metric": "accuracy", "value": sum(v) / len(v), "documents": len(v)} for p, v in acc["leaf"].items()]
        + [{"field": p, "metric": "soft_f1", "value": sum(v) / len(v), "documents": len(v)} for p, v in acc["array"].items()]
        + [{"field": p, "metric": "object_score", "value": sum(v) / len(v), "documents": len(v)} for p, v in acc["object"].items()]
    )
    return {"metrics": metrics, "per_field": per_field, "documents": rows, "calibration": calibration(acc["calibration"])}


def threshold_failures(metrics: dict) -> list[str]:
    return [f"{name}={metrics[name]:.3f} < {floor}" for name, floor in THRESHOLDS.items()
            if name in metrics and metrics[name] < floor]


# --- I/O ------------------------------------------------------------------------------------------------

def fetch_predictions(state: dict, file_names: list[str]) -> dict[str, list[dict]]:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementParameterListItem, StatementState

    for key in ("catalog", "schema", "prefix"):
        if not NAME_RE.match(str(state.get(key, ""))):
            raise SystemExit(f"extraction.json: {key} is missing or invalid")
    table = f"`{state['catalog']}`.`{state['schema']}`.`{state['prefix']}_extracted`"
    w = WorkspaceClient(profile=state.get("profile"))
    response = w.statement_execution.execute_statement(
        warehouse_id=state["warehouse_id"],
        wait_timeout="50s",
        statement=(
            f"SELECT regexp_extract(path, '[^/]+$', 0) AS file_name, segment_id, doc_type, "
            f"to_json(transform(page_ids, i -> i + 1)) AS page_numbers, to_json(extracted:response) AS response "
            f"FROM {table} WHERE array_contains(from_json(:names, 'ARRAY<STRING>'), regexp_extract(path, '[^/]+$', 0))"
        ),
        parameters=[StatementParameterListItem(name="names", value=json.dumps(file_names))],
    )
    while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(5)
        response = w.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        raise SystemExit(f"Reading {table} failed: {response.status.error}")
    rows = list(response.result.data_array or [])
    for chunk in range(1, response.manifest.total_chunk_count or 1):
        rows += w.statement_execution.get_statement_result_chunk_n(response.statement_id, chunk).data_array or []
    predictions = defaultdict(list)
    for file_name, segment_id, doc_type, pages, body in rows:
        predictions[file_name].append({
            "segment_id": int(segment_id), "doc_type": doc_type,
            "page_numbers": json.loads(pages or "[]"), "response": json.loads(body or "{}"),
        })
    return predictions


def make_judge():
    from mlflow.genai.judges import is_correct

    cache = {}

    def judge(path, predicted, expected) -> bool:
        key = (path, str(predicted), str(expected))
        if key not in cache:
            feedback = is_correct(
                request=f"What is the value of the field '{path}' in this document?",
                response=str(predicted),
                expected_response=str(expected),
            )
            cache[key] = str(feedback.value).lower() == "yes"
        return cache[key]

    return judge


def main() -> None:
    args = parse_args()
    state = json.loads(args.state.read_text())
    for key in ("warehouse_id", "experiment", "document_types"):
        if not state.get(key):
            raise SystemExit(f"extraction.json: {key} is missing; module 05 step 2 records it")
    truths = [json.loads(line) for line in args.ground_truth.read_text().splitlines() if line.strip()]
    if bad := [i + 1 for i, t in enumerate(truths) if not t.get("file_name") or not isinstance(t.get("fields", {}), dict)]:
        raise SystemExit(f"{args.ground_truth}: lines {bad} need file_name and a fields object")
    if state.get("profile"):
        os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", state["profile"])

    import mlflow

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(state["experiment"])
    predictions = fetch_predictions(state, sorted({t["file_name"] for t in truths}))
    result = evaluate(truths, predictions, state["document_types"], None if args.no_judge else make_judge())

    with mlflow.start_run(run_name="extraction-eval") as run:
        mlflow.log_params({"layout": state.get("layout"), "documents": len(truths), "llm_judge": not args.no_judge})
        mlflow.log_metrics(result["metrics"])
        mlflow.log_metrics({
            re.sub(r"[^A-Za-z0-9_./ -]", "_", f"{row['metric']}/{row['field']}"): row["value"] for row in result["per_field"]
        })
        mlflow.log_table({k: [row[k] for row in result["per_field"]] for k in ("field", "metric", "value", "documents")},
                         artifact_file="per_field.json")
        mlflow.log_table({k: [row[k] for row in result["documents"]] for k in ("file_name", "page", "doc_type", "score", "note")},
                         artifact_file="documents.json")
        mlflow.log_dict(result["calibration"], "calibration.json")

    print(json.dumps({"run_id": run.info.run_id, "metrics": result["metrics"], "calibration": result["calibration"]}, indent=2))
    if failures := threshold_failures(result["metrics"]):
        print("FAILED thresholds: " + "; ".join(failures))
        sys.exit(1)
    print("all thresholds met")


if __name__ == "__main__":
    main()
