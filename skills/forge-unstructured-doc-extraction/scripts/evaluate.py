# /// script
# requires-python = ">=3.11"
# dependencies = ["mlflow[databricks]>=3.1", "databricks-sdk>=0.40", "databricks-agents>=1.9.3", "pandas>=2"]
# ///
"""Measure how often the AI Search index returns the right document and page (forge-unstructured-doc-extraction module 03).

    uv run <skill>/scripts/evaluate.py --state <workdir>/doc-index.json --eval-file <workdir>/evals.jsonl
    # No eval set yet? Synthesize one from the chunks table, write it to --eval-file, then evaluate:
    uv run <skill>/scripts/evaluate.py --state <workdir>/doc-index.json --eval-file <workdir>/evals.jsonl --synthesize 30

The eval file is JSON Lines, one question per line: {"question": ..., "file_name": ..., "pages": [N, ...]}.
`file_name` is the file that answers the question (no path), and the optional `pages` are 1-based.
`filters` (a dict) is passed to the search as-is. Rows without `file_name`, such as out-of-scope
questions, aren't scored. `expected_response` and `expected_facts` are kept for forge-doc-qa-agent.

--synthesize N samples chunks, writes N questions with generate_evals_df, and labels each with the file
and pages of the chunk it was written from. Each question is sent to the index as one hybrid search for
--k results. A question is a document hit when a result comes from its file, and a page hit when that
result also covers one of its pages. Results are logged to the MLflow experiment `eval_experiment` in
doc-index.json. Exits 1 when a threshold is missed, so CI can gate on it.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

THRESHOLDS = {"doc_hit_rate": 0.9, "page_hit_rate": 0.8}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MIN_CHUNK_CHARS = 300  # shorter chunks (headers, captions) make poor synthetic questions


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/doc-index.json")
    p.add_argument("--eval-file", required=True, type=Path, help="JSON Lines of questions")
    p.add_argument("--synthesize", type=int, default=0, metavar="N", help="First write N synthetic questions to --eval-file")
    p.add_argument("--k", type=int, default=8, help="Results per search (forge-doc-qa-agent retrieves 8)")
    return p.parse_args()


# --- scoring (pure) --------------------------------------------------------------------------------------

def as_list(value) -> list:
    """A list from a JSON string, an array-like, or None."""
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value) if value.strip() else []
    return list(value)


def expected_pages(row: dict) -> set[int]:
    pages = row["pages"] if "pages" in row else ([row["page"]] if row.get("page") is not None else [])
    return {int(p) for p in as_list(pages)}


def score(rows: list[dict], results: list[list[dict]]) -> dict:
    """Hit rates for labeled rows; results[i] holds row i's retrieved {file_name, page_numbers}, best first."""
    per_question, doc_hits, page_hits, ranks = [], [], [], []
    for row, retrieved in zip(rows, results):
        files = [r.get("file_name") for r in retrieved]
        rank = files.index(row["file_name"]) + 1 if row["file_name"] in files else None
        pages = expected_pages(row)
        page_hit = None
        if pages:
            page_hit = any(r.get("file_name") == row["file_name"] and pages & {int(p) for p in as_list(r.get("page_numbers"))}
                           for r in retrieved)
            page_hits.append(1.0 if page_hit else 0.0)
        doc_hits.append(1.0 if rank else 0.0)
        ranks.append(1.0 / rank if rank else 0.0)
        per_question.append({"question": row["question"], "file_name": row["file_name"], "pages": sorted(pages),
                             "rank": rank, "page_hit": page_hit, "retrieved": files})
    metrics = {}
    if doc_hits:
        metrics.update(doc_hit_rate=sum(doc_hits) / len(doc_hits), doc_mrr=sum(ranks) / len(ranks))
    if page_hits:
        metrics["page_hit_rate"] = sum(page_hits) / len(page_hits)
    return {"metrics": metrics, "questions": per_question}


def threshold_failures(metrics: dict) -> list[str]:
    return [f"{name}={metrics[name]:.3f} < {floor}" for name, floor in THRESHOLDS.items()
            if name in metrics and metrics[name] < floor]


def label_synthetic(generated: list[dict], chunks: dict[str, dict]) -> list[dict]:
    """Eval rows from generate_evals_df records. Each sampled chunk went in with its chunk_id as doc_uri."""
    rows = []
    for record in generated:
        expectations = record.get("expectations") or {}
        row = {"question": record["inputs"]["messages"][-1]["content"],
               "expected_facts": [str(fact) for fact in as_list(expectations.get("expected_facts"))]}
        context = expectations.get("expected_retrieved_context")
        context = [context] if isinstance(context, dict) else as_list(context)
        sources = [chunks[c["doc_uri"]] for c in context if isinstance(c, dict) and c.get("doc_uri") in chunks]
        if sources:
            row.update(file_name=sources[0]["file_name"], pages=sources[0]["pages"], chunk_id=sources[0]["chunk_id"])
        rows.append(row)
    return rows


# --- I/O ------------------------------------------------------------------------------------------------

def run_statement(w, warehouse_id: str, statement: str) -> list[list]:
    from databricks.sdk.service.sql import StatementState

    response = w.statement_execution.execute_statement(warehouse_id=warehouse_id, statement=statement, wait_timeout="50s")
    while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(5)
        response = w.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        raise SystemExit(f"Query failed: {response.status.error}")
    rows = list(response.result.data_array or [])
    for chunk in range(1, response.manifest.total_chunk_count or 1):
        rows += w.statement_execution.get_statement_result_chunk_n(response.statement_id, chunk).data_array or []
    return rows


def synthesize(w, state: dict, table: str, count: int) -> list[dict]:
    import pandas as pd
    from databricks.agents.evals import generate_evals_df

    sample = run_statement(w, state["warehouse_id"], (
        f"SELECT chunk_id, file_name, to_json(page_numbers), chunk_to_retrieve FROM {table} "
        f"WHERE length(chunk_to_retrieve) > {MIN_CHUNK_CHARS} ORDER BY rand() LIMIT {max(50, count * 3)}"
    ))
    if not sample:
        raise SystemExit(f"{table} has no chunks longer than {MIN_CHUNK_CHARS} characters to write questions from")
    chunks = {cid: {"chunk_id": cid, "file_name": name, "pages": as_list(pages)} for cid, name, pages, _ in sample}
    corpus = state.get("corpus_description") or "the documents"
    generated = generate_evals_df(
        pd.DataFrame([(text, cid) for cid, _, _, text in sample], columns=["content", "doc_uri"]),
        num_evals=count,
        agent_description=f"Answers questions about {corpus}, citing the file and page of each answer.",
        question_guidelines="Ask questions a user of these documents would ask, naming the entity, product, "
        "or period the passage is about. Vary between fact lookups, numbers, and questions that combine two facts.",
    )
    return label_synthetic(generated.to_dict("records"), chunks)


def search(w, index: str, row: dict, k: int) -> list[dict]:
    response = w.vector_search_indexes.query_index(
        index_name=index,
        columns=["chunk_id", "file_name", "page_numbers"],
        query_text=row["question"],
        query_type="HYBRID",  # the same search forge-doc-qa-agent's tool runs
        num_results=k,
        filters_json=json.dumps(row["filters"]) if row.get("filters") else None,
    )
    names = [c.name for c in response.manifest.columns]
    return [dict(zip(names, values)) for values in (response.result.data_array or [])]


def main() -> None:
    args = parse_args()
    state = json.loads(args.state.read_text())
    for key in ("catalog", "schema", "prefix", "index_name"):
        if not NAME_RE.match(str(state.get(key, ""))):
            raise SystemExit(f"doc-index.json: {key} is missing or invalid")
    for key in ("eval_experiment",) + (("warehouse_id",) if args.synthesize else ()):
        if not state.get(key):
            raise SystemExit(f"doc-index.json: {key} is missing; module 03 records it")
    if state.get("profile"):
        os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", state["profile"])
    index = f"{state['catalog']}.{state['schema']}.{state['index_name']}"
    chunks_table = f"`{state['catalog']}`.`{state['schema']}`.`{state['prefix']}_chunks`"

    import mlflow
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=state.get("profile"))
    if args.synthesize:
        generated = synthesize(w, state, chunks_table, args.synthesize)
        args.eval_file.write_text("".join(json.dumps(row) + "\n" for row in generated))
        unlabeled = sum(1 for row in generated if "file_name" not in row)
        print(f"wrote {len(generated)} synthetic questions to {args.eval_file} ({unlabeled} without a source chunk); "
              "have a domain expert review them")

    rows = [json.loads(line) for line in args.eval_file.read_text().splitlines() if line.strip()]
    if bad := [i + 1 for i, row in enumerate(rows) if not row.get("question")]:
        raise SystemExit(f"{args.eval_file}: lines {bad} have no question")
    labeled = [row for row in rows if row.get("file_name")]
    if not labeled:
        raise SystemExit(f"{args.eval_file}: no row has a file_name, so there's nothing to score retrieval against")
    result = score(labeled, [search(w, index, row, args.k) for row in labeled])

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(state["eval_experiment"])
    with mlflow.start_run(run_name="retrieval-eval") as run:
        mlflow.log_params({"index": index, "k": args.k, "questions": len(labeled), "skipped": len(rows) - len(labeled),
                           "synthetic": bool(args.synthesize)})
        mlflow.log_metrics(result["metrics"])
        mlflow.log_table({key: [json.dumps(q[key]) if isinstance(q[key], list) else q[key] for q in result["questions"]]
                          for key in ("question", "file_name", "pages", "rank", "page_hit", "retrieved")},
                         artifact_file="questions.json")

    print(json.dumps({"run_id": run.info.run_id, "metrics": result["metrics"], "skipped": len(rows) - len(labeled)}, indent=2))
    if failures := threshold_failures(result["metrics"]):
        print("FAILED thresholds: " + "; ".join(failures))
        sys.exit(1)
    print("all thresholds met")


if __name__ == "__main__":
    main()
